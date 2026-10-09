"""v4 参数面：Cel 端到端竖切（提交 3A）。

覆盖四件事：

1. **基线** —— 只读拓扑探针 → 递归 schema + 身份 + 结构指纹 + 基线值；
   探不到的组安全降级，但基线仍然成功（L0 与 v4 是两条独立能力）。
2. **预览** —— L0 与 Cel 编进**同一个任务**、只渲染一次；成功路径完整恢复并回读校验。
3. **失效保护** —— 身份缺失 / 结构变化在**任何写入之前**失败；值层外部改动只报告。
4. **失败路径** —— 写入、回读、渲染、恢复各阶段失败注入，都必须恢复两部分基线并如实报告。

所有断言都跑在假 ``bpy`` 桩上：桩**真实执行**服务端生成的代码（不是假响应）。
真实 Blender 验收仍是待办项，本文件不声称它已通过。
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
from src.server.blender_ops import JSON_MARKER
from src.server import surface_probe
from tests.fake_bpy import FakeBpy, run_generated_code
from tests.fake_mcp_server import FakeMCPServer
from tests.support import authed, make_config, unauthed

CEL_GROUP = "Cel_Skin"
RAMP_ID = "cel.Cel_Skin.ramp"


# -- 桩工具 ----------------------------------------------------------------


class SurfaceInjector:
    """按「第几次 Cel 写入」注入失败，用于验证各阶段的失败路径。

    计数从 ``reset()`` 之后开始：基线那一轮的预览任务也在写 L0，不重置的话
    「第 1/2/3 次」到底指哪一次就说不清了 —— 这是上一轮踩过的竞态。
    """

    def __init__(self, fake: FakeBpy) -> None:
        self.fake = fake
        self.cel_writes = 0
        self.l0_writes = 0
        self.fail_cel_write_at: int | None = None
        self.mismatch_cel_readback_at: int | None = None
        self.fail_render = False

    def reset(self) -> None:
        self.cel_writes = 0
        self.l0_writes = 0
        self.fail_cel_write_at = None
        self.mismatch_cel_readback_at = None
        self.fail_render = False

    @staticmethod
    def _is_cel(code: str) -> bool:
        # 通用执行器的前奏里有 _ramp_ref；L0 的写入模板没有。
        return "_ramp_ref" in code

    def __call__(self, code: str) -> str:
        if "bpy.ops.render.render" in code:
            if self.fail_render:
                return JSON_MARKER + json.dumps(
                    {
                        "rendered": False,
                        "path": "",
                        "size_bytes": 0,
                        "render_resolution": [540, 990, 100],
                        "restored_resolution": [1080, 1980, 100],
                        "frame": 1,
                    },
                    ensure_ascii=False,
                )
            return run_generated_code(code, self.fake)

        if self._is_cel(code):
            self.cel_writes += 1
            if (
                self.fail_cel_write_at is not None
                and self.cel_writes == self.fail_cel_write_at
            ):
                return JSON_MARKER + json.dumps(
                    {
                        "applied": False,
                        "failure": {
                            "kind": "write_failed",
                            "stage": RAMP_ID,
                            "type": "RuntimeError",
                            "message": "注入的色带写入失败",
                        },
                        "snapshot": {},
                        "values": {},
                    },
                    ensure_ascii=False,
                )
            stdout = run_generated_code(code, self.fake)
            if (
                self.mismatch_cel_readback_at is not None
                and self.cel_writes == self.mismatch_cel_readback_at
            ):
                payload = json.loads(stdout.split(JSON_MARKER, 1)[1].splitlines()[0])
                values = payload.get("values") or {}
                corrupted = False
                for key, value in values.items():
                    if isinstance(value, list) and value and isinstance(value[0], dict):
                        value[0] = {"position": 0.99, "color": [0.0, 0.0, 0.0, 1.0]}
                        corrupted = True
                        break
                assert corrupted, "注入回读不一致失败：没找到可篡改的复合值"
                return JSON_MARKER + json.dumps(payload, ensure_ascii=False)
            return stdout

        if "vs.exposure = " in code:
            self.l0_writes += 1
        return run_generated_code(code, self.fake)


@contextlib.contextmanager
def running_env(
    tmp_path: Path, *, cel_groups: tuple[tuple[str, int, str], ...] = ((CEL_GROUP, 3, "LINEAR"),)
) -> Iterator[SimpleNamespace]:
    fake = FakeBpy()
    for name, count, interpolation in cel_groups:
        fake.add_cel_group(name, element_count=count, interpolation=interpolation)
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


def wait_job(client, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/v4/jobs/{job_id}").json()
        if body.get("status") in ("done", "failed", "superseded"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内结束：{body}")


def take_baseline(env: SimpleNamespace) -> dict:
    """建立基线并**等它那一轮预览跑完**，再把注入计数清零。"""
    body = env.client.post("/api/session/baseline").json()
    assert body["ok"] is True
    wait_job(env.client, body["job_id"])
    env.injector.reset()
    return body


def base_elements(fake: FakeBpy, name: str = CEL_GROUP) -> list[dict[str, Any]]:
    ramp = fake.node_groups[name].nodes.get("ColorRamp").color_ramp
    return [
        {"position": element.position, "color": list(element.color)} for element in ramp.elements
    ]


def ramp_values(fake: FakeBpy, name: str = CEL_GROUP) -> list[tuple[float, tuple[float, ...]]]:
    ramp = fake.node_groups[name].nodes.get("ColorRamp").color_ramp
    return [(element.position, tuple(element.color)) for element in ramp.elements]


def submit(env: SimpleNamespace, draft: dict, **extra) -> dict:
    payload = {"draft": draft, **extra}
    response = env.client.post("/api/v4/preview", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# -- 1. 基线 ---------------------------------------------------------------


def test_baseline_carries_surface_schema_identity_and_fingerprint(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        body = take_baseline(env)

        surface = body["surface"]
        assert surface["available"] is True
        assert surface["schema_version"] == "toon-surface/2"
        assert surface["structure_hash"]
        assert surface["found_groups"] == 1
        assert surface["degraded"], "未声明的其它 Cel 组应当出现在降级清单里"
        assert {"object_type": "NODE_GROUP", "name": CEL_GROUP, "source": "AI_Compositor"} in [
            dict(item) for item in surface["identities"]
        ]
        assert RAMP_ID in surface["values"]

        # 旧 L0 契约不变：values / look_map / preview_resolution 三件都在
        assert body["values"]["color.exposure"] == pytest.approx(0.0)
        assert body["preview_resolution"]


def test_v4_schema_exposes_writable_ramp_and_readonly_display_nodes(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        schema = env.client.get("/api/v4/surface/schema").json()

        assert schema["ok"] is True
        assert schema["schema_version"] == "toon-surface/2"
        # 色带的取值是 L1（只更新草稿 + 用户触发预览），不是结构性 L3
        assert schema["highest_cost"] == "L1"

        group = [item for item in schema["groups"] if item["label"] == CEL_GROUP][0]
        ramp = [child for child in group["children"] if child["id"] == RAMP_ID][0]
        assert ramp["kind"] == "ramp"
        assert ramp["editable"] is True
        assert len(ramp["elements"]) == 3
        assert ramp["interpolation"]["value"] == "LINEAR"
        # 色标数量是结构信息：只读，并且被明确标为 L3
        assert ramp["element_count"]["structural"] is True
        assert ramp["element_count"]["editable"] is False
        assert ramp["element_count"]["readonly_reason"] == "rollback_unavailable"

        # 只读展示项不得带可写 binding（否则「只读」只是文案）
        mode = [child for child in group["children"] if child["id"].endswith(".managed_mode")][0]
        assert mode["editable"] is False
        assert "binding" not in mode


def test_missing_cel_groups_degrade_but_baseline_still_succeeds(tmp_path: Path) -> None:
    """一个 Cel 组都探不到时：降级、但基线成功，且不假装支持。"""
    with running_env(tmp_path, cel_groups=()) as env:
        body = take_baseline(env)
        surface = body["surface"]
        assert surface["available"] is True
        assert surface["found_groups"] == 0
        assert CEL_GROUP in surface["degraded"]
        assert surface["values"] == {}

        schema = env.client.get("/api/v4/surface/schema").json()
        group = [item for item in schema["groups"] if item["label"] == CEL_GROUP][0]
        assert group["supported"] is False
        assert group["editable"] is False
        assert group["readonly_reason"] == "not_found"

        # 空草稿的 v4 预览仍然可用（L0 部分照常工作），不当成错误
        job = wait_job(env.client, submit(env, {})["job_id"])
        assert job["status"] == "done", job.get("error")
        assert job["result"]["applied_surface"] == {}


def test_v4_baseline_endpoint_requires_baseline(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        response = env.client.get("/api/v4/session/baseline")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_BASELINE"


# -- 2. 预览：应用与恢复 ---------------------------------------------------


def test_v4_preview_applies_cel_then_restores_baseline(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        before = ramp_values(env.fake)
        target = [
            {"position": 0.0, "color": [0.0, 0.0, 0.0, 1.0]},
            {"position": 0.4, "color": [0.5, 0.5, 0.5, 1.0]},
            {"position": 1.0, "color": [1.0, 1.0, 1.0, 1.0]},
        ]

        job = wait_job(
            env.client,
            submit(env, {RAMP_ID: {"elements": target, "interpolation": "CONSTANT"}})["job_id"],
        )
        assert job["status"] == "done", job.get("error")
        assert job["kind"] == "surface"
        assert job["steps"] == [
            "检查身份与结构",
            "恢复基线",
            "应用完整草稿",
            "回读校验",
            "渲染预览",
            "恢复基线并校验",
        ]

        result = job["result"]
        assert result["applied_surface"][RAMP_ID] == target
        assert result["applied_surface"][f"{RAMP_ID}.interpolation"] == "CONSTANT"
        assert result["restore_verified"] is True
        assert result["surface_restore_verified"] is True
        assert result["surface_restore_mismatches"] == []
        assert result["external_changes"] == []

        # 任务结束后桩里必须回到基线（位置 / 颜色 / 插值都回去）
        assert ramp_values(env.fake) == before
        ramp = env.fake.node_groups[CEL_GROUP].nodes.get("ColorRamp").color_ramp
        assert ramp.interpolation == "LINEAR"

        # 图片可取，且是 PNG 端点
        image = env.client.get(result["preview_url"])
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"


def test_mixed_l0_and_cel_draft_renders_exactly_once(tmp_path: Path) -> None:
    """混合草稿只跑一次：L0 与 Cel 同一任务、同一张图。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        before_renders = env.fake.render_count

        draft = {
            "color.exposure": 1.25,
            RAMP_ID: {"elements": base_elements(env.fake)},
        }
        job = wait_job(env.client, submit(env, draft)["job_id"])
        assert job["status"] == "done", job.get("error")

        assert env.fake.render_count - before_renders == 1, "混合草稿只允许渲染一次"
        assert job["result"]["applied"]["color.exposure"] == pytest.approx(1.25)
        assert RAMP_ID in job["result"]["applied_surface"]
        # Cel 写入次数：恢复基线 1 次 + 应用草稿 1 次 + 恢复基线 1 次
        assert env.injector.cel_writes == 3
        assert env.fake.view_settings.exposure == pytest.approx(0.0)


def test_v4_preview_rejects_unknown_param_id(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        response = env.client.post("/api/v4/preview", json={"draft": {"nope.nope": 1}})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "PARAM_INVALID"


def test_v4_preview_rejects_readonly_display_param(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        response = env.client.post(
            "/api/v4/preview", json={"draft": {f"cel.{CEL_GROUP}.managed_mode": "editable"}}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "NOT_EDITABLE"


def test_v4_preview_requires_session_token(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        response = env.unauthed_client.post("/api/v4/preview", json={"draft": {}})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "SESSION_TOKEN_INVALID"


def test_v4_preview_requires_baseline(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        response = env.client.post("/api/v4/preview", json={"draft": {}})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_BASELINE"


# -- 3. 失效保护 -----------------------------------------------------------


def test_element_count_change_fails_before_any_write(tmp_path: Path) -> None:
    """色标数量变化 ⇒ 结构变化 ⇒ 在**任何写入之前**失败。

    服务端在提交阶段就能识别它（草稿里的数量与基线不符），因此连任务都不产生。
    """
    with running_env(tmp_path) as env:
        take_baseline(env)
        elements = base_elements(env.fake) + [
            {"position": 0.75, "color": [0.2, 0.2, 0.2, 1.0]}
        ]
        response = env.client.post("/api/v4/preview", json={"draft": {RAMP_ID: elements}})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "STRUCTURE_CHANGED"
        assert env.injector.cel_writes == 0
        assert env.injector.l0_writes == 0


def test_engineering_side_structure_change_fails_inside_job_before_write(tmp_path: Path) -> None:
    """工程侧在我们背后增删色标 ⇒ 任务在写入之前失败，且两部分都没被写过。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        before_renders = env.fake.render_count
        ramp = env.fake.node_groups[CEL_GROUP].nodes.get("ColorRamp").color_ramp
        ramp.elements.append(ramp.elements[0].__class__(0.75, (0.2, 0.2, 0.2, 1.0)))

        job = wait_job(env.client, submit(env, {})["job_id"])
        assert job["status"] == "failed"
        assert job["error"]["code"] == "STRUCTURE_CHANGED"
        assert job["error"]["details"]["structure_changed"]
        # 闸门在任何写入之前：既没写 Cel，也没写 L0
        assert env.injector.cel_writes == 0
        assert env.injector.l0_writes == 0
        # 没写过就不该声称「回滚过」
        assert job["error"]["details"]["restore"]["attempted"] is False
        assert "无需回滚" in job["error"]["details"]["restore"]["reason"]
        assert env.fake.render_count == before_renders


def test_group_rename_produces_identity_missing(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        env.fake.node_groups[f"{CEL_GROUP}_renamed"] = env.fake.node_groups.pop(CEL_GROUP)

        job = wait_job(env.client, submit(env, {})["job_id"])
        assert job["status"] == "failed"
        assert job["error"]["code"] == "IDENTITY_MISSING"
        assert job["error"]["details"]["identity_missing"]
        assert "不做猜测迁移" in job["error"]["message"]
        assert env.injector.cel_writes == 0


def test_stale_structure_hash_is_rejected_at_submit(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        response = env.client.post(
            "/api/v4/preview",
            json={"draft": {}, "expected_structure_hash": "0" * 64},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "STRUCTURE_CHANGED"


def test_external_value_change_is_reported_but_not_fatal(tmp_path: Path) -> None:
    """用户在 Blender 里拖了一下色标：只报告，不作废草稿。

    若把它当致命错误，工具会变得完全不可用 —— 这正是三层指纹要分开的原因。
    """
    with running_env(tmp_path) as env:
        baseline = take_baseline(env)
        ramp = env.fake.node_groups[CEL_GROUP].nodes.get("ColorRamp").color_ramp
        ramp.elements[1].color = (0.9, 0.1, 0.1, 1.0)

        job = wait_job(env.client, submit(env, {})["job_id"])
        assert job["status"] == "done", job.get("error")
        changes = job["result"]["external_changes"]
        assert changes, "外部取值改动必须被报告出来"
        assert any("color" in str(item["id"]) for item in changes)
        # 结构指纹没有因为「拖色标」而变化
        assert job["result"]["structure_hash"] == baseline["surface"]["structure_hash"]


def test_next_job_supersedes_previous(tmp_path: Path) -> None:
    """新任务取代旧任务：任务排队与取代机制与 L0 路径**共用同一套**。

    刻意不等基线那一轮预览跑完就连续提交两个 v4 任务 —— 工作线程还在忙时，
    两个任务都在排队，后者取代前者，前者必须落到终态（否则前端会一直轮询）。
    """
    with running_env(tmp_path) as env:
        env.client.post("/api/session/baseline")
        first = submit(env, {RAMP_ID: {"elements": base_elements(env.fake)}})["job_id"]
        second = submit(
            env,
            {
                RAMP_ID: {
                    "elements": [
                        {"position": 0.0, "color": [0.0, 0.0, 0.0, 1.0]},
                        {"position": 0.5, "color": [0.5, 0.5, 0.5, 1.0]},
                        {"position": 1.0, "color": [1.0, 1.0, 1.0, 1.0]},
                    ]
                }
            },
        )["job_id"]

        newest = wait_job(env.client, second)
        assert newest["status"] == "done", newest.get("error")
        assert newest["result"]["applied_surface"][RAMP_ID][1]["color"] == [0.5, 0.5, 0.5, 1.0]

        stale = wait_job(env.client, first)
        assert stale["status"] == "superseded"
        assert stale["superseded"] is True
        assert stale.get("result") is None, "被取代的任务不得携带图片结果"


# -- 4. 失败路径 -----------------------------------------------------------


def test_cel_write_failure_restores_both_baselines(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        before_renders = env.fake.render_count
        before_ramp = ramp_values(env.fake)
        # 第 2 次 Cel 写入 = 应用草稿那一次
        env.injector.fail_cel_write_at = 2

        job = wait_job(env.client, submit(env, {RAMP_ID: {"elements": base_elements(env.fake)}})["job_id"])
        assert job["status"] == "failed"
        assert job["error"]["code"] == "SURFACE_APPLY_FAILED"

        restore = job["error"]["details"]["restore"]
        assert restore["attempted"] is True
        assert restore["l0"]["verified"] is True
        assert restore["cel"]["verified"] is True
        assert restore["verified"] is True
        assert "已恢复到提交前的基线" in job["error"]["message"]
        assert ramp_values(env.fake) == before_ramp
        assert env.fake.render_count == before_renders, "写入失败时不该渲染"


def test_cel_readback_mismatch_blocks_render_and_restores(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        before_renders = env.fake.render_count
        before_ramp = ramp_values(env.fake)
        env.injector.mismatch_cel_readback_at = 2  # 应用草稿那一次

        job = wait_job(env.client, submit(env, {RAMP_ID: {"elements": base_elements(env.fake)}})["job_id"])
        assert job["status"] == "failed"
        error = job["error"]
        assert error["code"] == "APPLY_VERIFY_FAILED"
        assert error["details"]["surface_mismatches"], "必须列出不一致项"
        assert error["details"]["restore"]["cel"]["verified"] is True
        assert ramp_values(env.fake) == before_ramp
        assert env.fake.render_count == before_renders


def test_render_failure_keeps_last_successful_preview(tmp_path: Path) -> None:
    """渲染失败：两部分都恢复，且**失败任务取不到图**（前端据此保留上一张成功图）。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        before_ramp = ramp_values(env.fake)

        good = wait_job(env.client, submit(env, {RAMP_ID: {"elements": base_elements(env.fake)}})["job_id"])
        assert good["status"] == "done", good.get("error")
        good_url = good["result"]["preview_url"]
        assert env.client.get(good_url).status_code == 200

        env.injector.fail_render = True
        failed = wait_job(env.client, submit(env, {RAMP_ID: {"elements": base_elements(env.fake)}})["job_id"])
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "PREVIEW_FAILED"
        assert failed["error"]["details"]["restore"]["verified"] is True
        assert ramp_values(env.fake) == before_ramp

        # 失败任务没有图，也不会把上一张成功图弄坏
        assert env.client.get(f"/api/preview/{failed['job_id']}").status_code == 404
        assert env.client.get(good_url).status_code == 200


def test_restore_failure_is_reported_explicitly(tmp_path: Path) -> None:
    """恢复失败必须直说没恢复成，而不是含糊地说一句「已回滚」。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        # 第 3 次 Cel 写入 = 渲染之后的「恢复基线」那一次
        env.injector.fail_render = True
        env.injector.fail_cel_write_at = 3

        job = wait_job(env.client, submit(env, {RAMP_ID: {"elements": base_elements(env.fake)}})["job_id"])
        assert job["status"] == "failed"
        error = job["error"]
        assert error["code"] == "PREVIEW_FAILED", "顶层错误码保留原始失败原因"
        restore = error["details"]["restore"]
        assert restore["attempted"] is True
        assert restore["verified"] is False
        assert restore["code"] == "ROLLBACK_FAILED"
        assert restore["cel"]["code"] == "ROLLBACK_FAILED"
        assert "ROLLBACK_FAILED" in error["message"]
        assert "请人工确认" in error["message"]
        assert "已恢复到提交前的基线" not in error["message"]


# -- 5. 与保存协议的接线（结构不完整开放，但绑定必须是真的）----------------


def test_commit_prepare_binds_surface_draft_and_structure_hash(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        body = take_baseline(env)
        surface_hash = body["surface"]["structure_hash"]
        target = env.tmp_path / "out" / "demo.blend"
        target.parent.mkdir(parents=True, exist_ok=True)

        elements = base_elements(env.fake)
        prepared = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
                "surface_draft": {RAMP_ID: {"elements": elements, "interpolation": "LINEAR"}},
                "structure_hash": surface_hash,
            },
        ).json()
        assert prepared["ok"] is True
        assert prepared["structure_hash"] == surface_hash
        assert prepared["surface_parameter_count"] > 0


def test_commit_prepare_rejects_stale_structure_hash(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = env.tmp_path / "out" / "demo.blend"
        target.parent.mkdir(parents=True, exist_ok=True)
        response = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
                "surface_draft": {},
                "structure_hash": "0" * 64,
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "STRUCTURE_CHANGED"


def test_commit_rejects_tampered_surface_draft(tmp_path: Path) -> None:
    with running_env(tmp_path) as env:
        body = take_baseline(env)
        target = env.tmp_path / "out" / "demo.blend"
        target.parent.mkdir(parents=True, exist_ok=True)
        elements = base_elements(env.fake)

        prepared = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
                "surface_draft": {RAMP_ID: {"elements": elements, "interpolation": "LINEAR"}},
                "structure_hash": body["surface"]["structure_hash"],
            },
        ).json()

        tampered = [dict(element) for element in elements]
        tampered[0] = {"position": 0.0, "color": [0.9, 0.9, 0.9, 1.0]}
        response = env.client.post(
            "/api/session/commit",
            json={
                "token": prepared["token"],
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
                "surface_draft": {RAMP_ID: {"elements": tampered, "interpolation": "LINEAR"}},
                "structure_hash": body["surface"]["structure_hash"],
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "COMMIT_TOKEN_MISMATCH"
        assert not target.exists()


def test_commit_without_surface_keeps_legacy_behaviour(tmp_path: Path) -> None:
    """旧客户端不传 ``surface_draft`` / ``structure_hash`` 时行为完全不变。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        target = env.tmp_path / "out" / "demo.blend"
        target.parent.mkdir(parents=True, exist_ok=True)

        prepared = env.client.post(
            "/api/session/commit/prepare",
            json={"mode": "save_as", "draft": {}, "target_path": str(target)},
        ).json()
        assert prepared["ok"] is True
        assert prepared.get("structure_hash") is None
        assert prepared.get("surface_parameter_count") in (0, None)

        result = env.client.post(
            "/api/session/commit",
            json={
                "token": prepared["token"],
                "mode": "save_as",
                "draft": {},
                "target_path": str(target),
            },
        ).json()
        assert result["saved"] is True
        assert result["status"]["applied_surface"] in ({}, None)
        assert target.is_file()


def test_all_declared_groups_are_reported(tmp_path: Path) -> None:
    """声明了七个受管组 + 一个参考组 ⇒ 全部出现在 schema 里，探不到的逐个降级。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        schema = env.client.get("/api/v4/surface/schema").json()

        labels = [item["label"] for item in schema["groups"]]
        assert labels == [*surface_probe.MANAGED_NODE_GROUPS, *surface_probe.REFERENCE_NODE_GROUPS]

        by_label = {item["label"]: item for item in schema["groups"]}
        # 参考组恒只读（存在也只看不写）
        reference = by_label["Sakura_Hair_Reference"]
        assert reference["editable"] is False
        assert reference["readonly_reason"] == "not_found"
        # 探不到的受管组降级，而不是假装支持
        assert by_label[CEL_GROUP]["supported"] is True
        assert by_label[CEL_GROUP]["editable"] is True
        missing = [item["label"] for item in schema["groups"] if not item["supported"]]
        assert CEL_GROUP not in missing
        assert set(schema["degraded"]) == set(missing)
