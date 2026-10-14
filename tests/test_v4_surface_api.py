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
    """按「第几次 Cel / 材质图写入」注入失败，用于验证各阶段的失败路径。

    计数从 ``reset()`` 之后开始：基线那一轮的预览任务也在写 L0，不重置的话
    「第 1/2/3 次」到底指哪一次就说不清了 —— 这是上一轮踩过的竞态。
    """

    def __init__(self, fake: FakeBpy) -> None:
        self.fake = fake
        self.cel_writes = 0
        self.l0_writes = 0
        #: 材质图（PR #7）单独计数：它走的是同一条通用执行器，但失败语义要分开验。
        self.mg_writes = 0
        self.fail_cel_write_at: int | None = None
        self.mismatch_cel_readback_at: int | None = None
        self.fail_mg_write_at: int | None = None
        self.mismatch_mg_readback_at: int | None = None
        self.fail_render = False
        #: 置 True 后，**下一次** L0 写入会把桩的 ``is_dirty`` 翻成 True（一次性）。
        self.mark_dirty_on_l0_write = False

    def reset(self) -> None:
        self.cel_writes = 0
        self.l0_writes = 0
        self.mg_writes = 0
        self.fail_cel_write_at = None
        self.mismatch_cel_readback_at = None
        self.fail_mg_write_at = None
        self.mismatch_mg_readback_at = None
        self.fail_render = False
        self.mark_dirty_on_l0_write = False

    @staticmethod
    def _is_material_graph(code: str) -> bool:
        """材质图参数 id 是 opaque 的 ``mg-<hash>``，生成代码里就是 ``_stage = 'mg-...'``。

        比匹配辅助函数名可靠：``_prelude()`` 是两类写入**共用**的，任何按函数名判定
        的做法都会把「前奏里定义了 _mg_*」误当成「这次写的是材质图」。
        """
        return "_stage = 'mg-" in code

    @staticmethod
    def _is_cel(code: str) -> bool:
        # 通用执行器的前奏里有 _ramp_ref；L0 的写入模板没有。
        # 材质图的色带辅助函数是 `_mg_ramp_ref`，**包含** `_ramp_ref` 子串 —— 先排除，
        # 否则材质图的写入会被算成 Cel 的第 N 次，两边的注入计数一起乱。
        if SurfaceInjector._is_material_graph(code):
            return False
        return "_ramp_ref" in code

    def _write_failure(self, stage: str, message: str) -> str:
        return JSON_MARKER + json.dumps(
            {
                "applied": False,
                "failure": {"kind": "write_failed", "stage": stage, "type": "RuntimeError", "message": message},
                "snapshot": {},
                "values": {},
            },
            ensure_ascii=False,
        )

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

        if self._is_material_graph(code):
            self.mg_writes += 1
            if self.fail_mg_write_at is not None and self.mg_writes == self.fail_mg_write_at:
                return self._write_failure("mg-injected", "注入的材质图写入失败")
            stdout = run_generated_code(code, self.fake)
            if (
                self.mismatch_mg_readback_at is not None
                and self.mg_writes == self.mismatch_mg_readback_at
            ):
                payload = json.loads(stdout.split(JSON_MARKER, 1)[1].splitlines()[0])
                values = payload.get("values") or {}
                corrupted = False
                for key, value in values.items():
                    if isinstance(value, float):
                        values[key] = value + 1.0
                        corrupted = True
                        break
                assert corrupted, "注入材质图回读不一致失败：没找到可篡改的标量"
                return JSON_MARKER + json.dumps(payload, ensure_ascii=False)
            return stdout

        if self._is_cel(code):
            self.cel_writes += 1
            if (
                self.fail_cel_write_at is not None
                and self.cel_writes == self.fail_cel_write_at
            ):
                return self._write_failure(RAMP_ID, "注入的色带写入失败")
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
            if self.mark_dirty_on_l0_write:
                # 模拟真实 Blender 的粘性脏标记：写过数据后 `is_dirty` 变 True，
                # 即使事后把值写回原值也不会自动清掉。
                self.fake.data.is_dirty = True
                self.mark_dirty_on_l0_write = False
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


# -- 1b. 工程脏标记（O3）与路径脱敏（O4）----------------------------------


def test_v4_baseline_endpoint_carries_project_dirty_flag(tmp_path: Path) -> None:
    """O3/O4：v4 基线必须带 ``project``，且**只有布尔值与文件名**。

    回归：``SurfaceBaseline.to_public()`` 里没有这个字段，路由必须自己补上 ——
    否则接口文档承诺的 ``project`` 会静默消失（返回体是普通 dict，不会报错）。
    """
    with running_env(tmp_path) as env:
        env.fake.set_project(tmp_path / "scene.blend", dirty=False)
        take_baseline(env)

        body = env.client.get("/api/v4/session/baseline").json()
        assert "project" in body, "v4 基线响应必须带 project"
        project = body["project"]
        assert set(project) <= {"dirty", "file_name"}, project
        assert project["dirty"] is False
        assert project["file_name"] == "scene.blend"
        assert "\\" not in json.dumps(project), "project 不得含任何路径分隔符"


def test_v4_baseline_project_snapshot_absent_without_clean_flag(tmp_path: Path) -> None:
    """桩未打开工程时快照仍要在（值为 None），**不能**整个键消失。"""
    with running_env(tmp_path) as env:
        take_baseline(env)
        project = env.client.get("/api/v4/session/baseline").json()["project"]
        assert set(project) <= {"dirty", "file_name"}
        assert project.get("dirty") in (None, False)


def test_preview_reports_dirty_flagged_when_baseline_was_clean(tmp_path: Path) -> None:
    """O3 端到端：基线干净 → 预览后变脏 ⇒ ``dirty_flagged = true``。

    这是前端弹出「工程未被保存」说明的唯一依据，必须从**任务结果**里真实拿到，
    而不是靠前端自己猜。
    """
    with running_env(tmp_path) as env:
        env.fake.set_project(tmp_path / "scene.blend", dirty=False)
        take_baseline(env)
        assert env.fake.data.is_dirty is False

        # 下一次 L0 写入把桩的 is_dirty 翻成 True（模拟 Blender 的粘性脏标记）
        env.injector.mark_dirty_on_l0_write = True
        job = wait_job(env.client, submit(env, {"color.exposure": 0.35})["job_id"])
        assert job["status"] == "done", job.get("error")

        project = job["result"]["project"]
        assert set(project) <= {
            "dirty_at_baseline",
            "dirty_after_preview",
            "dirty_flagged",
            "file_name",
        }, project
        assert project["dirty_at_baseline"] is False
        assert project["dirty_after_preview"] is True
        assert project["dirty_flagged"] is True
        assert project["file_name"] == "scene.blend"
        assert "\\" not in json.dumps(project)
        # 值本身确实被恢复了 —— 脏标记粘性**不是**因为工具没还原参数
        assert job["result"]["restored"]["color.exposure"] == pytest.approx(0.0)


def test_preview_does_not_flag_dirty_when_baseline_was_already_dirty(tmp_path: Path) -> None:
    """基线本来就脏 ⇒ 不提示。否则用户在编辑途中每点一次预览都被打扰。"""
    with running_env(tmp_path) as env:
        env.fake.set_project(tmp_path / "scene.blend", dirty=True)
        take_baseline(env)

        env.injector.mark_dirty_on_l0_write = True
        job = wait_job(env.client, submit(env, {"color.exposure": 0.35})["job_id"])
        assert job["status"] == "done", job.get("error")

        project = job["result"]["project"]
        assert project["dirty_at_baseline"] is True
        assert project["dirty_flagged"] is False


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


def test_commit_saves_mixed_l0_and_cel_draft_end_to_end(tmp_path: Path) -> None:
    """正向端到端：L0 + Cel **混合草稿**走完 prepare → commit → 应用 → 回读 → 保存。

    与上面几条互补：那些只证明「绑定与拒绝是真的」，本条证明**成功路径**也真的成立 ——
    必须报 ``saved: true``，且保存后**重新读取**工程，取值与完整草稿逐项一致。

    为什么单列一条：``tests/test_commit.py`` 的 31 条用例全部只带 L0 草稿，
    ``surface_draft`` 在那里一次都没出现过；而带 ``surface_draft`` 的用例又只覆盖
    「拒绝」。于是「混合草稿到底能不能存下去」这一条正向链此前**没有任何用例**。
    """
    with running_env(tmp_path) as env:
        body = take_baseline(env)
        structure_hash = body["surface"]["structure_hash"]
        target = env.tmp_path / "out" / "mixed.blend"
        target.parent.mkdir(parents=True, exist_ok=True)

        # L0：曝光（走既有 L0 通道）
        l0_draft = {"color.exposure": 0.35}
        # Cel：色带整体替换（位置 + 颜色 + 插值一起写，才原子）
        elements = [
            {"position": 0.0, "color": [0.10, 0.20, 0.30, 1.0]},
            {"position": 0.5, "color": [0.60, 0.55, 0.50, 1.0]},
            {"position": 1.0, "color": [0.95, 0.93, 0.90, 1.0]},
        ]
        surface_draft = {RAMP_ID: {"elements": elements, "interpolation": "CONSTANT"}}

        prepared = env.client.post(
            "/api/session/commit/prepare",
            json={
                "mode": "save_as",
                "draft": l0_draft,
                "target_path": str(target),
                "surface_draft": surface_draft,
                "structure_hash": structure_hash,
            },
        )
        assert prepared.status_code == 200, prepared.text
        prepared_body = prepared.json()
        assert prepared_body["structure_hash"] == structure_hash
        assert prepared_body["surface_parameter_count"] > 0
        assert prepared_body["token"]

        applied = env.client.post(
            "/api/session/commit",
            json={
                "token": prepared_body["token"],
                "mode": "save_as",
                "draft": l0_draft,
                "target_path": str(target),
                "surface_draft": surface_draft,
                "structure_hash": structure_hash,
            },
        )
        assert applied.status_code == 200, applied.text
        result = applied.json()

        # —— 正向断言：必须报告保存成功 ——
        assert result["saved"] is True
        status = result["status"]
        assert status["applied"] is True
        assert status["readback_verified"] is True
        assert status["mismatches"] == []
        assert status["saved"] is True
        # L0 与 Cel 两条都要通过回读
        assert status["surface_readback_verified"] is True
        assert status["surface_mismatches"] == []
        # 保存成功不得触发回滚
        assert status["rollback"]["attempted"] is False
        # 另存为到新路径：无需备份
        assert status["backup"]["required"] is False
        assert status["backup"]["created"] is False
        # 两条草稿都出现在「应用了什么」里
        assert status["applied_values"]["color.exposure"] == pytest.approx(0.35)
        assert RAMP_ID in status["applied_surface"]
        # 步骤轨迹要能看出 Cel 那一段真的跑了
        assert "应用完整草稿" in result["steps"]
        assert "应用 Cel 草稿" in result["steps"]
        assert "保存工程" in result["steps"]
        assert result["baseline_refreshed"] is True

        # —— 真的写盘 ——
        assert target.is_file()
        assert env.fake.data.filepath == str(target)

        # —— 保存后重新读取：参数与完整草稿一致 ——
        refreshed = env.client.get("/api/v4/session/baseline").json()
        # 色标数量没变 ⇒ 结构指纹不该变（值层改动不进结构哈希）
        assert refreshed["structure_hash"] == structure_hash
        reread = refreshed["values"][RAMP_ID]
        assert [item["position"] for item in reread] == [0.0, 0.5, 1.0]
        for got, want in zip(reread, elements):
            assert list(got["color"]) == pytest.approx(want["color"])

        # 直接问桩：L0 与 Cel 都落在了真实 datablock 上
        assert env.fake.view_settings.exposure == pytest.approx(0.35)
        ramp = env.fake.node_groups[CEL_GROUP].nodes.get("ColorRamp").color_ramp
        assert ramp.interpolation == "CONSTANT"
        assert [round(position, 4) for position, _ in ramp_values(env.fake)] == [0.0, 0.5, 1.0]
        # 元素数不变 ⇒ 写入路径没有偷偷做结构性改动
        assert len(ramp_values(env.fake)) == len(elements)


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
