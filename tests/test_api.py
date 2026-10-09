"""API 层测试：健康检查、状态区分、场景读取、错误码稳定性、无任意执行接口。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.server import errors
from src.server.app import create_app
from src.server.config import AppConfig, BlenderMCPConfig, ServerConfig
from src.server.scene_probe import PROBE_SCHEMA
from tests.fake_mcp_server import FAKE_PROBE_PAYLOAD, FakeMCPServer, find_free_port
from tests.support import TOKEN_HEADER, authed, unauthed


def make_config(port: int, *, response_timeout: float = 0.6) -> AppConfig:
    return AppConfig(
        blender_mcp=BlenderMCPConfig(
            host="127.0.0.1",
            port=port,
            connect_timeout_seconds=0.5,
            response_timeout_seconds=response_timeout,
        ),
        server=ServerConfig(host="127.0.0.1", port=8765),
    )


def make_client(port: int, *, response_timeout: float = 0.6) -> TestClient:
    """带会话令牌的客户端。写接口一律需要 ``X-Toon-Tuner-Token``。"""
    return authed(create_app(make_config(port, response_timeout=response_timeout)))


# -- 基础 ---------------------------------------------------------------
def test_health() -> None:
    with FakeMCPServer("ok") as server:
        response = make_client(server.port).get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "service": "toon-tuner", "version": "0.1.0"}


def test_index_serves_html() -> None:
    with FakeMCPServer("ok") as server:
        response = make_client(server.port).get("/")
    assert response.status_code == 200
    assert "Cartoon-Model-Shader" in response.text


@pytest.mark.parametrize("path", ["/execute", "/eval", "/api/execute", "/api/blender/execute"])
def test_no_arbitrary_code_execution_route(path: str) -> None:
    with FakeMCPServer("ok") as server:
        client = make_client(server.port)
        assert client.get(path).status_code == 404
        assert client.post(path, json={"code": "print(1)"}).status_code == 404


# -- 状态 ---------------------------------------------------------------
def test_status_connected() -> None:
    with FakeMCPServer("ok") as server:
        body = make_client(server.port).get("/api/blender/status").json()
    assert body["ok"] is True
    assert body["status"] == errors.STATUS_CONNECTED
    assert body["target"] == server.target
    assert body["error"] is None


def refuse_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """确定性模拟「9876 未监听」：连接直接抛 ConnectionRefusedError。"""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ConnectionRefusedError(10061, "connection refused")

    monkeypatch.setattr("src.server.blender_mcp.socket.create_connection", boom)


def test_status_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    refuse_connections(monkeypatch)
    body = make_client(9876).get("/api/blender/status").json()
    assert body["ok"] is False
    assert body["status"] == errors.STATUS_DISCONNECTED
    assert body["error"]["code"] == errors.BLENDER_CONNECTION_REFUSED
    assert body["error"]["retryable"] is True


def test_status_protocol_error() -> None:
    with FakeMCPServer("bad_json") as server:
        response = make_client(server.port)
        body = response.get("/api/blender/status").json()
    assert body["status"] == errors.STATUS_PROTOCOL_ERROR
    assert body["error"]["code"] == errors.BLENDER_PROTOCOL_ERROR


def test_status_blender_error() -> None:
    with FakeMCPServer("error") as server:
        body = make_client(server.port).get("/api/blender/status").json()
    assert body["status"] == errors.STATUS_BLENDER_ERROR
    assert body["error"]["code"] == errors.BLENDER_SCRIPT_ERROR


def test_status_timeout() -> None:
    with FakeMCPServer("timeout") as server:
        body = make_client(server.port, response_timeout=0.3).get("/api/blender/status").json()
    assert body["status"] == errors.STATUS_TIMEOUT
    assert body["error"]["code"] == errors.BLENDER_TIMEOUT


def test_reconnect_returns_status_payload() -> None:
    with FakeMCPServer("ok") as server:
        body = make_client(server.port).post("/api/blender/reconnect").json()
    assert body["ok"] is True
    assert body["status"] == errors.STATUS_CONNECTED


# -- 场景 ---------------------------------------------------------------
def test_scene_ok() -> None:
    with FakeMCPServer("ok") as server:
        response = make_client(server.port).get("/api/blender/scene")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["protocol"] == PROBE_SCHEMA
    assert body["blender"]["version"] == FAKE_PROBE_PAYLOAD["blender"]["version"]
    assert body["blender"]["file_name"] == "model.blend"
    assert body["objects"]["total"] == 336
    assert body["role_candidates"][0]["name"] == "model_mesh"


def test_scene_disconnected_returns_503_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    refuse_connections(monkeypatch)
    response = make_client(9876).get("/api/blender/scene")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == errors.BLENDER_CONNECTION_REFUSED
    assert body["error"]["hint"]


def test_scene_script_error_returns_502_envelope() -> None:
    with FakeMCPServer("error") as server:
        response = make_client(server.port).get("/api/blender/scene")
    assert response.status_code == 502
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == errors.BLENDER_SCRIPT_ERROR


def test_error_envelope_shape_is_stable() -> None:
    response = make_client(find_free_port()).get("/api/blender/scene")
    body = response.json()
    assert set(body.keys()) == {"ok", "error"}
    assert set(body["error"].keys()) >= {"code", "message", "retryable", "hint"}
    assert body["ok"] is False


# -- 会话令牌 -----------------------------------------------------------
def test_write_endpoints_reject_missing_token() -> None:
    """没有令牌的写请求一律 401，且不得产生任何副作用。"""
    with FakeMCPServer("ok") as server:
        app = create_app(make_config(server.port))
        response = unauthed(app).post("/api/blender/reconnect")
        assert response.status_code == 401
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["code"] == errors.SESSION_TOKEN_INVALID
        assert body["error"]["details"]["reason"] == "missing"


def test_write_endpoints_reject_wrong_token() -> None:
    with FakeMCPServer("ok") as server:
        app = create_app(make_config(server.port))
        client = unauthed(app)
        response = client.post(
            "/api/blender/reconnect", headers={TOKEN_HEADER: "not-the-real-token"}
        )
        assert response.status_code == 401
        body = response.json()
        assert body["error"]["code"] == errors.SESSION_TOKEN_INVALID
        assert body["error"]["details"]["reason"] == "mismatch"
        # 错误信息里绝不能回显令牌内容
        assert "not-the-real-token" not in response.text


def test_read_endpoints_do_not_require_token() -> None:
    """只读接口不需要令牌，否则用户直接打开页面就什么都看不到。"""
    with FakeMCPServer("ok") as server:
        client = unauthed(create_app(make_config(server.port)))
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/blender/status").status_code == 200


def test_index_injects_token_without_touching_static_file() -> None:
    """令牌只在响应里注入；磁盘上的静态文件里不得出现令牌。"""
    with FakeMCPServer("ok") as server:
        app = create_app(make_config(server.port))
        token = app.state.session_token
        html = unauthed(app).get("/").text
        assert token in html
        assert "window.__TOON_TUNER_TOKEN__" in html

        source = Path("src/web/index.html").read_text(encoding="utf-8")
        assert token not in source
        assert "window.__TOON_TUNER_TOKEN__ = " not in source
