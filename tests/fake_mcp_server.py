"""假 Blender MCP 服务器：无需安装 Blender 即可测试客户端与 API。

复刻真实 addon 的关键行为：
* 请求累积字节后 ``json.loads``（尾部换行无害）；
* 响应 ``sendall`` 且**不带换行分隔符、不关闭连接**；
* 外壳为 ``{"status": "success"|"error", ...}``；
* ``execute_code`` 的 result 为 ``{"executed": true, "result": "<stdout>"}``。
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

from src.server.scene_probe import PROBE_MARKER

# 全部为虚构占位数据，不含任何真实本机路径
FAKE_PROBE_PAYLOAD: dict[str, Any] = {
    "protocol": "toon-tuner-scene-probe/1",
    "blender": {
        "version": "5.2.1 LTS",
        "file_path": "demo_project/model.blend",
        "is_saved": True,
    },
    "scene": {
        "name": "Scene",
        "render_engine": "BLENDER_EEVEE_NEXT",
        "resolution": [1080, 1980, 100],
        "frame_current": 1,
        "camera": "Camera",
    },
    "objects": {
        "total": 336,
        "mesh_count": 139,
        "visible_mesh_count": 1,
        "light_count": 29,
        "camera_count": 1,
    },
    "role_candidates": [
        {
            "name": "model_mesh",
            "polygons": 35544,
            "material_slots": 19,
            "visible": True,
            "hide_render": False,
        }
    ],
}


def build_success_envelope(stdout: str) -> bytes:
    payload = {"status": "success", "result": {"executed": True, "result": stdout}}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def build_probe_stdout(payload: dict[str, Any] | None = None) -> str:
    body = payload if payload is not None else FAKE_PROBE_PAYLOAD
    return PROBE_MARKER + json.dumps(body, ensure_ascii=False)


class FakeMCPServer:
    """模式：ok / timeout / bad_json / error / chunked。"""

    def __init__(
        self,
        mode: str = "ok",
        probe_payload: dict[str, Any] | None = None,
        chunk_size: int = 24,
    ) -> None:
        self.mode = mode
        self.probe_payload = probe_payload
        self.chunk_size = chunk_size
        self.host = "127.0.0.1"
        self.port = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> "FakeMCPServer":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.bind((self.host, 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.host, self.port = self._sock.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def __enter__(self) -> "FakeMCPServer":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"

    # -- 内部 -------------------------------------------------------------
    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        buffer = bytearray()
        try:
            client.settimeout(2.0)
            while not self._stop.is_set():
                try:
                    data = client.recv(8192)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                buffer += data
                try:
                    command = json.loads(buffer.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                buffer = bytearray()
                if not self._respond(client, command):
                    break
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _respond(self, client: socket.socket, command: dict[str, Any]) -> bool:
        """返回 False 表示发送后应关闭连接。"""
        if self.mode == "timeout":
            return True  # 永远不回应，用于触发客户端超时

        if self.mode == "bad_json":
            client.sendall(b"<html>not json</html>")
            return False  # 发完即断，模拟「非 JSON 响应」

        if self.mode == "error":
            body = json.dumps(
                {
                    "status": "error",
                    "message": json.dumps(
                        {
                            "exception_type": "RuntimeError",
                            "message": "probe failed",
                            "traceback": 'Traceback (most recent call last):\n  File "<probe>", line 3\nRuntimeError: probe failed',
                        }
                    ),
                }
            ).encode("utf-8")
            client.sendall(body)
            return True

        cmd_type = command.get("type")
        if cmd_type == "ping":
            body = json.dumps({"status": "success", "result": {"pong": True}}).encode("utf-8")
        elif cmd_type == "execute_code":
            body = build_success_envelope(build_probe_stdout(self.probe_payload))
        else:
            body = json.dumps(
                {"status": "error", "message": f"Unknown command type: {cmd_type}"}
            ).encode("utf-8")

        if self.mode == "chunked":
            for start in range(0, len(body), self.chunk_size):
                client.sendall(body[start : start + self.chunk_size])
        else:
            client.sendall(body)
        return True


def find_free_port() -> int:
    """返回一个当前必定无人监听的端口（用于「连接被拒绝」测试）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port
