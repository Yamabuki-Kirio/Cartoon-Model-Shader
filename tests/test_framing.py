"""取景（framing）测试：构图诊断 + 临时自动取景。

对应需求里的 9 条验收：
1. 当前相机模式不改变相机和当前帧
2. 自动取景后角色包围盒完整进入画面
3. 预览结束恢复原 ``scene.camera``
4. 渲染失败也恢复
5. 用户切帧后检测到基线失效
6. 相机有关键帧时正确显示警告
（另含：只读诊断、离原点取景、纵横比、选项校验、前端契约）

这些测试**真的执行服务端生成的 Python 代码**（``bpy`` 换成桩），
因此能抓出生成代码里的数学/恢复逻辑错误。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.server import blender_ops, errors, framing
from src.server.app import create_app
from src.server.binder import BlenderBinder, preview_resolution_for
from src.server.blender_ops import JSON_MARKER
from src.server.config import AppConfig, BlenderMCPConfig, ServerConfig
from src.server.session import PreviewService
from tests.fake_bpy import (
    CHARACTER_LOCAL_MAX,
    CHARACTER_LOCAL_MIN,
    PNG_BYTES,
    FakeBpy,
    FakeImage,
    extract_marker,
    run_generated_code,
)
from tests.fake_mcp_server import FakeMCPServer
from tests.support import authed

RENDER_MARKER = JSON_MARKER


# =============================================================================
#  工具
# =============================================================================


def run_context(fake: FakeBpy) -> dict:
    """跑一遍只读取景探针并返回归一化上下文。"""
    raw = run_generated_code(framing.build_context_code(), fake)
    return framing.normalize_context(framing.parse_context(raw))


def run_render(fake: FakeBpy, tmp_path, mode: str, margin: float = 0.15, expected: dict | None = None) -> dict:
    target = tmp_path / f"preview_{mode}.png"
    code = framing.build_render_code(
        target.as_posix(), 540, 990, 100, {"mode": mode, "margin": margin}, expected
    )
    return extract_marker(run_generated_code(code, fake), RENDER_MARKER)


def _binder(server: FakeMCPServer) -> BlenderBinder:
    return BlenderBinder(BlenderMCPConfig(host=server.host, port=server.port))


def _wait(service: PreviewService, job_id: str, timeout: float = 5.0):
    async def _poll():
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            job = service.get_job(job_id)
            if job is not None and job.status in ("done", "failed", "superseded"):
                return job
            await asyncio.sleep(0.01)
        return service.get_job(job_id)

    return _poll


def make_app(server: FakeMCPServer) -> TestClient:
    config = AppConfig(
        blender_mcp=BlenderMCPConfig(
            host=server.host,
            port=server.port,
            connect_timeout_seconds=1.0,
            response_timeout_seconds=5.0,
        ),
        server=ServerConfig(),
    )
    return create_app(config)


def test_write_endpoints_reject_missing_token_in_framing_suite() -> None:
    """取景侧的写接口同样受会话令牌保护（缺令牌 401，且不产生任何任务）。"""
    from tests.support import unauthed

    fake = FakeBpy()
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        app = make_app(server)
        client = unauthed(app)
        for path, payload in (
            ("/api/session/baseline", {"framing": {"mode": "current_camera"}}),
            ("/api/preview", {"draft": {"color.exposure": 1.0}}),
            ("/api/session/restore", None),
            ("/api/color/looks/refresh", None),
        ):
            response = client.post(path, json=payload) if payload is not None else client.post(path)
            assert response.status_code == 401, path
            assert response.json()["error"]["code"] == "SESSION_TOKEN_INVALID"


def wait_job(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body.get("status") in ("done", "failed", "superseded"):
            return body
        time.sleep(0.05)
    return body


@pytest.fixture()
def project_fake() -> FakeBpy:
    """模拟现场工程：长焦 + 大 shift 的带动画相机，角色出画。"""
    fake = FakeBpy()
    fake.use_project_camera(frame=217)
    return fake


@pytest.fixture()
def tuner_client(project_fake: FakeBpy):
    with FakeMCPServer(executor=lambda code: run_generated_code(code, project_fake)) as server:
        with authed(make_app(server)) as client:
            yield client, project_fake


# =============================================================================
#  1. 只读诊断
# =============================================================================


def test_context_reports_frame_camera_and_character(project_fake: FakeBpy) -> None:
    context = run_context(project_fake)
    assert context["frame_current"] == 217
    assert context["frame_start"] == 0 and context["frame_end"] == 741
    assert context["camera"] == "Duo_Vertical_MMD_Camera"
    assert context["resolution"] == [1080, 1980, 50]

    info = context["camera_info"]
    assert info["lens"] == pytest.approx(68.49658966064453)
    assert info["shift_x"] == pytest.approx(0.4)
    assert info["shift_y"] == pytest.approx(-0.16)
    assert info["type"] == "PERSP"

    character = context["character"]
    assert character["group"] == "model_arm"
    assert [o["name"] for o in character["objects"]] == ["model_mesh"]
    # 刚体代理与描边壳必须被排除
    assert character["excluded"]["rigid_or_shell"] == 4
    bounds = character["bounds"]
    assert bounds["min"][0] == pytest.approx(CHARACTER_LOCAL_MIN[0])
    assert bounds["max"][2] == pytest.approx(CHARACTER_LOCAL_MAX[2])
    # 角色世界变换必须被记录（基线要求）
    assert character["objects"][0]["matrix_world"][3][3] == pytest.approx(1.0)


def test_context_detects_character_out_of_frame(project_fake: FakeBpy) -> None:
    """现场那台相机确实把角色放到了画面外（本次要修的问题）。"""
    fit = run_context(project_fake)["fit"]
    assert fit["camera"] == "Duo_Vertical_MMD_Camera"
    assert fit["inside"] is False
    assert fit["full_inside"] is False
    assert fit["reason"] in ("outside_frame", "behind_camera", "clipped_near")
    assert fit["worst_ndc"] > 1.0
    assert "超出画面" in fit["message"] or "背后" in fit["message"]


def test_context_reports_tidy_frame_for_default_camera() -> None:
    fit = run_context(FakeBpy())["fit"]
    assert fit["inside"] is True
    assert fit["reason"] == "ok"
    assert fit["worst_ndc"] <= 1.0


def test_view_frame_is_not_at_unit_distance() -> None:
    """回归护栏：Blender 的 view_frame 返回点不在单位距离上。

    如果实现里忘掉「先按 z 归一化」，shift 会被放大成永远装不进画面。
    这里把陷阱显式钉住：桩与真实 Blender 一样返回半高 0.5、z≈-2.854。
    """
    fake = FakeBpy()
    camera = fake.use_project_camera()
    corners = camera.data.view_frame(scene=fake.scene)
    assert all(c[2] < -1.0 for c in corners), "view_frame 的 z 不应是 -1"
    half_h = (max(c[1] for c in corners) - min(c[1] for c in corners)) / 2.0
    assert half_h == pytest.approx(0.5)
    depth = -corners[0][2]
    # 归一化之后才是真正的 tan(vfov/2)
    assert half_h / depth == pytest.approx(24.0 / 2.0 / 68.49658966064453)


def test_fit_reports_no_camera_and_no_character() -> None:
    fake = FakeBpy()
    fake.scene.camera = None
    fit = run_context(fake)["fit"]
    assert fit["inside"] is None and fit["reason"] == "no_camera"

    fake2 = FakeBpy()
    for obj in list(fake2.scene.objects):
        if obj.type == "MESH":
            obj.hide_render = True
    fit2 = run_context(fake2)["fit"]
    assert fit2["inside"] is None and fit2["reason"] == "no_character"


def test_camera_animation_is_reported(project_fake: FakeBpy) -> None:
    """要求 6：相机有关键帧时必须能被识别出来。"""
    animation = run_context(project_fake)["camera_info"]["animation"]
    assert animation["has_animation"] is True
    assert set(animation["sources"]) == {"object.action", "data.action"}
    assert animation["fcurves"] == 6
    assert animation["action"] == "Duo_Vertical_MMD_Camera动作.001"

    plain = FakeBpy()
    assert run_context(plain)["camera_info"]["animation"]["has_animation"] is False


# =============================================================================
#  2. 纯函数：选项校验 / 基线比对 / 预览分辨率
# =============================================================================


def test_validate_options_rejects_bad_input() -> None:
    assert framing.validate_options(None) == {"mode": "current_camera", "margin": 0.15}
    assert framing.validate_options({"mode": "auto_headshot", "margin": 0.1}) == {
        "mode": "auto_headshot",
        "margin": 0.1,
    }
    for bad in (
        {"mode": "auto_full"},
        {"mode": "current"},
        {"mode": 3},
        {"margin": -0.1},
        {"margin": 0.5},
        {"margin": "big"},
        {"margin": True},
        {"evil": 1},
        "not-a-dict",
    ):
        with pytest.raises(errors.ToonTunerError) as exc:
            framing.validate_options(bad)
        assert exc.value.code == errors.PARAM_INVALID, bad


def test_describe_options_marks_temporary_camera() -> None:
    assert framing.describe_options({"mode": "current_camera", "margin": 0.15})["uses_temporary_camera"] is False
    described = framing.describe_options({"mode": "auto_upper_body", "margin": 0.2})
    assert described["uses_temporary_camera"] is True
    assert described["mode_label"] == "自动半身"


def test_compare_context_flags_stale_only_for_frame_or_camera() -> None:
    fake = FakeBpy()
    fake.use_project_camera(frame=217)
    baseline = framing.baseline_snapshot(run_context(fake))

    assert framing.compare_context(baseline, run_context(fake))["stale"] is False

    # 切帧 -> 硬失效
    fake.scene.frame_current = 652
    verdict = framing.compare_context(baseline, run_context(fake))
    assert verdict["stale"] is True
    assert [r["code"] for r in verdict["reasons"]] == ["frame_changed"]
    assert "217" in verdict["reasons"][0]["message"] and "652" in verdict["reasons"][0]["message"]

    # 换相机 -> 硬失效
    fake.scene.frame_current = 217
    fake.scene.camera = next(o for o in fake.scene.objects if o.name == "Camera")
    verdict = framing.compare_context(baseline, run_context(fake))
    assert verdict["stale"] is True
    assert "camera_changed" in [r["code"] for r in verdict["reasons"]]


def test_compare_context_flags_camera_moved_and_lens_changed() -> None:
    fake = FakeBpy()
    camera = fake.use_project_camera(frame=217)
    baseline = framing.baseline_snapshot(run_context(fake))

    camera.location = (camera.location[0] + 0.5, camera.location[1], camera.location[2])
    verdict = framing.compare_context(baseline, run_context(fake))
    assert verdict["stale"] is True
    assert "camera_moved" in [r["code"] for r in verdict["reasons"]]

    camera.location = (-0.2975170314311981, -1.2671719789505005, 1.3735994100570679)
    camera.data.lens = 85.0
    verdict = framing.compare_context(baseline, run_context(fake))
    assert "lens_changed" in [r["code"] for r in verdict["reasons"]]

    camera.data.lens = 68.49658966064453
    camera.data.shift_x = 0.0
    verdict = framing.compare_context(baseline, run_context(fake))
    assert "shift_changed" in [r["code"] for r in verdict["reasons"]]


def test_compare_context_character_drift_is_only_a_warning() -> None:
    """角色包围盒变化只告警：需求只要求「帧/相机变化」硬停。"""
    fake = FakeBpy()
    fake.use_project_camera(frame=217)
    baseline = framing.baseline_snapshot(run_context(fake))

    mesh = next(o for o in fake.scene.objects if o.name == "model_mesh")
    mesh.data._bbox = ((CHARACTER_LOCAL_MIN[0] - 1.0, CHARACTER_LOCAL_MIN[1], CHARACTER_LOCAL_MIN[2]),
                       (CHARACTER_LOCAL_MAX[0] + 1.0, CHARACTER_LOCAL_MAX[1], CHARACTER_LOCAL_MAX[2]))
    verdict = framing.compare_context(baseline, run_context(fake))
    assert verdict["stale"] is False
    assert "character_moved" in [w["code"] for w in verdict["warnings"]]


def test_compare_context_warns_when_camera_has_keyframes() -> None:
    """要求 6：带关键帧的相机要给出明确告警（而不是静默）。"""
    fake = FakeBpy()
    fake.use_project_camera(frame=217)
    verdict = framing.compare_context(framing.baseline_snapshot(run_context(fake)), run_context(fake))
    codes = [w["code"] for w in verdict["warnings"]]
    assert "camera_animated" in codes
    message = next(w["message"] for w in verdict["warnings"] if w["code"] == "camera_animated")
    assert "动画" in message and "帧一旦变化基线即失效" in message
    assert verdict["stale"] is False


def test_compare_context_without_baseline_is_not_stale() -> None:
    fake = FakeBpy()
    verdict = framing.compare_context(None, run_context(fake))
    assert verdict == {"stale": False, "reasons": [], "warnings": [], "checked": False}


def test_preview_resolution_keeps_aspect_ratio() -> None:
    assert preview_resolution_for({"resolution_x": 1080, "resolution_y": 1980, "resolution_percentage": 50}) == (540, 990, 100)
    assert preview_resolution_for({"resolution_x": 1920, "resolution_y": 1080, "resolution_percentage": 100}) == (990, 557, 100)
    assert preview_resolution_for({"resolution_x": 1000, "resolution_y": 1600, "resolution_percentage": 100}) == (619, 990, 100)
    assert preview_resolution_for(None) == (540, 990, 100)
    for render in (
        {"resolution_x": 1080, "resolution_y": 1980, "resolution_percentage": 50},
        {"resolution_x": 3840, "resolution_y": 2160, "resolution_percentage": 100},
    ):
        width, height, _ = preview_resolution_for(render)
        expected = render["resolution_x"] / render["resolution_y"]
        assert width / height == pytest.approx(expected, rel=2e-3)


def test_generated_code_has_no_arbitrary_python_input() -> None:
    """取景代码同样是服务端常量拼接；取景方式必须来自白名单。"""
    code = framing.build_render_code("/tmp/x.png", 540, 990, 100, {"mode": "current_camera", "margin": 0.0})
    for forbidden in ("os.system", "subprocess", "eval(", "exec(", "__import__"):
        assert forbidden not in code, f"取景代码不应包含 {forbidden!r}"

    evil = "__TEMP_CAM__')\nimport os\nos.system('boom')\n#"
    with pytest.raises(errors.ToonTunerError) as exc:
        framing.build_render_code("/tmp/x.png", 540, 990, 100, {"mode": evil, "margin": 0.15})
    assert exc.value.code == errors.PARAM_INVALID


# =============================================================================
#  3. 要求 1 / 2 / 3：当前相机不改动；自动取景把角色装进画面；恢复原相机
# =============================================================================


def test_current_camera_mode_keeps_camera_and_frame(project_fake: FakeBpy, tmp_path) -> None:
    """要求 1：当前相机模式不改变相机、不改变当前帧、不建临时相机。"""
    before = project_fake.snapshot_framing()
    payload = run_render(project_fake, tmp_path, "current_camera")

    assert payload["rendered"] is True
    assert payload["camera_used"] == "Duo_Vertical_MMD_Camera"
    assert payload["framing"]["temporary"] is False
    assert payload["camera_restored"] is True
    assert payload["temporary_camera_leftovers"] == 0
    assert project_fake.temp_camera_objects() == []
    assert project_fake.temp_camera_datablocks() == []

    after = project_fake.snapshot_framing()
    assert after == before, "当前相机模式下相机/帧/分辨率必须原样不动"
    assert after["frame"] == 217
    assert payload["frame"] == 217 and payload["frame_after"] == 217
    # 顺带给出「角色出画」的诊断（这正是本次要暴露的事实）
    assert payload["framing"]["fit"]["inside"] is False


def test_auto_full_body_puts_whole_character_in_frame(project_fake: FakeBpy, tmp_path) -> None:
    """要求 2：自动全身取景后，角色包围盒完整进入画面（并留出安全边距）。"""
    payload = run_render(project_fake, tmp_path, "auto_full_body", margin=0.15)
    fit = payload["framing"]["fit"]

    assert payload["rendered"] is True
    assert fit["inside"] is True
    assert fit["full_inside"] is True
    assert fit["reason"] == "ok"
    assert fit["worst_ndc"] <= 1.0 / (1.0 + 0.15) + 1e-6
    assert fit["worst_ndc"] == pytest.approx(1.0 / 1.15, rel=1e-6)
    assert fit["overflow"] == {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}

    # 用的是临时相机，且渲染真的走了临时相机
    assert payload["framing"]["temporary"] is True
    assert payload["camera_used"] == framing.TEMP_CAMERA_NAME
    assert project_fake.last_render_camera == framing.TEMP_CAMERA_NAME


@pytest.mark.parametrize("mode", ["auto_upper_body", "auto_headshot"])
def test_auto_region_modes_fit_their_region(project_fake: FakeBpy, tmp_path, mode: str) -> None:
    payload = run_render(project_fake, tmp_path, mode, margin=0.15)
    fit = payload["framing"]["fit"]
    assert fit["inside"] is True
    assert fit["worst_ndc"] <= 1.0 / 1.15 + 1e-6
    # 半身/头像只保证「取景区域」入画，整角色当然会超出画面
    assert fit["full_inside"] is False
    assert payload["framing"]["temporary"] is True


def test_margin_controls_tightness(project_fake: FakeBpy, tmp_path) -> None:
    """安全边距越大，机位越远、画面越松。"""
    tight = run_render(project_fake, tmp_path, "auto_full_body", margin=0.0)
    loose = run_render(project_fake, tmp_path, "auto_full_body", margin=0.35)
    assert tight["framing"]["fit"]["worst_ndc"] > loose["framing"]["fit"]["worst_ndc"]
    assert loose["framing"]["distance"] > tight["framing"]["distance"]
    assert loose["framing"]["fit"]["inside"] is True


def test_auto_full_body_handles_model_off_origin(project_fake: FakeBpy, tmp_path) -> None:
    """要求 6：模型不在世界原点时同样要把角色装进画面（不能假设 y 在 0）。"""
    mesh = next(o for o in project_fake.scene.objects if o.name == "model_mesh")
    mesh.location = (5.0, -3.0, 1.25)
    payload = run_render(project_fake, tmp_path, "auto_full_body")
    fit = payload["framing"]["fit"]
    assert fit["inside"] is True and fit["full_inside"] is True
    # 渲染确实走了临时相机
    assert project_fake.last_render_camera == framing.TEMP_CAMERA_NAME
    assert payload["framing"]["distance"] > 0.0
    # 机位必须跟着角色走：临时相机与角色的水平距离应保持在原处附近
    assert payload["framing"]["distance"] == pytest.approx(5.3377, rel=1e-2)


def test_auto_full_body_ignores_rigid_proxies_and_outline_shell(project_fake: FakeBpy, tmp_path) -> None:
    """要求 6：刚体代理与描边壳不得撑大取景范围。"""
    character = run_context(project_fake)["character"]
    names = sorted(n for g in character["groups"] for n in g["objects"])
    assert names == ["model_mesh"]
    assert character["excluded"]["rigid_or_shell"] >= 4

    # 把刚体代理与描边壳挪到很远、做得很大：画面不应因此被拉远
    for obj in project_fake.scene.objects:
        if obj.name != "model_mesh" and obj.type == "MESH":
            obj.location = (0.0, 0.0, 400.0)
            obj.hide_render = False
            obj.hide_viewport = False
            if obj.data._bbox is not None:
                obj.data._bbox = ((-50.0, -50.0, -50.0), (50.0, 50.0, 50.0))
    payload = run_render(project_fake, tmp_path, "auto_full_body")
    assert payload["framing"]["fit"]["full_inside"] is True
    assert payload["framing"]["distance"] < 10.0


def test_preview_restores_scene_camera_after_auto_framing(project_fake: FakeBpy, tmp_path) -> None:
    """要求 3：预览结束后恢复原 ``scene.camera``，并清理临时相机数据块。"""
    original = project_fake.scene.camera
    original_state = project_fake.snapshot_framing()

    payload = run_render(project_fake, tmp_path, "auto_headshot", margin=0.1)

    assert payload["camera_restored"] is True
    assert project_fake.scene.camera is original
    assert project_fake.scene.camera.name == "Duo_Vertical_MMD_Camera"
    assert project_fake.temp_camera_objects() == []
    assert project_fake.temp_camera_datablocks() == []
    assert payload["temporary_camera_leftovers"] == 0
    # 原相机本体（位置/朝向/焦距/shift）与帧、分辨率都不得被动过
    assert project_fake.snapshot_framing() == original_state


def test_render_failure_still_restores_camera(project_fake: FakeBpy, tmp_path, monkeypatch) -> None:
    """要求 4：渲染失败也必须在 finally 里恢复原相机并清掉临时相机。"""
    original = project_fake.scene.camera
    original_state = project_fake.snapshot_framing()

    def boom(*_args, **_kwargs):
        raise RuntimeError("gpu exploded")

    monkeypatch.setattr(FakeImage, "save_render", boom, raising=True)
    with pytest.raises(RuntimeError):
        run_generated_code(
            framing.build_render_code(
                (tmp_path / "boom.png").as_posix(), 540, 990, 100,
                {"mode": "auto_full_body", "margin": 0.15}, None,
            ),
            project_fake,
        )

    assert project_fake.scene.camera is original
    assert project_fake.temp_camera_objects() == []
    assert project_fake.temp_camera_datablocks() == []
    assert project_fake.snapshot_framing() == original_state


def test_stale_guard_aborts_before_render(project_fake: FakeBpy, tmp_path) -> None:
    """兜底闸门：期望帧与现场不符时，Blender 侧直接中止且不渲染。"""
    payload = run_render(
        project_fake, tmp_path, "auto_full_body",
        expected={"frame_current": 218, "camera": "Duo_Vertical_MMD_Camera"},
    )
    assert payload["rendered"] is False
    assert payload["aborted"]["code"] == "FRAMING_STALE"
    assert "218" in payload["aborted"]["message"]
    assert project_fake.render_count == 0
    assert project_fake.scene.camera.name == "Duo_Vertical_MMD_Camera"
    assert project_fake.temp_camera_objects() == []

    payload = run_render(
        project_fake, tmp_path, "auto_full_body",
        expected={"frame_current": 217, "camera": "Camera"},
    )
    assert payload["aborted"]["code"] == "FRAMING_STALE"
    assert project_fake.render_count == 0


# =============================================================================
#  4. 会话层：基线快照 / 取景闸门 / 任务结果
# =============================================================================


def test_service_baseline_records_framing_snapshot(project_fake: FakeBpy) -> None:
    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, project_fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                baseline = await service.capture_baseline()
                snapshot = baseline["framing"]
                assert snapshot["frame_current"] == 217
                assert snapshot["camera"] == "Duo_Vertical_MMD_Camera"
                assert snapshot["lens"] == pytest.approx(68.49658966064453)
                assert snapshot["shift_x"] == pytest.approx(0.4)
                assert snapshot["shift_y"] == pytest.approx(-0.16)
                assert snapshot["camera_transform"]["location"][0] == pytest.approx(-0.2975, abs=1e-3)
                assert snapshot["camera_animation"]["has_animation"] is True
                assert snapshot["character_group"] == "model_arm"
                assert snapshot["character_objects"][0]["name"] == "model_mesh"
                assert snapshot["character_objects"][0]["matrix_world"][3][3] == pytest.approx(1.0)
                assert snapshot["character_bounds"]["max"][2] == pytest.approx(CHARACTER_LOCAL_MAX[2])
                # 预览分辨率按工程分辨率等比缩小（保持纵横比）
                assert baseline["preview_resolution"] == [540, 990, 100]
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_service_preview_records_framing_result(project_fake: FakeBpy) -> None:
    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, project_fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                job = await service.submit({"color.exposure": 0.5}, {"mode": "auto_full_body", "margin": 0.15})
                finished = await _wait(service, job.job_id)()
                assert finished.status == "done", finished.error
                framing_result = finished.result["framing"]
                assert framing_result["mode"] == "auto_full_body"
                assert framing_result["temporary_camera"] is True
                assert framing_result["camera_restored"] is True
                assert framing_result["original_camera"] == "Duo_Vertical_MMD_Camera"
                assert framing_result["fit"]["inside"] is True
                assert framing_result["fit"]["full_inside"] is True
                assert framing_result["character"]["group"] == "model_arm"
                # 参数照旧生效 + 回滚校验
                assert finished.result["applied"]["color.exposure"] == pytest.approx(0.5)
                assert finished.result["restore_verified"] is True
                # 预览结束后工程完全还原
                assert project_fake.scene.camera.name == "Duo_Vertical_MMD_Camera"
                assert project_fake.temp_camera_objects() == []
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_service_rejects_preview_after_frame_change(project_fake: FakeBpy) -> None:
    """要求 5：用户切帧后必须检测到基线失效，且不产生任何任务。"""
    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, project_fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                job = await service.submit({})
                assert (await _wait(service, job.job_id)()).status == "done"
                before_render_count = project_fake.render_count

                project_fake.scene.frame_current = 652
                with pytest.raises(errors.ToonTunerError) as exc:
                    await service.submit({"color.exposure": 1.0})
                assert exc.value.code == errors.FRAMING_STALE
                assert "请刷新基线" in exc.value.message
                assert "217" in exc.value.message and "652" in exc.value.message
                # 被拒绝的提交不得触发任何渲染，也不得产生新任务
                assert project_fake.render_count == before_render_count
                assert len([j for j in service._jobs.values()]) == 1  # noqa: SLF001
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_service_rejects_preview_after_camera_swap(project_fake: FakeBpy) -> None:
    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, project_fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                project_fake.scene.camera = next(o for o in project_fake.scene.objects if o.name == "Camera")
                with pytest.raises(errors.ToonTunerError) as exc:
                    await service.submit({})
                assert exc.value.code == errors.FRAMING_STALE
                assert "当前相机" in exc.value.message
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_service_refresh_baseline_clears_stale(project_fake: FakeBpy) -> None:
    """刷新基线后可以继续预览（失效是可恢复的，而不是死锁）。"""
    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, project_fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                project_fake.scene.frame_current = 300
                with pytest.raises(errors.ToonTunerError):
                    await service.submit({})

                await service.capture_baseline()
                job = await service.submit({}, {"mode": "auto_full_body", "margin": 0.15})
                finished = await _wait(service, job.job_id)()
                assert finished.status == "done", finished.error
                assert finished.result["framing"]["fit"]["full_inside"] is True
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_service_current_camera_job_keeps_project_untouched(project_fake: FakeBpy) -> None:
    """要求 1（端到端）：当前相机模式跑完整任务后，相机与帧、分辨率都不变。"""
    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, project_fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                before = project_fake.snapshot_framing()
                await service.capture_baseline()
                job = await service.submit({"color.gamma": 1.2}, {"mode": "current_camera"})
                finished = await _wait(service, job.job_id)()
                assert finished.status == "done", finished.error
                assert finished.result["framing"]["temporary_camera"] is False
                assert project_fake.snapshot_framing() == before
                assert project_fake.temp_camera_objects() == []
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_service_auto_framing_restores_camera_when_render_fails(project_fake: FakeBpy) -> None:
    """要求 4（端到端）：渲染步骤失败时，任务落 failed，但工程必须已还原。"""
    def failing_executor(code: str) -> str:
        if "bpy.ops.render.render" in code:
            # 让 Blender 侧「渲染完成但没有产出文件」
            return RENDER_MARKER + json.dumps(
                {
                    "rendered": False,
                    "aborted": None,
                    "path": "",
                    "size_bytes": 0,
                    "render_resolution": [540, 990, 100],
                    "restored_resolution": [1080, 1980, 50],
                    "frame": 217,
                    "frame_after": 217,
                    "camera_original": "Duo_Vertical_MMD_Camera",
                    "camera_used": framing.TEMP_CAMERA_NAME,
                    "camera_restored": True,
                    "temporary_camera_leftovers": 0,
                    "framing": None,
                },
                ensure_ascii=False,
            )
        return run_generated_code(code, project_fake)

    async def scenario() -> None:
        with FakeMCPServer(executor=failing_executor) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                before = project_fake.snapshot_framing()
                await service.capture_baseline()
                job = await service.submit({}, {"mode": "auto_full_body", "margin": 0.15})
                finished = await _wait(service, job.job_id)()
                assert finished.status == "failed"
                assert finished.error["code"] == "PREVIEW_FAILED"
                assert finished.result is None
                # 失败路径同样必须还原工程
                assert project_fake.snapshot_framing() == before
                assert project_fake.temp_camera_objects() == []
            finally:
                await service.stop()

    asyncio.run(scenario())


# =============================================================================
#  5. API 层
# =============================================================================


def test_api_framing_context_shape(tuner_client) -> None:
    client, fake = tuner_client
    body = client.get("/api/framing/context").json()
    assert body["ok"] is True
    assert body["frame_current"] == 217
    assert body["camera"] == "Duo_Vertical_MMD_Camera"
    assert body["camera_info"]["animation"]["has_animation"] is True
    assert body["fit"]["inside"] is False
    assert body["baseline"]["established"] is False
    assert body["framing_modes"]["default"] == "current_camera"
    assert [m["id"] for m in body["framing_modes"]["modes"]] == [
        "current_camera", "auto_full_body", "auto_upper_body", "auto_headshot"
    ]
    assert body["character"]["group"] == "model_arm"


def test_api_framing_context_reports_baseline_staleness(tuner_client) -> None:
    client, fake = tuner_client
    client.post("/api/session/baseline", json={"framing": {"mode": "current_camera"}})
    body = client.get("/api/framing/context").json()
    assert body["baseline"]["established"] is True
    assert body["baseline"]["stale"] is False
    assert "camera_animated" in [w["code"] for w in body["baseline"]["warnings"]]

    fake.scene.frame_current = 652
    body = client.get("/api/framing/context").json()
    assert body["baseline"]["stale"] is True
    assert body["baseline"]["reasons"][0]["code"] == "frame_changed"
    assert body["frame_current"] == 652


def test_api_baseline_accepts_framing_options(tuner_client) -> None:
    client, fake = tuner_client
    body = client.post(
        "/api/session/baseline",
        json={"framing": {"mode": "auto_full_body", "margin": 0.12}},
    ).json()
    assert body["ok"] is True
    assert body["framing"]["frame_current"] == 217
    job = wait_job(client, body["job_id"])
    assert job["status"] == "done", job.get("error")
    assert job["framing_request"]["mode"] == "auto_full_body"
    assert job["result"]["framing"]["fit"]["full_inside"] is True
    assert job["result"]["framing"]["temporary_camera"] is True
    assert project_camera_restored(fake)


def project_camera_restored(fake: FakeBpy) -> bool:
    return (
        fake.scene.camera is not None
        and fake.scene.camera.name == "Duo_Vertical_MMD_Camera"
        and not fake.temp_camera_objects()
        and not fake.temp_camera_datablocks()
    )


def test_api_preview_honours_framing_mode(tuner_client) -> None:
    client, fake = tuner_client
    client.post("/api/session/baseline")
    submit = client.post(
        "/api/preview",
        json={"draft": {"color.exposure": 0.3}, "framing": {"mode": "auto_headshot", "margin": 0.2}},
    ).json()
    assert submit["ok"] is True
    assert submit["framing"]["mode"] == "auto_headshot"

    job = wait_job(client, submit["job_id"])
    assert job["status"] == "done", job.get("error")
    assert job["result"]["framing"]["mode"] == "auto_headshot"
    assert job["result"]["framing"]["margin"] == 0.2
    assert job["result"]["framing"]["fit"]["inside"] is True
    assert project_camera_restored(fake)
    assert fake.scene.frame_current == 217


def test_api_preview_rejects_bad_framing(tuner_client) -> None:
    client, _ = tuner_client
    client.post("/api/session/baseline")
    for payload in (
        {"draft": {}, "framing": {"mode": "auto_full"}},
        {"draft": {}, "framing": {"mode": "current_camera", "margin": 0.9}},
        {"draft": {}, "framing": {"mode": "current_camera", "margin": "x"}},
        {"draft": {}, "framing": {"mode": "current_camera", "evil": 1}},
    ):
        response = client.post("/api/preview", json=payload)
        assert response.status_code == 422 or response.json()["error"]["code"] == "PARAM_INVALID", payload


def test_api_preview_returns_409_framing_stale(tuner_client) -> None:
    """要求 5（API 层）：切帧后提交预览必须得到稳定的 409 错误码。"""
    client, fake = tuner_client
    client.post("/api/session/baseline")
    fake.scene.frame_current = 652

    response = client.post("/api/preview", json={"draft": {"color.exposure": 1.0}})
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "FRAMING_STALE"
    assert "请刷新基线" in body["error"]["message"]
    assert body["error"]["retryable"] is True
    assert body["error"]["details"]["reasons"][0]["code"] == "frame_changed"

    # 刷新基线后恢复正常
    again = client.post("/api/session/baseline").json()
    assert again["ok"] is True
    job = wait_job(client, again["job_id"])
    assert job["status"] == "done", job.get("error")


def test_api_preview_image_still_served_after_auto_framing(tuner_client) -> None:
    client, _ = tuner_client
    body = client.post("/api/session/baseline", json={"framing": {"mode": "auto_full_body"}}).json()
    job = wait_job(client, body["job_id"])
    image = client.get(job["result"]["preview_url"])
    assert image.status_code == 200
    assert image.content == PNG_BYTES
    assert image.headers["content-type"] == "image/png"


def test_api_job_payload_exposes_no_local_paths(tuner_client) -> None:
    """取景结果里不得夹带本机路径（沿用既有脱敏约束）。"""
    import tempfile

    client, _ = tuner_client
    body = client.post("/api/session/baseline", json={"framing": {"mode": "auto_full_body"}}).json()
    job = wait_job(client, body["job_id"])
    dumped = json.dumps(job, ensure_ascii=False)
    assert str(tempfile.gettempdir()) not in dumped
    assert "toon-tuner-previews" not in dumped
    assert job["result"]["preview_url"].startswith("/api/preview/")


# =============================================================================
#  6. 前端契约（真实浏览器验收见 tests/browser_check.mjs）
# =============================================================================


def test_frontend_exposes_framing_controls_and_stale_banner() -> None:
    html = Path("src/web/index.html").read_text(encoding="utf-8")
    js = Path("src/web/app.js").read_text(encoding="utf-8")

    for element_id in (
        "framing-bar",
        "framing-frame",
        "framing-camera",
        "framing-camera-anim",
        "framing-inside",
        "framing-source",
        "framing-mode",
        "framing-margin",
        "reframe-btn",
        "framing-stale",
    ):
        assert f'id="{element_id}"' in html, f"缺少取景界面元素：{element_id}"

    for mode in ("current_camera", "auto_full_body", "auto_upper_body", "auto_headshot"):
        assert mode in js, f"前端缺少取景方式：{mode}"

    assert "当前帧/相机已变化，请刷新基线" in js, "必须显示刷新基线提示"
    assert "FRAMING_STALE" in js, "必须识别取景失效错误码"
    assert "临时自动取景" in js and "当前相机预览" in js, "必须标明取景来源"
    assert "不会改动用户相机" in js or "不改动用户相机" in js
    assert "framingStale" in js, "必须用状态位停止自动预览"
    assert "自动预览已停止" in js

    # 「是否入画」的判据必须跟着取景方式走，并标注参照的相机。
    assert "applyInsideVerdict" in js, "入画判据必须走统一入口，避免两条路径文案不一致"
    assert "按临时取景相机" in js and "按当前相机" in js, "判据必须标注参照的相机"
    assert js.count("applyInsideVerdict(") >= 3, "上下文渲染与任务结果两条路径都要写判据"
    assert "setInsidePlaceholder" in js, "切到自动取景时应先置为取景中，避免滞留上一次结论"

    # 顶部「当前相机」描述场景状态：临时相机渲染完即销毁，必须显示工程相机。
    assert "framingResult.original_camera || framingResult.camera_used" in js, (
        "自动取景后顶部「当前相机」必须显示工程相机（original_camera），而不是已销毁的临时相机"
    )
    assert "framingResult.camera_used || framingResult.original_camera" not in js, (
        "禁止把已销毁的临时相机当作「当前相机」显示"
    )
