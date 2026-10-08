"""MVP-02：生成代码 + 会话（基线/草稿/预览/回滚）的单元测试。

关键点：这些测试**真的执行服务端生成的 Python 代码**（``bpy`` 换成桩），
因此能发现生成代码里的逻辑错误，而不是只验证「响应格式」。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.server import blender_ops, errors, framing
from src.server.binder import BlenderBinder
from src.server.blender_ops import JSON_MARKER, build_read_code, build_set_code
from src.server.config import BlenderMCPConfig
from src.server.session import PreviewService
from tests.fake_bpy import FakeBpy, FakeImage, extract_marker, run_generated_code
from tests.fake_mcp_server import FakeMCPServer


# -- 生成代码 ---------------------------------------------------------------


def test_read_code_returns_glare_strings_not_char_lists() -> None:
    """回归：字符串属性不能被 list() 拆成字符列表。"""
    fake = FakeBpy()
    payload = extract_marker(run_generated_code(build_read_code(), fake), JSON_MARKER)
    assert payload["glare"]["Type"] == "Bloom"
    assert payload["glare"]["Quality"] == "Medium"
    assert payload["glare"]["Threshold"] == pytest.approx(1.1)
    assert payload["glare"]["Size"] == pytest.approx(0.5)
    assert payload["view"]["view_transform"] == "AgX"
    assert payload["glare_present"] is True


def test_set_code_applies_and_reads_back() -> None:
    fake = FakeBpy()
    code = build_set_code({"color.exposure": 1.25, "color.gamma": 0.9, "color.look": "None"})
    payload = extract_marker(run_generated_code(code, fake), JSON_MARKER)
    assert payload["view"]["exposure"] == pytest.approx(1.25)
    assert payload["view"]["gamma"] == pytest.approx(0.9)
    assert payload["view"]["look"] == "None"
    assert fake.context.scene.view_settings.exposure == pytest.approx(1.25)


def test_set_code_preserves_full_draft_semantics() -> None:
    """整份草稿一次性应用：未显式给出的参数保持原值，不被重置。"""
    fake = FakeBpy()
    fake.context.scene.view_settings.gamma = 1.7
    run_generated_code(build_set_code({"color.exposure": 0.5}), fake)
    assert fake.context.scene.view_settings.gamma == pytest.approx(1.7)


def test_set_code_rejects_non_whitelisted_param() -> None:
    with pytest.raises(errors.ToonTunerError) as exc:
        build_set_code({"system.evil": 1})
    assert exc.value.code == errors.PARAM_INVALID


def test_set_code_cannot_inject_code_through_values() -> None:
    """值只作为字面量出现，无法注入语句。"""
    fake = FakeBpy()
    evil = "AgX'\nimport os\nos.system('echo pwned')\n#"
    code = build_set_code({"color.view_transform": evil})
    # 关卡一：evil 只以 repr 字面量形式出现在生成的代码里，注入语句不构成独立代码行
    assert "os.system('echo pwned')" not in code.replace(repr(evil), "")
    # 关卡二：真实执行后只是把整串赋给了字符串属性，未执行任何注入语句
    payload = extract_marker(run_generated_code(code, fake), JSON_MARKER)
    assert payload["view"]["view_transform"] == evil


def test_render_code_writes_png_and_restores_resolution(tmp_path) -> None:
    """渲染代码：落盘 PNG、按取景方式渲染、最后恢复原分辨率与原相机。"""
    fake = FakeBpy()
    target = tmp_path / "preview.png"
    code = framing.build_render_code(target.as_posix(), 540, 990, 100)
    payload = extract_marker(run_generated_code(code, fake), JSON_MARKER)
    assert payload["rendered"] is True
    assert payload["size_bytes"] > 0
    assert target.is_file()
    assert payload["render_resolution"] == [540, 990, 100]
    assert payload["restored_resolution"] == [1080, 1980, 100]
    assert fake.render_count == 1
    assert fake.last_render_resolution == (540, 990, 100)
    # 默认（当前相机）模式：不得新建临时相机，且相机与帧原样不动
    assert payload["camera_used"] == "Camera"
    assert payload["camera_restored"] is True
    assert payload["temporary_camera_leftovers"] == 0
    assert fake.snapshot_framing()["camera"] == "Camera"


def test_render_code_restores_resolution_even_when_saving_fails(tmp_path, monkeypatch) -> None:
    fake = FakeBpy()

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(FakeImage, "save_render", boom, raising=True)
    assert fake.context.scene.render.resolution_x == 1080
    with pytest.raises(RuntimeError):
        run_generated_code(framing.build_render_code((tmp_path / "x.png").as_posix(), 540, 990, 100), fake)
    # finally 分支必须已把分辨率还原
    assert fake.context.scene.render.resolution_x == 1080
    assert fake.context.scene.render.resolution_y == 1980
    assert fake.context.scene.render.resolution_percentage == 100
    assert fake.context.scene.camera is not None
    assert fake.context.scene.camera.name == "Camera"


# -- 会话（基线 / 草稿 / 预览 / 回滚）---------------------------------------


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


def test_session_baseline_preview_and_rollback() -> None:
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                baseline = await service.capture_baseline()
                assert baseline["values"]["color.exposure"] == 0.0
                # 桩环境没有 OCIO，候选项应回退到 params 的静态列表
                assert "AgX" in baseline["options"]["view.view_transform"]

                job = await service.submit({"color.exposure": 1.5})
                finished = await _wait(service, job.job_id)()
                assert finished.status == "done", finished.error
                assert finished.result is not None
                assert finished.result["applied"]["color.exposure"] == pytest.approx(1.5)
                assert finished.result["restore_verified"] is True
                assert finished.result["preview_url"] == f"/api/preview/{job.job_id}"
                # 预览结束后 Blender 侧必须已回到基线
                assert fake.context.scene.view_settings.exposure == pytest.approx(0.0)
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_session_restore_is_idempotent() -> None:
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                first = await service.restore_baseline()
                second = await service.restore_baseline()
                assert first["verified"] is True
                assert second["verified"] is True
                assert first["readback"] == second["readback"]
                assert first["mismatches"] == []
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_session_requires_baseline_before_preview() -> None:
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                with pytest.raises(errors.ToonTunerError) as exc:
                    await service.submit({"color.exposure": 1.0})
                assert exc.value.code == errors.NO_BASELINE
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_session_discards_superseded_job() -> None:
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                old = await service.submit({"color.exposure": 1.0})
                new = await service.submit({"color.exposure": -1.0})
                finished_new = await _wait(service, new.job_id)()
                finished_old = service.get_job(old.job_id)
                assert finished_new.status == "done"
                assert finished_old is not None
                assert finished_old.status == "superseded"
                assert finished_old.superseded is True
                assert finished_old.result is None
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_session_draft_values_are_independent_per_job() -> None:
    """两次预览各自应用整份草稿，不会相互叠加。"""
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                first = await service.submit({"color.exposure": 2.0})
                await _wait(service, first.job_id)()
                second = await service.submit({"color.gamma": 1.5})
                finished = await _wait(service, second.job_id)()
                assert finished.status == "done"
                # 第二次草稿没提 exposure，应回到基线 0，而不是保留上一次的 2.0
                assert finished.result["applied"]["color.exposure"] == pytest.approx(0.0)
                assert finished.result["applied"]["color.gamma"] == pytest.approx(1.5)
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_session_enforces_param_ranges_and_enums() -> None:
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                for draft in (
                    {"color.exposure": 99},
                    {"color.exposure": -99},
                    {"color.exposure": "1.0"},
                    {"color.gamma": 0.0},
                    {"color.view_transform": "NotAThing"},
                    {"system.evil": 1},
                    "not-a-dict",
                ):
                    with pytest.raises(errors.ToonTunerError) as exc:
                        await service.submit(draft)  # type: ignore[arg-type]
                    assert exc.value.code == errors.PARAM_INVALID, draft
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_job_registry_is_bounded() -> None:
    fake = FakeBpy()

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(_binder(server))
            await service.start()
            try:
                await service.capture_baseline()
                for i in range(60):
                    job = await service.submit({"color.exposure": i / 10.0})
                    await _wait(service, job.job_id)()
                assert len(service._jobs) <= 50  # noqa: SLF001 - 内部上限的显式断言
                assert service._jobs  # noqa: SLF001
            finally:
                await service.stop()

    asyncio.run(scenario())


def test_extract_json_reports_missing_marker() -> None:
    with pytest.raises(errors.BlenderUnexpectedResponse):
        blender_ops.extract_json("no marker here")


def test_params_schema_serializes_groups() -> None:
    from src.server import params

    schema = params.public_schema({})
    assert schema["schema"] == "toon-l0-surface/1"
    ids = [p["id"] for g in schema["groups"] for p in g["params"]]
    assert "color.exposure" in ids
    assert {"glow.threshold", "glow.strength", "glow.size"} <= set(ids)
    exposure = next(p for g in schema["groups"] for p in g["params"] if p["id"] == "color.exposure")
    assert exposure["minimum"] == -10.0 and exposure["maximum"] == 10.0
    assert [g["name"] for g in schema["groups"]] == ["曝光", "辉光"]
    assert isinstance(json.loads(json.dumps(schema)), dict)


def test_glow_params_are_wired_to_glare_node() -> None:
    """辉光参数写入后必须落到 Autocel_Glow 对应插座上。"""
    fake = FakeBpy()
    code = build_set_code({"glow.threshold": 2.5, "glow.strength": 0.4, "glow.size": 0.2})
    payload = extract_marker(run_generated_code(code, fake), JSON_MARKER)
    assert payload["glare"]["Threshold"] == pytest.approx(2.5)
    assert payload["glare"]["Strength"] == pytest.approx(0.4)
    assert payload["glare"]["Size"] == pytest.approx(0.2)
    assert fake.glare_node.inputs.get("Threshold").default_value == pytest.approx(2.5)  # type: ignore[union-attr]


def test_glow_enum_params_stay_strings() -> None:
    fake = FakeBpy()
    code = build_set_code({"glow.type": "Streaks", "glow.quality": "High"})
    payload = extract_marker(run_generated_code(code, fake), JSON_MARKER)
    assert payload["glare"]["Type"] == "Streaks"
    assert payload["glare"]["Quality"] == "High"


def test_missing_glare_node_reports_clear_error() -> None:
    fake = FakeBpy()
    fake.node_groups.pop("AI_Compositor")
    with pytest.raises(RuntimeError, match="Autocel_Glow"):
        run_generated_code(build_set_code({"glow.threshold": 1.0}), fake)
