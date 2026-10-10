"""F1/F2 回归：预览输出的 PNG 隔离与渲染状态逐项恢复。

## 缺陷背景（真机实测，非推测）

F1（阻断）：预览渲染代码里**硬写** ``render.image_settings.file_format = "PNG"``。
工程是影片输出时 ``image_settings.media_type == "VIDEO"``，此时 ``file_format``
的可用集合被限定为影片格式，该赋值直接抛

    TypeError: bpy_struct: item.attr = val: enum "PNG" not found in ('FFMPEG')

→ 该工程上**所有**预览任务失败。

F2（潜伏）：同一处还写了 ``color_mode``，且 ``finally`` **只**恢复了相机 / 分辨率 /
采样数 —— ``file_format`` 与 ``color_mode`` 从未还原。在本来就是 PNG 的工程上是
no-op 看不出来；在 JPEG / EXR 工程上会把工程悄悄改成 PNG 而不还原。

## 本文件锁定的行为

1. 渲染前整份记录 ``media_type`` / ``file_format`` / ``color_mode`` / ``color_depth`` /
   ``filepath``，渲染后**逐项**写回；
2. 切 PNG 走「先切 ``media_type`` 到 ``IMAGE`` → 赋值 → **回读**」，
   ``bl_rna.enum_items`` 不算数（影片态下它照样列出 PNG）；
3. 切不过去就**什么都不改**地中止，回稳定错误码 ``PREVIEW_OUTPUT_UNAVAILABLE``，
   不渲染、不留半成品；
4. ``finally`` 逐项独立 —— 渲染失败、存图失败、临时相机清理失败，都不阻断其余恢复。

桩（``tests/fake_bpy.FakeImageSettings``）刻意复刻了真机的枚举约束，
所以「硬写 PNG」在这里一定会失败，修好了才能通过。
"""

from __future__ import annotations

import inspect
import time

import pytest
from fastapi.testclient import TestClient

from src.server import blender_ops, errors, framing
from src.server.app import create_app
from src.server.binder import BlenderBinder
from src.server.config import AppConfig, BlenderMCPConfig, ServerConfig
from src.server.session import PreviewService
from tests.fake_bpy import PNG_BYTES, FakeBpy, run_generated_code
from tests.fake_mcp_server import FakeMCPServer
from tests.support import authed

RENDER_MARKER = blender_ops.JSON_MARKER
USER_MOVIE_OUTPUT = "Z:/user/movie/output"


# =============================================================================
#  工具
# =============================================================================


def build_render_code(target, mode: str = "current_camera", *, margin: float = 0.15) -> str:
    """构造渲染代码。

    ``samples`` 是**预览质量档位**的参数，与本文件要验证的输出隔离无关；它在
    质量档位分支上才存在，因此按签名自适应，避免本文件在两个分支上互相牵制。
    """
    args = [
        str(target).replace("\\", "/"), 540, 990, 100,
        {"mode": mode, "margin": margin}, None,
    ]
    if "samples" in inspect.signature(framing.build_render_code).parameters:
        args.append(8)
    return framing.build_render_code(*args)


def output_summary(result: dict) -> dict:
    """取出「输出设置」的审计块。

    PR#4 分支直接放在 ``result["output"]``；质量档位分支把它折在
    ``result["quality"]["output"]`` 下 —— 两处都认得，断言口径一致。
    """
    return result.get("output") or (result.get("quality") or {}).get("output") or {}


def run_render(fake: FakeBpy, target, mode: str = "current_camera", *, margin: float = 0.15) -> dict:
    """真的执行服务端生成的渲染代码（bpy 换成桩），返回 Blender 侧回传的负载。"""
    stdout = run_generated_code(build_render_code(target, mode, margin=margin), fake)
    return blender_ops.extract_json(stdout)


def set_video_output(fake: FakeBpy, *, filepath: str = USER_MOVIE_OUTPUT) -> None:
    """把桩的工程切成「影片输出」—— 也就是 F1 现场。"""
    render = fake.context.scene.render
    render.filepath = filepath
    render.image_settings.media_type = "VIDEO"
    render.image_settings.file_format = "FFMPEG"


def make_app(server: FakeMCPServer):
    config = AppConfig(
        blender_mcp=BlenderMCPConfig(
            host=server.host,
            port=server.port,
            connect_timeout_seconds=1.0,
            response_timeout_seconds=3.0,
        ),
        server=ServerConfig(),
    )
    return create_app(config)


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
def video_client():
    """API 级夹具：工程处于影片输出（FFMPEG）状态。"""
    fake = FakeBpy()
    set_video_output(fake)
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        with authed(make_app(server)) as client:
            yield client, fake


# =============================================================================
#  0. 桩的保真度：F1 的机制必须在这里能被复现
# =============================================================================


def test_stub_reproduces_blender_enum_error_under_video_output() -> None:
    """影片输出态下硬写 PNG 必抛 —— 桩忠实复刻真机的枚举约束。"""
    is_ = FakeBpy().context.scene.render.image_settings
    is_.media_type = "VIDEO"
    is_.file_format = "FFMPEG"

    with pytest.raises(TypeError) as exc:
        is_.file_format = "PNG"
    assert 'enum "PNG" not found in' in str(exc.value)
    assert "'FFMPEG'" in str(exc.value)

    # media_type 是**静态枚举**：切换是双向可行的，这正是修复的立足点
    is_.media_type = "IMAGE"
    is_.file_format = "PNG"
    assert is_.file_format == "PNG"


def test_stub_enum_items_listing_lies_under_video_output() -> None:
    """``bl_rna.enum_items`` 在影片态下**照样**列出 PNG —— 所以可用性只能靠赋值判定。

    真机实测：``enum_items`` 返回全量列表，但赋值抛 ``enum "PNG" not found in ('FFMPEG')``。
    本测试把这个陷阱钉住，防止有人把生产代码「优化」成读枚举来判断可用性。
    """
    is_ = FakeBpy().context.scene.render.image_settings
    is_.media_type = "VIDEO"
    is_.file_format = "FFMPEG"

    assert "PNG" in is_.static_enum_items("file_format"), "静态枚举列表本身包含 PNG"
    assert is_.available_file_formats == ("FFMPEG",), "但真正可用的只有影片格式"


def test_stub_changing_format_resets_color_mode_and_depth() -> None:
    """换 ``file_format`` 会**连带**改 ``color_mode`` / ``color_depth`` —— F2 的根源之一。"""
    is_ = FakeBpy().context.scene.render.image_settings
    is_.file_format = "OPEN_EXR"
    assert (is_.color_mode, is_.color_depth) == ("RGB", "32")
    is_.file_format = "PNG"
    assert (is_.color_mode, is_.color_depth) == ("RGB", "16")


# =============================================================================
#  1. F1 主场景：FFMPEG / QUICKTIME 工程
# =============================================================================


def test_preview_succeeds_on_movie_output_project(tmp_path) -> None:
    """影片输出工程上预览必须成功 —— 这就是 F1 的修复本身。"""
    fake = FakeBpy()
    set_video_output(fake)
    target = tmp_path / "preview.png"

    payload = run_render(fake, target)

    assert payload["aborted"] is None
    assert payload["rendered"] is True
    assert payload["output"]["applied"] is True
    assert payload["output"]["unavailable_reason"] is None
    assert target.read_bytes() == PNG_BYTES, "确实产出了一张 PNG"

    # 保存图片时场景必须**真的**处于 PNG：真机里 save_render 听场景设置、不听扩展名
    render_result = fake.data.images.render_result  # type: ignore[attr-defined]
    assert render_result.saved_with_format == ["PNG"]


def test_movie_output_project_is_fully_restored(tmp_path) -> None:
    """渲染结束后，五项输出设置逐项回到「影片输出」原样。"""
    fake = FakeBpy()
    set_video_output(fake)
    render = fake.context.scene.render
    before = {
        "media_type": "VIDEO",
        "file_format": "FFMPEG",
        "color_mode": render.image_settings.color_mode,
        "color_depth": render.image_settings.color_depth,
        "filepath": USER_MOVIE_OUTPUT,
    }

    payload = run_render(fake, tmp_path / "preview.png")

    output = payload["output"]
    assert output["original"] == before
    assert output["restored"] == before
    assert output["mismatches"] == {}
    assert output["restored_ok"] is True

    # 逐项写回都成功了（不是「恰好没改」，而是真的改过又写回）
    for name in ("media_type", "file_format", "color_mode", "color_depth"):
        step = output["restore_steps"][name]
        assert step["ok"] is True, f"{name} 未还原：{step}"
        assert step["readback"] == before[name]
    assert render.image_settings.media_type == "VIDEO"
    assert render.image_settings.file_format == "FFMPEG"
    assert render.filepath == USER_MOVIE_OUTPUT


def test_output_restoration_is_observed_by_whole_scene_snapshot(tmp_path) -> None:
    """整景快照（含输出设置、resolution、相机）前后必须完全一致。"""
    fake = FakeBpy()
    set_video_output(fake)
    before = fake.snapshot_framing()

    run_render(fake, tmp_path / "preview.png", mode="auto_full_body")

    assert fake.snapshot_framing() == before


# =============================================================================
#  2. 四种初始输出格式
# =============================================================================


@pytest.mark.parametrize(
    ("media_type", "file_format", "color_mode", "color_depth"),
    [
        ("VIDEO", "FFMPEG", "RGB", "8"),      # 影片输出（F1 现场）
        ("IMAGE", "JPEG", "RGB", "8"),
        ("IMAGE", "PNG", "RGBA", "8"),
        ("IMAGE", "OPEN_EXR", "RGB", "32"),
    ],
    ids=["FFMPEG-QUICKTIME", "JPEG", "PNG", "OPEN_EXR"],
)
def test_all_initial_output_formats_render_and_restore(
    tmp_path, media_type: str, file_format: str, color_mode: str, color_depth: str
) -> None:
    fake = FakeBpy()
    render = fake.context.scene.render
    render.filepath = USER_MOVIE_OUTPUT
    render.image_settings.media_type = media_type
    render.image_settings.file_format = file_format
    render.image_settings.color_mode = color_mode
    render.image_settings.color_depth = color_depth
    before = render.image_settings.snapshot()
    before_path = render.filepath
    target = tmp_path / f"preview_{file_format}.png"

    payload = run_render(fake, target)

    assert payload["rendered"] is True, f"{file_format} 起步时预览失败"
    assert payload["output"]["applied"] is True
    # image_settings.snapshot() 只含四项；filepath 在 render 上，单独比
    for name in ("media_type", "file_format", "color_mode", "color_depth"):
        assert payload["output"]["restored"][name] == before[name], f"{name} 未还原"
    assert payload["output"]["restored"]["filepath"] == before_path
    assert payload["output"]["mismatches"] == {}
    assert payload["output"]["restored_ok"] is True
    assert render.image_settings.snapshot() == before
    assert render.filepath == before_path
    assert target.read_bytes() == PNG_BYTES
    assert fake.data.images.render_result.saved_with_format == ["PNG"]  # type: ignore[attr-defined]


# =============================================================================
#  3. 渲染失败 / 存图失败：仍要逐项恢复，且不留半成品
# =============================================================================


def test_render_exception_still_restores_everything(tmp_path) -> None:
    """渲染抛异常：`finally` 必须照常把输出设置、分辨率、相机全部写回。"""
    fake = FakeBpy()
    set_video_output(fake)
    before = fake.snapshot_framing()
    fake.render_error = "注入的渲染失败"
    target = tmp_path / "preview.png"

    code = build_render_code(target, "auto_full_body")
    with pytest.raises(RuntimeError, match="注入的渲染失败"):
        run_generated_code(code, fake)

    assert fake.snapshot_framing() == before, "渲染异常路径下工程被污染了"
    assert fake.temp_camera_objects() == []
    assert not target.exists(), "失败路径不得留下半成品预览"


def test_save_render_failure_removes_partial_file_and_restores(tmp_path) -> None:
    """存图失败：半成品文件必须被清掉，设置必须全部还原。"""
    fake = FakeBpy()
    set_video_output(fake)
    before = fake.snapshot_framing()
    render_result = fake.data.images.render_result  # type: ignore[attr-defined]
    render_result.save_partial_bytes = b""          # 先落一个 0 字节的半成品
    render_result.save_error = "注入的存图失败"
    target = tmp_path / "preview.png"

    code = build_render_code(target, "current_camera")
    with pytest.raises(RuntimeError, match="注入的存图失败"):
        run_generated_code(code, fake)

    assert not target.exists(), "半成品预览文件必须被清理"
    assert fake.snapshot_framing() == before


def test_temp_camera_cleanup_failure_does_not_block_other_restores(tmp_path) -> None:
    """临时相机删不掉时，其余恢复项**必须继续执行**（要求 3 的逐项独立）。"""
    fake = FakeBpy()
    set_video_output(fake)
    render = fake.context.scene.render
    before = {
        "media_type": "VIDEO",
        "file_format": "FFMPEG",
        "color_mode": render.image_settings.color_mode,
        "color_depth": render.image_settings.color_depth,
        "filepath": USER_MOVIE_OUTPUT,
    }
    original_camera = fake.context.scene.camera
    fake.remove_object_error = "注入的对象删除失败"
    target = tmp_path / "preview.png"

    payload = run_render(fake, target, mode="auto_full_body")

    # 清理失败被如实报出（残留 1 个临时相机），但**不阻断**其它恢复
    assert payload["temporary_camera_leftovers"] == 1
    assert payload["rendered"] is True
    assert payload["output"]["restored"] == before
    assert payload["output"]["restored_ok"] is True
    assert payload["camera_restored"] is True
    assert fake.context.scene.camera is original_camera
    assert render.resolution_x == 1080 and render.resolution_y == 1980
    assert render.image_settings.file_format == "FFMPEG"
    assert render.filepath == USER_MOVIE_OUTPUT


# =============================================================================
#  4. 切不过去：明确失败 + 零污染
# =============================================================================


def _assert_zero_pollution(fake: FakeBpy, target, before_framing: dict, before_output: dict) -> None:
    assert fake.snapshot_framing() == before_framing, "中止路径改了工程"
    assert fake.context.scene.render.image_settings.snapshot() == before_output
    assert not target.exists(), "中止路径留下了预览文件"
    assert fake.temp_camera_objects() == [], "中止路径留下了临时相机"
    assert fake.render_count == 0, "中止后仍然发起了渲染"


def test_aborts_when_media_type_cannot_switch(tmp_path) -> None:
    """media_type 切不到 IMAGE 时：中止、明确错误码、零污染、不渲染。"""
    fake = FakeBpy()
    set_video_output(fake)
    is_ = fake.context.scene.render.image_settings
    is_.reject_media_type = "IMAGE"
    before_framing = fake.snapshot_framing()
    before_output = is_.snapshot()
    target = tmp_path / "preview.png"

    payload = run_render(fake, target)

    assert payload["rendered"] is False
    assert payload["aborted"]["code"] == errors.PREVIEW_OUTPUT_UNAVAILABLE
    assert "IMAGE" in payload["aborted"]["message"]
    assert payload["output"]["applied"] is False
    assert payload["output"]["unavailable_reason"]
    _assert_zero_pollution(fake, target, before_framing, before_output)


def test_aborts_when_file_format_rejects_png(tmp_path) -> None:
    """即使 media_type 已是 IMAGE，PNG 赋值仍被拒时同样中止且零污染。"""
    fake = FakeBpy()
    is_ = fake.context.scene.render.image_settings
    is_.file_format = "JPEG"
    is_.reject_file_format = "PNG"
    before_framing = fake.snapshot_framing()
    before_output = is_.snapshot()
    target = tmp_path / "preview.png"

    payload = run_render(fake, target)

    assert payload["rendered"] is False
    assert payload["aborted"]["code"] == errors.PREVIEW_OUTPUT_UNAVAILABLE
    assert "不接受 PNG" in payload["aborted"]["message"]
    _assert_zero_pollution(fake, target, before_framing, before_output)


def test_binder_maps_abort_code_to_stable_error(tmp_path) -> None:
    """中止码必须被翻译成稳定错误码，而不是退化成通用的 BLENDER_SCRIPT_ERROR。"""
    fake = FakeBpy()
    set_video_output(fake)
    fake.context.scene.render.image_settings.reject_media_type = "IMAGE"
    target = tmp_path / "preview.png"
    code = build_render_code(target, "current_camera")

    with FakeMCPServer(executor=lambda _code: run_generated_code(code, fake)) as server:
        binder = BlenderBinder(BlenderMCPConfig(host=server.host, port=server.port))
        with pytest.raises(errors.ToonTunerError) as exc:
            binder.render_preview("job-under-test", {"mode": "current_camera", "margin": 0.15})

    assert exc.value.code == errors.PREVIEW_OUTPUT_UNAVAILABLE
    assert exc.value.http_status == 409
    assert exc.value.retryable is False


# =============================================================================
#  5. API 级：影片输出工程端到端
# =============================================================================


def test_api_baseline_preview_succeeds_on_movie_output_project(video_client) -> None:
    """F1 的端到端修复：影片输出工程上，基线首张预览必须真的出来。"""
    client, fake = video_client
    is_ = fake.context.scene.render.image_settings

    body = client.post("/api/session/baseline").json()
    job = wait_job(client, body["job_id"])

    assert job["status"] == "done", f"预览任务失败：{job.get('error')}"
    assert job["result"]["preview_url"]
    output = output_summary(job["result"])
    assert output["applied"] is True
    assert output["verified"] is True
    # 质量档位分支另有分辨率/采样数的总闸门；存在则必须也为真
    quality = job["result"].get("quality") or {}
    if quality.get("settings_restored") is not None:
        assert quality["settings_restored"] is True

    # 工程仍是影片输出，没有被预览改成 PNG
    assert (is_.media_type, is_.file_format) == ("VIDEO", "FFMPEG")
    assert fake.context.scene.render.filepath == USER_MOVIE_OUTPUT

    # 图片端点确实能取到 PNG
    image = client.get(job["result"]["preview_url"])
    assert image.status_code == 200
    assert image.content == PNG_BYTES


def test_api_preview_fails_cleanly_when_png_unavailable(video_client) -> None:
    """PNG 切不过去时：任务失败、错误码稳定、结果不携带 preview_url、零污染。"""
    client, fake = video_client
    is_ = fake.context.scene.render.image_settings
    is_.reject_file_format = "PNG"
    is_.reject_media_type = "IMAGE"

    body = client.post("/api/session/baseline").json()
    job = wait_job(client, body["job_id"])

    assert job["status"] == "failed"
    assert job["error"]["code"] == errors.PREVIEW_OUTPUT_UNAVAILABLE
    assert job["error"]["retryable"] is False
    assert job.get("result") is None, "失败任务绝不能携带 preview_url"
    assert client.get(f"/api/preview/{body['job_id']}").status_code == 404

    # 零污染：工程与渲染设置原封不动
    assert (is_.media_type, is_.file_format) == ("VIDEO", "FFMPEG")
    assert fake.render_count == 0
    assert fake.temp_camera_objects() == []


def test_api_output_summary_is_exposed_for_audit(video_client) -> None:
    """渲染结果的 ``quality.output`` 必须能直接用于人工审计（改前 / 回读 / 逐项步骤）。"""
    client, _ = video_client
    body = client.post("/api/session/baseline").json()
    job = wait_job(client, body["job_id"])

    output = output_summary(job["result"])
    assert output["target_format"] == "PNG"
    assert set(output["original"]) == {
        "media_type", "file_format", "color_mode", "color_depth", "filepath",
    }
    assert output["original"] == output["restored"]
    assert output["mismatches"] == {}
    assert set(output["restore_steps"]) >= {
        "media_type", "file_format", "color_mode", "color_depth", "filepath",
    }
    assert all(step["ok"] for step in output["restore_steps"].values())


def test_service_keeps_baseline_when_output_unavailable(tmp_path) -> None:
    """输出切不过去时：基线仍可读，只有预览失败（与渲染失败同一策略）。"""
    import asyncio

    fake = FakeBpy()
    set_video_output(fake)
    fake.context.scene.render.image_settings.reject_media_type = "IMAGE"

    async def scenario() -> None:
        with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
            service = PreviewService(BlenderBinder(BlenderMCPConfig(host=server.host, port=server.port)))
            await service.start()
            try:
                baseline = await service.capture_baseline()
                job = await service.submit({}, {"mode": "current_camera", "margin": 0.15})
                deadline = asyncio.get_running_loop().time() + 5.0
                while asyncio.get_running_loop().time() < deadline:
                    finished = service.get_job(job.job_id)
                    if finished is not None and finished.status in ("done", "failed", "superseded"):
                        break
                    await asyncio.sleep(0.01)

                assert finished.status == "failed"
                assert finished.error["code"] == errors.PREVIEW_OUTPUT_UNAVAILABLE
                assert finished.result is None
                # 基线保留
                again = service.baseline_public()
                assert again is not None and again["baseline_id"] == baseline["baseline_id"]
            finally:
                await service.stop()

    asyncio.run(scenario())
