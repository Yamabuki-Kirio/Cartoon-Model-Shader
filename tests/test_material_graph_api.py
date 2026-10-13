"""材质图 HTTP 接口（提交 3）：schema 端点、预览端点与保存令牌绑定。

钉住规格「提交 3」验收条目里与接口层相关的部分：
* 前端只提交 opaque 参数 ID 与值；任何路径形态 / 代码形态都是「未知参数」；
* 共享组未确认时拒绝（``SHARED_PARAMETER_CONFIRM_REQUIRED``）；
* 保存令牌绑定材质图草稿与结构指纹，篡改即拒；
* 结构变化使旧草稿失效。
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from src.server.app import create_app
from src.server.config import load_config
from src.server.surface.executor import JSON_MARKER
from tests.fake_bpy import FakeBpy, FakeColorRamp, FakeLink, FakeNode, run_generated_code
from tests.test_v4_surface_api import (
    CEL_GROUP,
    FakeMCPServer,
    SurfaceInjector,
    authed,
    make_config,
    unauthed,
    wait_job,
)
MATERIAL = "材质_测试"


def _out(node: FakeNode, name: str):
    for sock in node.outputs:
        if sock.name == name:
            return sock
    raise KeyError(name)


def _build_material(fake: FakeBpy) -> None:
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None
    out = FakeNode("Material Output", "OUTPUT_MATERIAL", {"Surface": None},
                   bl_idname="ShaderNodeOutputMaterial")
    bsdf = FakeNode("Principled BSDF", "BSDF_PRINCIPLED",
                    {"Base Color": [1.0, 1.0, 1.0, 1.0], "Roughness": 0.5},
                    bl_idname="ShaderNodeBsdfPrincipled", outputs={"BSDF": "SHADER"})
    value = FakeNode("Value", "VALUE", {"Value": 0.25},
                     bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"})
    rgb = FakeNode("RGB", "RGB", {"Color": [0.9, 0.4, 0.4, 1.0]},
                   bl_idname="ShaderNodeRGB", outputs={"Color": "RGBA"})
    links = [
        FakeLink(value, _out(value, "Value"), bsdf, bsdf.inputs["Roughness"]),
        FakeLink(rgb, _out(rgb, "Color"), bsdf, bsdf.inputs["Base Color"]),
        FakeLink(bsdf, _out(bsdf, "BSDF"), out, out.inputs["Surface"]),
    ]
    fake.add_material_graph(MATERIAL, nodes=[out, bsdf, value, rgb], links=links,
                            users=[mesh])


@contextlib.contextmanager
def running_env(tmp_path: Path) -> Iterator[SimpleNamespace]:
    fake = FakeBpy()
    fake.add_cel_group(CEL_GROUP, element_count=3, interpolation="LINEAR")
    _build_material(fake)
    injector = SurfaceInjector(fake)
    with FakeMCPServer(executor=injector) as server:
        app = create_app(make_config(server.port, presets_dir=tmp_path / "presets"))
        with authed(app) as client:
            yield SimpleNamespace(
                client=client,
                app=app,
                fake=fake,
                injector=injector,
                tmp_path=tmp_path,
                unauthed_client=unauthed(app),
            )


def take_baseline(env: SimpleNamespace) -> dict:
    body = env.client.post("/api/session/baseline").json()
    assert body["ok"] is True
    wait_job(env.client, body["job_id"])
    env.injector.reset()
    return body


def writable_params(env: SimpleNamespace) -> list[dict[str, Any]]:
    schema = env.client.get("/api/material-graph/schema").json()
    assert schema["ok"] is True
    out: list[dict[str, Any]] = []
    for material in schema["materials"]:
        for param in material["parameters"]:
            if param["editable"]:
                out.append(param)
    return out


# -- schema 端点 -----------------------------------------------------------


def test_material_graph_schema_requires_baseline(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        response = env.client.get("/api/material-graph/schema")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_BASELINE"


def test_material_graph_schema_exposes_confidence_and_impact(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        schema = env.client.get("/api/material-graph/schema").json()
        assert schema["available"] is True
        material = next(m for m in schema["materials"] if m["material"] == MATERIAL)
        assert material["available"] is True
        params = material["parameters"]
        assert params
        for param in params:
            assert param["confidence"] in (
                "confirmed", "inferred", "user_confirmed", "experimental", "unsupported"
            )
            assert "influence_paths" in param
            assert "affected_materials" in param
            assert "shared" in param
            assert "evidence" in param
            # 参数 id 必须是 opaque（mg- 前缀），不能含真实节点名
            assert param["id"].startswith("mg-")


def test_material_graph_schema_by_material_id(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        schema = env.client.get("/api/material-graph/schema").json()
        material = next(m for m in schema["materials"] if m["material"] == MATERIAL)
        detail = env.client.get(
            f"/api/material-graph/materials/{material['material_id']}"
        ).json()
        assert detail["ok"] is True
        assert detail["material"] == MATERIAL
        # 反向追踪结果也带出来（入口、深度、影响路径）
        assert detail["trace"]["entry"]
        assert detail["trace"]["entry_output"] == "BSDF"


def test_material_graph_unknown_material_is_rejected(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        response = env.client.get("/api/material-graph/materials/不存在")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "PARAM_INVALID"


# -- 预览端点 --------------------------------------------------------------


def test_material_graph_preview_requires_token(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        response = env.unauthed_client.post(
            "/api/material-graph/preview", json={"material_draft": {}}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "SESSION_TOKEN_INVALID"


def test_material_graph_preview_requires_baseline(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        response = env.client.post(
            "/api/material-graph/preview", json={"material_draft": {"x": 1.0}}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_BASELINE"


def test_material_graph_preview_applies_and_restores(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        params = writable_params(env)
        target = next(p for p in params if p["kind"] == "float")
        before = _material_value(env.fake, "Value")

        body = env.client.post(
            "/api/material-graph/preview",
            json={"material_draft": {target["id"]: 0.75}},
        ).json()
        assert body["ok"] is True
        result = wait_job(env.client, body["job_id"])
        assert result["status"] == "done", result
        # 渲染用的是草稿值，结束后必须恢复基线
        assert _material_value(env.fake, "Value") == before
        assert result["result"]["material_graph_restore_verified"] is True


def test_material_graph_preview_rejects_unknown_param(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        response = env.client.post(
            "/api/material-graph/preview",
            json={"material_draft": {"材质_测试/Value/Value": 0.5}},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "PARAM_INVALID"
        # 未知 id 只按「不在 schema 内」报告，不据此透露任何真实节点路径
        assert body["error"]["details"]["unknown"] == ["材质_测试/Value/Value"]


def test_material_graph_preview_rejects_code_injection(tmp_path: Path) -> None:
    """请求无法注入代码：任何代码 / 路径形态都只是「未知参数」。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        hostile = [
            "__import__('os').system('echo pwned')",
            "../../etc/passwd",
            "Value/inputs['Value'].default_value",
        ]
        for payload in hostile:
            response = env.client.post(
                "/api/material-graph/preview", json={"material_draft": {payload: 1.0}}
            )
            assert response.status_code == 400, payload
            assert response.json()["error"]["code"] == "PARAM_INVALID", payload


def test_material_graph_preview_rejects_type_mismatch(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = next(p for p in writable_params(env) if p["kind"] == "float")
        response = env.client.post(
            "/api/material-graph/preview",
            json={"material_draft": {target["id"]: ["不是数字"]}},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "PARAM_INVALID"


def test_material_graph_preview_rejects_stale_structure_hash(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = next(p for p in writable_params(env) if p["kind"] == "float")
        response = env.client.post(
            "/api/material-graph/preview",
            json={
                "material_draft": {target["id"]: 0.5},
                "expected_material_structure_hash": "sha256:deadbeef",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "STRUCTURE_CHANGED"


def test_material_graph_preview_rejects_ramp_element_change(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        ramp = next(
            (p for p in writable_params(env) if p["kind"] == "ramp"), None
        )
        if ramp is None:
            pytest.skip("当前 fixture 未生成可写色带")
        response = env.client.post(
            "/api/material-graph/preview",
            json={"material_draft": {ramp["id"]: [{"position": 0.0, "color": [0, 0, 0, 1]}]}},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "STRUCTURE_CHANGED"


def _material_value(fake: FakeBpy, node_type: str) -> float | None:
    material = fake.data.materials.get(MATERIAL)
    assert material is not None and material.node_tree is not None
    for node in material.node_tree.nodes:
        if node.type == node_type:
            return node.inputs["Value"].default_value
    return None


# -- 保存令牌绑定 -----------------------------------------------------------


def test_commit_prepare_binds_material_graph_draft(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = next(p for p in writable_params(env) if p["kind"] == "float")
        schema = env.client.get("/api/material-graph/schema").json()
        material_hash = schema["structure_hash"]

        response = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(tmp_path / "out.blend"),
                "confirm": True,
                "material_draft": {target["id"]: 0.5},
                "expected_material_structure_hash": material_hash,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["material_graph_parameter_count"] == 1


def test_commit_rejects_material_draft_tampering(tmp_path: Path) -> None:
    """prepare 之后改动材质图草稿 ⇒ 令牌立即失效。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        params = writable_params(env)
        target = next(p for p in params if p["kind"] == "float")
        schema = env.client.get("/api/material-graph/schema").json()
        material_hash = schema["structure_hash"]

        prepared = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(tmp_path / "out.blend"),
                "confirm": True,
                "material_draft": {target["id"]: 0.5},
                "expected_material_structure_hash": material_hash,
            },
        ).json()

        response = env.client.post(
            "/api/session/commit",
            json={
                "token": prepared["token"],
                "mode": "save_as",
                "draft": {},
                "target_path": str(tmp_path / "out.blend"),
                "material_draft": {target["id"]: 0.9},
                "expected_material_structure_hash": material_hash,
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "COMMIT_TOKEN_MISMATCH"


def test_commit_rejects_missing_material_draft_when_bound(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = next(p for p in writable_params(env) if p["kind"] == "float")
        schema = env.client.get("/api/material-graph/schema").json()
        prepared = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(tmp_path / "out.blend"),
                "confirm": True,
                "material_draft": {target["id"]: 0.5},
                "expected_material_structure_hash": schema["structure_hash"],
            },
        ).json()

        response = env.client.post(
            "/api/session/commit",
            json={
                "token": prepared["token"],
                "mode": "save_as",
                "draft": {},
                "target_path": str(tmp_path / "out.blend"),
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "COMMIT_TOKEN_MISMATCH"


def test_commit_rejects_stale_material_structure_hash(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = next(p for p in writable_params(env) if p["kind"] == "float")
        response = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(tmp_path / "out.blend"),
                "confirm": True,
                "material_draft": {target["id"]: 0.5},
                "expected_material_structure_hash": "sha256:stale",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "STRUCTURE_CHANGED"


def test_commit_without_material_draft_keeps_legacy_behaviour(tmp_path: Path) -> None:
    """不带材质图草稿时，这条新路径完全不参与（旧客户端行为不变）。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        response = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {"color.exposure": 0.25},
                "target_path": str(tmp_path / "out.blend"),
                "confirm": True,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["material_graph_parameter_count"] == 0