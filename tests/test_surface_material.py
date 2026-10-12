"""逐材质选择与编辑竖切测试。

钉住六件事：

1. **探针**：只报告被对象引用的材质；带使用对象、节点结构签名、着色器候选插座；
   材质输出不唯一 / Surface 未连线时**不返回着色器**（绝不猜）。
2. **适配器**：只把「结构确认」的候选插座生成为可编辑参数；其余一律只读占位并给出原因。
3. **分类**：走仓库内唯一规则源（``material_rules.json``），只读展示；规则不可用也不拖垮基线。
4. **执行器**：材质写入走 ``材质名/节点名/插座名``，颜色与标量共用一条写入路径。
5. **预览**：材质草稿进同一个任务、渲染后完整恢复基线（无污染预览）。
6. **保存**：材质草稿参与 prepare → commit；材质身份或节点结构变化时**阻止保存**。

所有断言都跑在假 ``bpy`` 桩上：桩**真实执行**服务端生成的代码。真机验收仍是待办。
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from src.server import surface_probe
from src.server.app import create_app
from src.server.surface import material as material_module
from src.server.surface.draft import DraftError, validate_draft
from src.server.surface.executor import build_apply_code, extract_json
from src.server.surface.schema import flatten
from tests.fake_bpy import FakeBpy, run_generated_code
from tests.fake_mcp_server import FakeMCPServer
from tests.support import authed, make_config

MATERIAL = "皮肤_脸"
BASE_COLOR_ID = f"material.{MATERIAL}.base_color"
ROUGHNESS_ID = f"material.{MATERIAL}.roughness"
EMISSION_STRENGTH_ID = f"material.{MATERIAL}.emission_strength"
CEL_GROUP = "Cel_Skin"


# -- 桩工具 ----------------------------------------------------------------


def build_fake(**material_kwargs: Any) -> FakeBpy:
    """一个 Cel 组 + 一个挂在网格上的材质。"""
    fake = FakeBpy()
    fake.add_cel_group(CEL_GROUP, element_count=3)
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None
    fake.add_material(MATERIAL, users=[mesh], **material_kwargs)
    return fake


def probe_materials(fake: FakeBpy) -> list[dict[str, Any]]:
    payload = surface_probe.describe_groups(
        surface_probe.redact_describe(
            surface_probe.parse_describe(
                run_generated_code(surface_probe.build_describe_code(), fake)
            )
        )
    )
    return list(payload["materials"])


def material_nodes(fake: FakeBpy) -> dict[str, Any]:
    """只建材质分组（不走分类器，避免测试依赖规则文件内容）。"""
    describe = {"materials": probe_materials(fake)}
    groups = material_module.build_material_groups(describe, classifier=None)
    return flatten(groups)


def socket_value(fake: FakeBpy, node_name: str, socket_name: str) -> Any:
    material = fake.data.materials.get(MATERIAL)
    assert material is not None and material.node_tree is not None
    socket = material.node_tree.nodes.get(node_name).inputs.get(socket_name)
    assert socket is not None
    return socket.default_value


# -- 1. 探针 ---------------------------------------------------------------


def test_probe_reports_only_materials_used_by_objects() -> None:
    fake = build_fake()
    fake.data.materials.add(__import__("tests.fake_bpy", fromlist=["FakeMaterial"]).FakeMaterial("未挂载材质"))

    names = [item["name"] for item in probe_materials(fake)]
    assert names == [MATERIAL], "没挂到任何对象上的材质不属于受管范围"


def test_probe_reports_users_structure_and_candidate_sockets() -> None:
    fake = build_fake(sockets={"Roughness": 0.42})
    report = probe_materials(fake)[0]

    assert report["users"] == ["model_mesh"], "必须报告使用对象"
    assert report["node_count"] == 2
    assert "Principled BSDF:BSDF_PRINCIPLED" in report["structure_signature"]

    shader = report["shader"]
    assert shader["name"] == "Principled BSDF"
    assert shader["bl_idname"] == "ShaderNodeBsdfPrincipled"
    assert shader["linked_to_output"] is True
    sockets = {item["name"]: item for item in shader["sockets"]}
    assert sockets["Roughness"]["value"] == pytest.approx(0.42)
    assert sockets["Base Color"]["type"] == "RGBA"
    assert sockets["Metallic"]["type"] == "VALUE"
    # 只报告候选插座：非候选（Alpha / IOR）不进载荷
    assert "Alpha" not in sockets


def test_probe_returns_no_shader_when_output_is_ambiguous() -> None:
    assert probe_materials(build_fake(outputs=2))[0]["shader"] is None
    assert probe_materials(build_fake(connect=False))[0]["shader"] is None


# -- 2. 适配器 -------------------------------------------------------------


def test_adapter_exposes_only_confirmed_safe_params() -> None:
    nodes = material_nodes(build_fake())

    group = nodes[f"material.{MATERIAL}"]
    assert group.supported is True
    assert group.editable is True
    assert group.impact["users"] == ["model_mesh"]
    assert group.impact["slot_count"] == 1

    editable = {
        node_id: node
        for node_id, node in nodes.items()
        if node_id.startswith(f"material.{MATERIAL}.") and node.editable
    }
    assert set(editable) == {
        BASE_COLOR_ID,
        ROUGHNESS_ID,
        EMISSION_STRENGTH_ID,
        f"material.{MATERIAL}.metallic",
        f"material.{MATERIAL}.emission_color",
        f"material.{MATERIAL}.specular",
    }
    # 绑定的三段全部来自探测结果
    assert editable[BASE_COLOR_ID].binding.object_id == "皮肤_脸/Principled BSDF/Base Color"
    assert editable[BASE_COLOR_ID].binding.field == "base_color"
    assert editable[ROUGHNESS_ID].binding.field == "roughness"


def test_adapter_degrades_entirely_when_shader_is_missing() -> None:
    nodes = material_nodes(build_fake(outputs=2))

    for field in ("base_color", "metallic", "roughness", "emission_color", "emission_strength", "specular"):
        node = nodes[f"material.{MATERIAL}.{field}"]
        assert node.supported is False, f"{field} 必须降级"
        assert node.editable is False
        assert node.binding is None, "降级项不得带可写 binding"
        assert "无法唯一确定" in (node.reason or "")


def test_adapter_marks_linked_socket_readonly_with_reason() -> None:
    nodes = material_nodes(build_fake(linked=["Roughness"]))

    node = nodes[ROUGHNESS_ID]
    assert node.supported is True
    assert node.editable is False
    assert node.readonly_reason == "structural"
    assert "已被上游连线" in (node.note or "")
    # 其它未连线的插座照常可编辑
    assert nodes[BASE_COLOR_ID].editable is True


def test_adapter_rejects_material_name_with_path_separator() -> None:
    fake = FakeBpy()
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None
    fake.add_material("a/b", users=[mesh])

    nodes = material_nodes(fake)
    node = nodes["material.a/b.base_color"]
    assert node.editable is False
    assert node.binding is None
    assert "无法安全地拼进参数身份" in (node.reason or "")


def test_adapter_keeps_readonly_classification_and_scope_nodes() -> None:
    nodes = material_nodes(build_fake())

    for suffix in ("classification", "target_group", "users.count"):
        node = nodes[f"material.{MATERIAL}.{suffix}"]
        assert node.editable is False, f"{suffix} 必须只读"
        assert node.binding is None
    assert nodes[f"material.{MATERIAL}.users.count"].value == 1


def test_adapter_uses_repo_rule_source_for_classification() -> None:
    """分类必须来自仓库内唯一规则源，而不是本模块自带的副本。"""
    classify = material_module.load_default_classifier()
    if classify is None:  # pragma: no cover - 规则文件缺失时跳过
        pytest.skip("material_rules.json 不可用")
    result = classify("皮肤_脸")
    assert result["class"] == "skin"
    assert result["group"] == "Cel_Skin"
    assert result["label"] == "皮肤"

    nodes = material_module.build_material_groups(
        {"materials": probe_materials(build_fake())}, classifier=classify
    )
    index = flatten(nodes)
    assert index[f"material.{MATERIAL}.classification"].value == "skin"
    assert index[f"material.{MATERIAL}.target_group"].value == "Cel_Skin"
    assert index[f"material.{MATERIAL}.classification"].editable is False


def test_adapter_survives_classifier_failure() -> None:
    """分类只是展示信息：分类器炸了也不能拖垮参数面。"""

    def broken(_name: str) -> dict[str, Any]:
        raise RuntimeError("规则文件损坏")

    nodes = material_module.build_material_groups(
        {"materials": probe_materials(build_fake())}, classifier=broken
    )
    index = flatten(nodes)
    assert index[f"material.{MATERIAL}.classification"].supported is False
    # 可编辑参数不受影响
    assert index[BASE_COLOR_ID].editable is True


# -- 3. 执行器 -------------------------------------------------------------


def test_executor_writes_material_color_and_scalar() -> None:
    fake = build_fake()
    nodes = material_module.build_material_groups(
        {"materials": probe_materials(fake)}, classifier=None
    )
    index = flatten(nodes)
    draft = {
        BASE_COLOR_ID: [0.1, 0.2, 0.3, 1.0],
        ROUGHNESS_ID: 0.75,
        EMISSION_STRENGTH_ID: 2.5,
    }
    ops = validate_draft(draft, nodes, baseline_values={})
    payload = extract_json(run_generated_code(build_apply_code(ops), fake))

    assert payload["applied"] is True, payload.get("failure")
    assert socket_value(fake, "Principled BSDF", "Base Color") == pytest.approx([0.1, 0.2, 0.3, 1.0])
    assert socket_value(fake, "Principled BSDF", "Roughness") == pytest.approx(0.75)
    assert socket_value(fake, "Principled BSDF", "Emission Strength") == pytest.approx(2.5)
    # 回读值与计划一致（键与 Binding.key() 同构：``类型:对象.字段``）
    assert payload["values"][
        f"MATERIAL:{MATERIAL}/Principled BSDF/Base Color.base_color"
    ] == pytest.approx([0.1, 0.2, 0.3, 1.0])
    assert payload["values"][
        f"MATERIAL:{MATERIAL}/Principled BSDF/Roughness.roughness"
    ] == pytest.approx(0.75)
    assert payload["values"][
        f"MATERIAL:{MATERIAL}/Principled BSDF/Emission Strength.emission_strength"
    ] == pytest.approx(2.5)


def test_executor_refuses_to_write_linked_socket() -> None:
    fake = build_fake(linked=["Roughness"])
    nodes = material_module.build_material_groups(
        {"materials": probe_materials(fake)}, classifier=None
    )
    # 已连线 ⇒ 只读 ⇒ 草稿里出现即拒绝
    with pytest.raises(DraftError) as excinfo:
        validate_draft({ROUGHNESS_ID: 0.9}, nodes, baseline_values={})
    assert excinfo.value.code == "NOT_EDITABLE"


def test_draft_rejects_out_of_range_material_scalar() -> None:
    nodes = material_module.build_material_groups(
        {"materials": probe_materials(build_fake())}, classifier=None
    )
    with pytest.raises(DraftError) as excinfo:
        validate_draft({ROUGHNESS_ID: 1.5}, nodes, baseline_values={})
    assert excinfo.value.code == "PARAM_INVALID"


# -- 4/5/6. 端到端（预览 / 保存 / 结构闸门）--------------------------------


@contextlib.contextmanager
def running_env(tmp_path: Path, **material_kwargs: Any) -> Iterator[SimpleNamespace]:
    fake = build_fake(**material_kwargs)
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        app = create_app(make_config(server.port, presets_dir=tmp_path / "presets"))
        with authed(app) as client:
            yield SimpleNamespace(client=client, app=app, fake=fake, tmp_path=tmp_path)


def take_baseline(env: SimpleNamespace) -> dict:
    body = env.client.post("/api/session/baseline").json()
    assert body["ok"] is True
    _wait_job(env.client, body["job_id"])
    return body


def _wait_job(client, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/v4/jobs/{job_id}").json()
        if body.get("status") in ("done", "failed", "superseded"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内结束：{body}")


def test_v4_schema_lists_materials_with_readonly_scope(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        body = take_baseline(env)
        assert body["surface"]["material_count"] == 1

        schema = env.client.get("/api/v4/surface/schema").json()
        group = next(item for item in schema["groups"] if item["id"] == f"material.{MATERIAL}")
        assert group["label"] == MATERIAL
        assert group["editable"] is True
        assert group["impact"]["users"] == ["model_mesh"]

        by_id = {child["id"]: child for child in group["children"]}
        assert by_id[f"material.{MATERIAL}.classification"]["editable"] is False
        assert by_id[BASE_COLOR_ID]["editable"] is True
        assert by_id[BASE_COLOR_ID]["binding"]["object_type"] == "MATERIAL"


def test_v4_preview_applies_material_draft_then_restores_baseline(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        before = socket_value(env.fake, "Principled BSDF", "Roughness")

        response = env.client.post(
            "/api/v4/preview",
            json={"draft": {ROUGHNESS_ID: 0.9, BASE_COLOR_ID: [0.1, 0.2, 0.3, 1.0]}},
        )
        assert response.status_code == 200, response.text
        job = _wait_job(env.client, response.json()["job_id"])

        assert job["status"] == "done", job.get("error")
        result = job["result"]
        assert result["applied_surface"][ROUGHNESS_ID] == pytest.approx(0.9)
        assert result["applied_surface"][BASE_COLOR_ID] == pytest.approx([0.1, 0.2, 0.3, 1.0])
        assert result["surface_restore_verified"] is True
        assert result["surface_restore_mismatches"] == []

        # 无污染预览：任务结束后材质回到基线
        assert socket_value(env.fake, "Principled BSDF", "Roughness") == pytest.approx(before)
        assert socket_value(env.fake, "Principled BSDF", "Base Color") == pytest.approx(
            [0.8, 0.8, 0.8, 1.0]
        )


def test_commit_saves_material_draft_and_rereads_it(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        body = take_baseline(env)
        structure_hash = body["surface"]["structure_hash"]
        target = env.tmp_path / "out" / "material.blend"
        target.parent.mkdir(parents=True, exist_ok=True)
        surface_draft = {ROUGHNESS_ID: 0.66, BASE_COLOR_ID: [0.2, 0.4, 0.6, 1.0]}

        prepared = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
                "surface_draft": surface_draft,
                "structure_hash": structure_hash,
            },
        )
        assert prepared.status_code == 200, prepared.text

        applied = env.client.post(
            "/api/session/commit",
            json={
                "token": prepared.json()["token"],
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
                "surface_draft": surface_draft,
                "structure_hash": structure_hash,
            },
        )
        assert applied.status_code == 200, applied.text
        result = applied.json()
        assert result["saved"] is True
        assert result["status"]["surface_readback_verified"] is True
        assert result["status"]["surface_mismatches"] == []

        # 保存后重新读取：材质取值与草稿一致
        assert socket_value(env.fake, "Principled BSDF", "Roughness") == pytest.approx(0.66)
        assert socket_value(env.fake, "Principled BSDF", "Base Color") == pytest.approx(
            [0.2, 0.4, 0.6, 1.0]
        )
        refreshed = env.client.get("/api/v4/session/baseline").json()
        assert refreshed["values"][ROUGHNESS_ID] == pytest.approx(0.66)


def test_commit_blocks_when_material_node_structure_changes(tmp_path: Path) -> None:
    """材质节点结构变化 ⇒ STRUCTURE_CHANGED，且**不得**写盘。"""
    from tests.fake_bpy import FakeNode

    with running_env(tmp_path) as env:
        body = take_baseline(env)
        structure_hash = body["surface"]["structure_hash"]
        target = env.tmp_path / "out" / "material.blend"
        target.parent.mkdir(parents=True, exist_ok=True)

        prepared = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
                "surface_draft": {ROUGHNESS_ID: 0.66},
                "structure_hash": structure_hash,
            },
        )
        assert prepared.status_code == 200, prepared.text

        # 结构在确认之后被改：给材质加一个节点
        material = env.fake.data.materials.get(MATERIAL)
        assert material is not None and material.node_tree is not None
        material.node_tree.nodes._by_name["Extra"] = FakeNode("Extra", "MIX_RGB")

        applied = env.client.post(
            "/api/session/commit",
            json={
                "token": prepared.json()["token"],
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
                "surface_draft": {ROUGHNESS_ID: 0.66},
                "structure_hash": structure_hash,
            },
        )
        assert applied.status_code == 409
        assert applied.json()["error"]["code"] == "STRUCTURE_CHANGED"
        assert not target.exists(), "结构变化时绝不允许写盘"


def test_commit_blocks_when_material_renamed(tmp_path: Path) -> None:
    """材质改名 ⇒ IDENTITY_MISSING，且**不得**写盘。"""
    with running_env(tmp_path) as env:
        body = take_baseline(env)
        structure_hash = body["surface"]["structure_hash"]
        target = env.tmp_path / "out" / "material.blend"
        target.parent.mkdir(parents=True, exist_ok=True)

        prepared = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
                "surface_draft": {ROUGHNESS_ID: 0.66},
                "structure_hash": structure_hash,
            },
        )
        assert prepared.status_code == 200, prepared.text

        material = env.fake.data.materials.get(MATERIAL)
        assert material is not None
        material.name = "改过名的材质"

        applied = env.client.post(
            "/api/session/commit",
            json={
                "token": prepared.json()["token"],
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
                "surface_draft": {ROUGHNESS_ID: 0.66},
                "structure_hash": structure_hash,
            },
        )
        assert applied.status_code == 409
        assert applied.json()["error"]["code"] == "IDENTITY_MISSING"
        assert not target.exists()


# -- 7. 隔离预览 -----------------------------------------------------------


def _mesh_visibility(fake: FakeBpy) -> dict[str, bool]:
    """场景里所有网格对象的 ``hide_render``，用于验证「写回原值」。"""
    return {
        obj.name: bool(obj.hide_render)
        for obj in fake.data.objects
        if getattr(obj, "type", "") == "MESH"
    }


def test_framing_options_validate_isolate_material() -> None:
    """隔离材质名走 framing 校验：空 = 不隔离，非法形态一律拒绝。"""
    from src.server import framing
    from src.server.errors import ToonTunerError

    assert framing.validate_options(None)["isolate_material"] is None
    assert framing.validate_options({})["isolate_material"] is None
    # 下拉框「全部材质」回空串 / 纯空白 => 显式归一成「未指定」
    assert framing.validate_options({"isolate_material": ""})["isolate_material"] is None
    assert framing.validate_options({"isolate_material": "   "})["isolate_material"] is None
    # 前后空白被去掉后原样保留
    assert framing.validate_options({"isolate_material": " 皮肤_脸 "})["isolate_material"] == "皮肤_脸"

    for bad in (123, ["a"], {"a": 1}, True):
        with pytest.raises(ToonTunerError) as excinfo:
            framing.validate_options({"isolate_material": bad})
        assert excinfo.value.code == "PARAM_INVALID"
    for bad in ("皮肤/脸", "皮肤\\脸", "x" * 201, "皮肤\x00脸", "皮肤\n脸"):
        with pytest.raises(ToonTunerError) as excinfo:
            framing.validate_options({"isolate_material": bad})
        assert excinfo.value.code == "PARAM_INVALID", bad

    # 未知字段仍然被拒（隔离不能成为放宽 framing 契约的后门）
    with pytest.raises(ToonTunerError) as excinfo:
        framing.validate_options({"isolate_materials": "皮肤_脸"})
    assert excinfo.value.code == "PARAM_INVALID"

    described = framing.describe_options(
        {"mode": "current_camera", "margin": 0.15, "isolate_material": "皮肤_脸"}
    )
    assert described["isolate_material"] == "皮肤_脸"
    assert described["isolates"] is True


def test_v4_preview_isolates_material_and_restores_visibility(tmp_path: Path) -> None:
    """隔离预览：只留使用该材质的对象，渲染后**逐个写回**可见性。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        before = _mesh_visibility(env.fake)
        assert before["model_mesh_outline"] is False, "描边壳默认参与渲染"

        response = env.client.post(
            "/api/v4/preview",
            json={"draft": {}, "framing": {"isolate_material": MATERIAL}},
        )
        assert response.status_code == 200, response.text
        # 提交响应就把隔离选项回显给前端
        assert response.json()["framing"]["isolate_material"] == MATERIAL
        job = _wait_job(env.client, response.json()["job_id"])

        assert job["status"] == "done", job.get("error")
        isolation = job["result"]["isolation"]
        assert isolation["requested"] is True
        assert isolation["material"] == MATERIAL
        assert isolation["applied"] is True
        assert isolation["reason"] is None
        # 使用该材质的对象被保留，其余参与渲染的网格被隐藏
        assert isolation["objects_kept"] == ["model_mesh"]
        assert "model_mesh_outline" in isolation["objects_hidden"]
        # 本来就 hide_render 的刚体代理不进入写入清单（少写一个属性，少一份恢复风险）
        assert not any(name.startswith("000_") for name in isolation["objects_hidden"])
        assert isolation["restored_ok"] is True
        assert isolation["mismatches"] == {}

        # 无污染：任务结束后每个网格的可见性都回到渲染前
        assert _mesh_visibility(env.fake) == before


def test_v4_preview_without_isolation_touches_no_visibility(tmp_path: Path) -> None:
    """不请求隔离时，可见性一个字节都不动（隔离是 opt-in）。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        before = _mesh_visibility(env.fake)

        response = env.client.post("/api/v4/preview", json={"draft": {}})
        assert response.status_code == 200, response.text
        job = _wait_job(env.client, response.json()["job_id"])
        assert job["status"] == "done", job.get("error")

        isolation = job["result"]["isolation"]
        assert isolation["requested"] is False
        assert isolation["applied"] is False
        assert isolation["objects_hidden"] == []
        assert _mesh_visibility(env.fake) == before


def test_v4_preview_aborts_when_isolated_material_missing(tmp_path: Path) -> None:
    """材质不存在 => 中止渲染（不回一张「看起来隔离了、其实没有」的图）。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        before = _mesh_visibility(env.fake)

        response = env.client.post(
            "/api/v4/preview",
            json={"draft": {}, "framing": {"isolate_material": "并不存在的材质"}},
        )
        assert response.status_code == 200, response.text
        job = _wait_job(env.client, response.json()["job_id"])

        assert job["status"] == "failed"
        assert job["error"]["code"] == "ISOLATE_MATERIAL_MISSING"
        # 中止发生在任何写入之后也必须零污染
        assert _mesh_visibility(env.fake) == before
        # 失败任务的 job_id 取不到图（前端据此保留最后一张成功预览）
        assert env.client.get(f"/api/preview/{response.json()['job_id']}.png").status_code == 404


def test_v4_preview_aborts_when_isolated_material_unused(tmp_path: Path) -> None:
    """材质存在但没有任何网格对象在用 => 同样中止（否则画面全空、看着像参数没生效）。"""
    from tests.fake_bpy import FakeMaterial

    with running_env(tmp_path) as env:
        take_baseline(env)
        env.fake.data.materials.add(FakeMaterial("未挂载材质"))

        response = env.client.post(
            "/api/v4/preview",
            json={"draft": {}, "framing": {"isolate_material": "未挂载材质"}},
        )
        assert response.status_code == 200, response.text
        job = _wait_job(env.client, response.json()["job_id"])
        assert job["status"] == "failed"
        assert job["error"]["code"] == "ISOLATE_MATERIAL_MISSING"
