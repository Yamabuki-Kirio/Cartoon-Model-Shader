"""MVP-02：API 层端到端测试（假 MCP + 真执行生成的代码）。"""

from __future__ import annotations

import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from src.server import blender_ops
from src.server.app import create_app
from src.server.config import AppConfig, BlenderMCPConfig, ServerConfig
from tests.fake_bpy import PNG_BYTES, FakeBpy, run_generated_code
from tests.fake_mcp_server import FakeMCPServer


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


def render_failing_executor(fake: FakeBpy):
    """只让「渲染」这一步失败：其余步骤照常真实执行。

    模拟 Blender 报告渲染未产出文件（``rendered=False``），用于验证
    「基线参数保留，但任务必须落 failed」这一分支。
    """

    def run(code: str) -> str:
        if "bpy.ops.render.render" in code:
            return blender_ops.JSON_MARKER + json.dumps(
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
        return run_generated_code(code, fake)

    return run


@pytest.fixture()
def tuner_client():
    fake = FakeBpy()
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
        with TestClient(make_app(server)) as client:
            yield client, fake


@pytest.fixture()
def render_failing_client():
    fake = FakeBpy()
    with FakeMCPServer(executor=render_failing_executor(fake)) as server:
        with TestClient(make_app(server)) as client:
            yield client, fake


def test_schema_endpoint_lists_exposure_params(tuner_client) -> None:
    client, _ = tuner_client
    body = client.get("/api/params/schema").json()
    assert body["ok"] is True
    ids = [p["id"] for g in body["groups"] for p in g["params"]]
    assert "color.exposure" in ids
    assert "color.view_transform" in ids


def test_baseline_endpoint_captures_values(tuner_client) -> None:
    client, _ = tuner_client
    body = client.post("/api/session/baseline").json()
    assert body["ok"] is True
    assert body["values"]["color.exposure"] == 0.0
    assert body["glare_present"] is True
    # 桩环境无 OCIO → 回退静态候选，仍非空
    assert body["options"]["view.view_transform"]

    again = client.get("/api/session/baseline").json()
    assert again["baseline_id"] == body["baseline_id"]


def test_preview_requires_baseline(tuner_client) -> None:
    client, _ = tuner_client
    response = client.post("/api/preview", json={"draft": {"color.exposure": 1.0}})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_BASELINE"
    # 未建立基线时不应能读到基线
    assert client.get("/api/session/baseline").status_code == 409


def test_full_preview_flow(tuner_client) -> None:
    client, fake = tuner_client
    client.post("/api/session/baseline")

    submit = client.post("/api/preview", json={"draft": {"color.exposure": 1.0}}).json()
    assert submit["ok"] is True
    job = wait_job(client, submit["job_id"])
    assert job["status"] == "done", job.get("error")
    assert job["steps"] == ["恢复基线", "应用完整草稿", "渲染预览", "恢复基线并校验"]
    assert job["result"]["applied"]["color.exposure"] == pytest.approx(1.0)
    assert job["result"]["restore_verified"] is True

    image = client.get(job["result"]["preview_url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert image.content == PNG_BYTES
    # 预览结束后 Blender 侧已回到基线
    assert fake.context.scene.view_settings.exposure == pytest.approx(0.0)


def test_restore_endpoint_verifies(tuner_client) -> None:
    client, _ = tuner_client
    client.post("/api/session/baseline")
    body = client.post("/api/session/restore").json()
    assert body["ok"] is True
    assert body["verified"] is True
    assert body["mismatches"] == []


def test_preview_rejects_invalid_inputs(tuner_client) -> None:
    client, _ = tuner_client
    client.post("/api/session/baseline")
    cases = [
        {"draft": {"system.evil": 1}},
        {"draft": {"color.exposure": 999}},
        {"draft": {"color.exposure": "1.0"}},
        {"draft": {"color.view_transform": "NotAThing"}},
        {"draft": {"color.gamma": True}},
    ]
    for payload in cases:
        response = client.post("/api/preview", json=payload)
        assert response.status_code == 400, payload
        assert response.json()["error"]["code"] == "PARAM_INVALID"


def test_no_arbitrary_python_route_exists(tuner_client) -> None:
    client, _ = tuner_client
    for path in ("/api/execute", "/api/eval", "/api/python", "/api/blender/execute_code"):
        assert client.post(path, json={"code": "print(1)"}).status_code == 404


def test_unknown_job_and_image_are_404(tuner_client) -> None:
    client, _ = tuner_client
    assert client.get("/api/jobs/deadbeef").status_code == 404
    assert client.get("/api/preview/deadbeef").status_code == 404


def test_preview_image_requires_done_job(tuner_client) -> None:
    client, _ = tuner_client
    client.post("/api/session/baseline")
    submit = client.post("/api/preview", json={"draft": {"color.exposure": 0.2}}).json()
    wait_job(client, submit["job_id"])
    # 已完成的 job 能取图
    assert client.get(f"/api/preview/{submit['job_id']}").status_code == 200
    # 未知 job 取图 404
    assert client.get("/api/preview/nope").status_code == 404


def test_superseded_job_reports_terminal_status(tuner_client) -> None:
    client, _ = tuner_client
    client.post("/api/session/baseline")
    first = client.post("/api/preview", json={"draft": {"color.exposure": 1.0}}).json()["job_id"]
    second = client.post("/api/preview", json={"draft": {"color.exposure": -1.0}}).json()["job_id"]

    final_second = wait_job(client, second)
    assert final_second["status"] == "done"

    # 关键：旧任务必须到达终态，否则前端会一直轮询
    deadline = time.monotonic() + 10.0
    first_body = client.get(f"/api/jobs/{first}").json()
    while time.monotonic() < deadline and first_body["status"] in ("queued", "running"):
        time.sleep(0.05)
        first_body = client.get(f"/api/jobs/{first}").json()
    assert first_body["status"] == "superseded"
    assert first_body["superseded"] is True
    # 被取代的任务不得携带任何结果（响应模型会显式给出 null）
    assert first_body.get("result") is None


def test_glow_params_preview_flow(tuner_client) -> None:
    """加入辉光组后，同一套基线/草稿/预览/回滚链路仍然成立。"""
    client, fake = tuner_client
    client.post("/api/session/baseline")
    submit = client.post(
        "/api/preview",
        json={
            "draft": {
                "glow.threshold": 1.8,
                "glow.strength": 0.9,
                "glow.size": 0.3,
                "color.exposure": 0.25,
            }
        },
    ).json()
    job = wait_job(client, submit["job_id"])
    assert job["status"] == "done", job.get("error")
    applied = job["result"]["applied"]
    assert applied["glow.threshold"] == pytest.approx(1.8)
    assert applied["glow.strength"] == pytest.approx(0.9)
    assert applied["glow.size"] == pytest.approx(0.3)
    assert job["result"]["restore_verified"] is True
    # 回滚后辉光插座回到基线值
    assert fake.glare_node.inputs.get("Threshold").default_value == pytest.approx(1.1)  # type: ignore[union-attr]


def test_glow_out_of_range_and_bad_enum_rejected(tuner_client) -> None:
    client, _ = tuner_client
    client.post("/api/session/baseline")
    for payload in (
        {"draft": {"glow.threshold": -1}},
        {"draft": {"glow.size": 2}},
        {"draft": {"glow.strength": "1.0"}},
        {"draft": {"glow.type": "SuperNova"}},
        {"draft": {"glow.quality": "Ultra"}},
    ):
        response = client.post("/api/preview", json=payload)
        assert response.status_code == 400, payload
        assert response.json()["error"]["code"] == "PARAM_INVALID"


def test_disconnected_blender_yields_error_envelope() -> None:
    """Blender 不在时，预览提交应给出可重试的错误而不是崩溃。"""
    from tests.fake_mcp_server import find_free_port

    config = AppConfig(
        blender_mcp=BlenderMCPConfig(
            host="127.0.0.1", port=find_free_port(), connect_timeout_seconds=0.4,
            response_timeout_seconds=0.4,
        ),
        server=ServerConfig(),
    )
    with TestClient(create_app(config)) as client:
        assert client.post("/api/session/baseline").status_code in (503, 504)


# -- 基线首张预览（本次修复） ---------------------------------------------


def test_baseline_creates_first_preview_job(tuner_client) -> None:
    """建立基线后必须立即创建首张预览任务，并产出可访问的 preview_url。"""
    client, fake = tuner_client
    body = client.post("/api/session/baseline").json()

    assert body["ok"] is True
    assert body["job_id"], "建立基线必须同时返回首张预览任务 id"
    assert body["preview_url"] == f"/api/preview/{body['job_id']}"
    assert body["job_status"] in ("queued", "running")

    job = wait_job(client, body["job_id"])
    assert job["status"] == "done", job.get("error")
    assert job["result"]["preview_url"] == body["preview_url"]
    # 首张预览 = 用基线参数渲染（草稿为空，回读即为基线值）
    assert job["result"]["applied"]["color.exposure"] == pytest.approx(0.0)
    assert job["result"]["restore_verified"] is True
    # 渲染确实发生过，且分辨率已还原
    assert fake.render_count == 1
    assert fake.context.scene.render.resolution_x == 1080

    image = client.get(job["result"]["preview_url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == PNG_BYTES


def test_preview_url_is_http_endpoint_without_local_paths(tuner_client) -> None:
    """预览图必须由 HTTP 端点提供，返回给浏览器的 URL 不得出现本机路径。"""
    client, fake = tuner_client
    body = client.post("/api/session/baseline").json()
    job = wait_job(client, body["job_id"])
    url = job["result"]["preview_url"]

    assert url.startswith("/api/preview/")
    assert "\\" not in url
    assert not re.match(r"^[A-Za-z]:", url)
    assert "toon-tuner-previews" not in url
    # 临时目录只应出现在 Blender 侧（服务端内部），不进入 API 响应
    assert str(__import__("tempfile").gettempdir()) not in json.dumps(job, ensure_ascii=False)

    # 前端会追加 ?v=<job_id> 防缓存，同一 URL 带查询串仍可取到图
    assert client.get(f"{url}?v={body['job_id']}").status_code == 200


def test_stale_job_cannot_replace_newer_image(tuner_client) -> None:
    """旧任务必须进入 superseded 终态且不带 result —— 前端据此不覆盖新图片。"""
    client, _ = tuner_client
    base = client.post("/api/session/baseline").json()
    first = client.post("/api/preview", json={"draft": {"color.exposure": 2.0}}).json()["job_id"]
    second = client.post("/api/preview", json={"draft": {"color.exposure": -2.0}}).json()["job_id"]

    newest = wait_job(client, second)
    assert newest["status"] == "done"
    assert newest["result"]["applied"]["color.exposure"] == pytest.approx(-2.0)

    stale = wait_job(client, first)
    assert stale["status"] == "superseded"
    assert stale["superseded"] is True
    assert stale.get("result") is None, "被取代的任务不得携带任何图片结果"

    # 基线任务同样被后来的预览取代
    assert client.get(f"/api/jobs/{base['job_id']}").json()["status"] == "superseded"


def test_baseline_preview_failure_keeps_baseline_but_fails_job(render_failing_client) -> None:
    """渲染失败时：基线参数保留，但任务必须落 failed 且带稳定错误码。"""
    client, _ = render_failing_client
    body = client.post("/api/session/baseline").json()

    assert body["ok"] is True, "渲染失败不应丢掉基线采集结果"
    assert body["values"]["color.exposure"] == pytest.approx(0.0)
    assert body["job_id"]

    job = wait_job(client, body["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "PREVIEW_FAILED"
    assert job.get("result") is None, "失败任务绝不能携带 preview_url"

    # 基线仍然可读，说明「基线保留、仅预览失败」
    again = client.get("/api/session/baseline").json()
    assert again["baseline_id"] == body["baseline_id"]

    # 失败的 job 取图必须报错，而不是给出 200
    assert client.get(f"/api/preview/{body['job_id']}").status_code == 404


def test_frontend_uses_job_id_cache_busting_and_handles_image_error() -> None:
    """前端静态契约：URL 带 job_id 防缓存；图片 error 必须落到错误态。

    这两点是纯浏览器行为，无法用 TestClient 覆盖，故在此锁定源码契约，
    真实浏览器验证见 tests/browser_check.mjs。
    """
    from pathlib import Path

    source = Path("src/web/app.js").read_text(encoding="utf-8")
    assert '"?v=" + encodeURIComponent(job.job_id)' in source, "预览 URL 必须带 job_id 防缓存"
    assert "img.onerror" in source, "必须处理图片加载失败"
    assert "基线已建立，预览失败" in source
    assert "previewToken" in source, "必须用代次守卫防止旧任务覆盖新图"
