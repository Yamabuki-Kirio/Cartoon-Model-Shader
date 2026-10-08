"""MVP-02：API 层端到端测试（假 MCP + 真执行生成的代码）。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

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


@pytest.fixture()
def tuner_client():
    fake = FakeBpy()
    with FakeMCPServer(executor=lambda code: run_generated_code(code, fake)) as server:
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
