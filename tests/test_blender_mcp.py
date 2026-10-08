"""TCP 客户端单元测试：成功 / 拒绝 / 超时 / 坏 JSON / 分块 / 探针异常。

不依赖 Blender，也不依赖固定 sleep（分块用例用「脚本化 socket」确定性构造）。
"""

from __future__ import annotations

import json

import pytest

from src.server import errors
from src.server.blender_mcp import BlenderMCPClient
from src.server.config import BlenderMCPConfig
from src.server.scene_probe import PROBE_CODE, PROBE_MARKER
from tests.fake_mcp_server import (
    FAKE_PROBE_PAYLOAD,
    FakeMCPServer,
    build_probe_stdout,
    build_success_envelope,
    find_free_port,
)


def make_config(port: int, *, connect: float = 0.5, response: float = 0.6) -> BlenderMCPConfig:
    return BlenderMCPConfig(
        host="127.0.0.1",
        port=port,
        connect_timeout_seconds=connect,
        response_timeout_seconds=response,
    )


class ScriptedSocket:
    """按脚本逐段返回字节，用于确定性验证「无换行分隔符」下的增量解析。"""

    def __init__(self, pieces: list[bytes]) -> None:
        self._pieces = list(pieces)
        self.sent = b""
        self.closed = False

    def settimeout(self, _timeout: float) -> None:  # noqa: D401 - 桩
        return None

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _size: int) -> bytes:
        if self._pieces:
            return self._pieces.pop(0)
        return b""  # 对端已关闭

    def close(self) -> None:
        self.closed = True


# -- 正常路径 -------------------------------------------------------------
def test_ping_ok() -> None:
    with FakeMCPServer("ok") as server:
        client = BlenderMCPClient(make_config(server.port))
        assert client.ping() == {"pong": True}


def test_execute_code_ok() -> None:
    with FakeMCPServer("ok") as server:
        client = BlenderMCPClient(make_config(server.port))
        stdout = client.execute_code(PROBE_CODE)
        assert PROBE_MARKER in stdout
        assert json.loads(stdout.split(PROBE_MARKER, 1)[1])["scene"]["name"] == "Scene"


def test_unicode_roundtrip() -> None:
    payload = dict(FAKE_PROBE_PAYLOAD)
    payload["scene"] = dict(payload["scene"], name="场景·测试")
    payload["role_candidates"] = [
        {"name": "模型_测试", "polygons": 10, "material_slots": 2, "visible": True, "hide_render": False}
    ]
    with FakeMCPServer("ok", probe_payload=payload) as server:
        client = BlenderMCPClient(make_config(server.port))
        stdout = client.execute_code(PROBE_CODE)
        parsed = json.loads(stdout.split(PROBE_MARKER, 1)[1])
        assert parsed["scene"]["name"] == "场景·测试"
        assert parsed["role_candidates"][0]["name"] == "模型_测试"


# -- 失败路径 -------------------------------------------------------------
def test_connection_refused_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入 ConnectionRefusedError，确定性验证错误码映射（不依赖本机网络行为）。"""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ConnectionRefusedError(10061, "connection refused")

    monkeypatch.setattr("src.server.blender_mcp.socket.create_connection", boom)
    client = BlenderMCPClient(make_config(9999))
    with pytest.raises(errors.BlenderConnectionRefused) as exc:
        client.ping()
    assert exc.value.code == errors.BLENDER_CONNECTION_REFUSED
    assert exc.value.retryable is True


def test_closed_port_is_reported_as_connection_failure() -> None:
    """真实空闲端口：有的环境返回拒绝，有的环境静默丢包（表现为超时）。

    两者都必须被归类为「连不上」，绝不能是未捕获异常。
    """
    client = BlenderMCPClient(make_config(find_free_port(), connect=0.4, response=0.4))
    with pytest.raises(errors.ToonTunerError) as exc:
        client.ping()
    assert exc.value.code in {errors.BLENDER_CONNECTION_REFUSED, errors.BLENDER_TIMEOUT}


def test_response_timeout() -> None:
    with FakeMCPServer("timeout") as server:
        client = BlenderMCPClient(make_config(server.port, connect=0.5, response=0.35))
        with pytest.raises(errors.BlenderTimeout) as exc:
            client.ping()
    assert exc.value.code == errors.BLENDER_TIMEOUT


def test_non_json_response() -> None:
    with FakeMCPServer("bad_json") as server:
        client = BlenderMCPClient(make_config(server.port, connect=0.5, response=0.6))
        with pytest.raises(errors.BlenderProtocolError) as exc:
            client.ping()
    assert exc.value.code == errors.BLENDER_PROTOCOL_ERROR


def test_chunked_response_is_reassembled() -> None:
    with FakeMCPServer("chunked", chunk_size=11) as server:
        client = BlenderMCPClient(make_config(server.port))
        stdout = client.execute_code(PROBE_CODE)
        assert json.loads(stdout.split(PROBE_MARKER, 1)[1])["protocol"] == "toon-tuner-scene-probe/1"


def test_probe_script_error_is_reported() -> None:
    with FakeMCPServer("error") as server:
        client = BlenderMCPClient(make_config(server.port))
        with pytest.raises(errors.BlenderScriptError) as exc:
            client.execute_code(PROBE_CODE)
    assert exc.value.code == errors.BLENDER_SCRIPT_ERROR
    assert exc.value.details.get("exception_type") == "RuntimeError"
    assert "probe failed" in str(exc.value)


# -- 增量解析（确定性，不依赖真实网络分片）--------------------------------
def _patch_socket(monkeypatch: pytest.MonkeyPatch, sock: ScriptedSocket) -> None:
    monkeypatch.setattr(
        "src.server.blender_mcp.socket.create_connection", lambda *args, **kwargs: sock
    )


def test_incremental_parse_across_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = build_probe_stdout()
    body = build_success_envelope(stdout)
    # 切成 3 段，第二段落在 JSON 中间，客户端必须先累积再解析
    third = len(body) // 3
    sock = ScriptedSocket([body[:third], body[third : third * 2], body[third * 2 :]])
    _patch_socket(monkeypatch, sock)

    client = BlenderMCPClient(make_config(9999))
    assert client.execute_code(PROBE_CODE) == stdout
    assert sock.closed is True


def test_truncated_response_raises_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    body = build_success_envelope(build_probe_stdout())
    sock = ScriptedSocket([body[:20], body[20:40]])  # 之后 recv 返回 b"" → 截断
    _patch_socket(monkeypatch, sock)

    client = BlenderMCPClient(make_config(9999))
    with pytest.raises(errors.BlenderProtocolError) as exc:
        client.execute_code(PROBE_CODE)
    assert exc.value.code == errors.BLENDER_PROTOCOL_ERROR
