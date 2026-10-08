"""Blender MCP TCP 客户端（纯传输层，不含业务逻辑）。

协议事实（依据 addon `blender_mcp.py` 实测）：

* 请求：``json.dumps({"type": ..., "params": {...}}).encode("utf-8")``；
  服务端累积字节后做 ``json.loads``，因此尾部补一个换行是安全的。
* 响应：服务端 ``client.sendall(json.dumps(response).encode("utf-8"))``——
  **不带换行分隔符，也不关闭连接**。客户端必须「累积缓冲 + 增量 json.loads」，
  不能依赖行分隔，也不能依赖 EOF 判断结束。
* 外壳：``{"status": "success", "result": {...}}`` 或 ``{"status": "error", "message": "..."}``。
* ``execute_code`` 的 result 形如 ``{"executed": true, "result": "<captured stdout>"}``。

不使用固定 ``sleep``：连接与读取都用 socket 超时 + ``time.monotonic()`` 截止时间。
"""

from __future__ import annotations

import errno
import json
import socket
import time
from typing import Any

from . import errors
from .config import BlenderMCPConfig
from .redact import redact

# 响应上限，防止异常对端灌爆内存
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# 连接阶段可视为「连不上」的 errno
_REFUSED_ERRNOS = {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}


class BlenderMCPClient:
    """短连接的 MCP 客户端。每次请求独立建连、读一条响应后关闭。"""

    def __init__(self, config: BlenderMCPConfig) -> None:
        self._config = config

    @property
    def config(self) -> BlenderMCPConfig:
        return self._config

    @property
    def target(self) -> str:
        return self._config.target

    # -- 公开能力 ---------------------------------------------------------
    def ping(self) -> dict[str, Any]:
        """最小存活探测：不触碰任何 bpy 数据。"""
        response = self.request({"type": "ping"})
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def execute_code(self, code: str) -> str:
        """执行固定只读探针代码，返回其捕获到的 stdout（字符串）。"""
        response = self.request({"type": "execute_code", "params": {"code": code}})
        captured = self._extract_captured_output(response.get("result"))
        return captured

    # -- 传输 -------------------------------------------------------------
    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = self._config
        deadline = time.monotonic() + cfg.response_timeout_seconds
        sock = self._open_socket()
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
            try:
                sock.sendall(body)
            except OSError as exc:
                raise errors.BlenderProtocolError(
                    f"向 Blender MCP 发送请求失败：{exc.__class__.__name__}"
                ) from exc
            response = self._read_response(sock, deadline)
        finally:
            try:
                sock.close()
            except OSError:
                pass

        return self._validate_envelope(response)

    def _open_socket(self) -> socket.socket:
        cfg = self._config
        try:
            return socket.create_connection(
                (cfg.host, cfg.port), timeout=cfg.connect_timeout_seconds
            )
        except (socket.timeout, TimeoutError) as exc:
            raise errors.BlenderTimeout(cfg.target, "建立连接", cfg.connect_timeout_seconds) from exc
        except ConnectionRefusedError as exc:
            raise errors.BlenderConnectionRefused(cfg.target) from exc
        except OSError as exc:
            if exc.errno in _REFUSED_ERRNOS:
                raise errors.BlenderConnectionRefused(cfg.target) from exc
            raise errors.BlenderConnectionRefused(
                cfg.target
            ) from exc

    def _read_response(self, sock: socket.socket, deadline: float) -> Any:
        cfg = self._config
        buffer = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise errors.BlenderTimeout(
                    cfg.target, "等待响应", cfg.response_timeout_seconds
                )
            try:
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError) as exc:
                # 收到部分数据后再超时 = 响应被截断
                if buffer:
                    raise errors.BlenderProtocolError(
                        "响应超时前被截断：只收到不完整的 JSON。",
                        details={"received_bytes": len(buffer)},
                    ) from exc
                raise errors.BlenderTimeout(
                    cfg.target, "等待响应", cfg.response_timeout_seconds
                ) from exc
            except OSError as exc:
                raise errors.BlenderProtocolError(
                    f"读取响应失败：{exc.__class__.__name__}"
                ) from exc

            if not chunk:
                if not buffer:
                    raise errors.BlenderProtocolError("Blender MCP 在返回任何数据前关闭了连接。")
                raise errors.BlenderProtocolError(
                    "响应被截断：连接在 JSON 结束前关闭。",
                    details={"received_bytes": len(buffer)},
                )

            buffer += chunk
            if len(buffer) > MAX_RESPONSE_BYTES:
                raise errors.BlenderProtocolError(
                    f"响应超过 {MAX_RESPONSE_BYTES} 字节上限，已中止读取。"
                )

            try:
                text = buffer.decode("utf-8")
            except UnicodeDecodeError:
                # 多字节字符被 TCP 分块截断，继续累积
                continue

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # 尚未收完，继续累积（这正是「无换行分隔符」协议下的正确做法）
                continue

    # -- 响应解析 ---------------------------------------------------------
    @staticmethod
    def _validate_envelope(response: Any) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise errors.BlenderProtocolError(
                "响应不是 JSON 对象。",
                details={"received_type": type(response).__name__},
            )

        status = response.get("status")
        if status == "error":
            message, details = BlenderMCPClient._parse_error_message(response.get("message"))
            raise errors.BlenderScriptError(message, details=details)
        if status != "success":
            raise errors.BlenderUnexpectedResponse(
                "响应缺少可识别的 status 字段。",
                details={"response_keys": sorted(map(str, response.keys()))},
            )
        return response

    @staticmethod
    def _parse_error_message(raw: Any) -> tuple[str, dict[str, Any]]:
        """``execute_code`` 的异常会把 type/message/traceback 序列化成 JSON 字符串。"""
        details: dict[str, Any] = {}
        message = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                message = str(parsed.get("message") or raw)
                if parsed.get("exception_type"):
                    details["exception_type"] = str(parsed["exception_type"])
                if parsed.get("traceback"):
                    details["traceback"] = redact(str(parsed["traceback"]))
        return redact(message), details

    @staticmethod
    def _extract_captured_output(result: Any) -> str:
        """兼容 result 是字符串 / 对象 / 嵌套对象三种形态。"""
        captured: Any = None
        if isinstance(result, dict):
            captured = result.get("result")
        elif isinstance(result, str):
            captured = result

        if captured is None:
            raise errors.BlenderUnexpectedResponse(
                "execute_code 响应中缺少可识别的 result 字段。",
                details={"result_type": type(result).__name__},
            )
        if isinstance(captured, str):
            return captured
        return json.dumps(captured, ensure_ascii=False)
