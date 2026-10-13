"""公共测试支撑：**统一携带会话令牌**的 TestClient。

所有写接口（本文件里全部 POST）都强制校验 ``X-Toon-Tuner-Token``，因此测试必须经由
同一个入口构造客户端。这样做的目的不是「让测试更容易过」，而是：

* 令牌只在一处注入，避免每个测试文件各写一份、写着写着有人顺手把它去掉；
* 仍然保留 ``unauthed()`` 用于**明确验证拒绝路径**（缺失 / 错误令牌一律 401），
  拒绝测试与正常路径测试共用同一份装配代码，谁也不会掩盖谁。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.server.config import AppConfig, BlenderMCPConfig, ServerConfig
from src.server.security import TOKEN_HEADER

__all__ = [
    "TOKEN_HEADER",
    "FakeBlenderGateway",
    "authed",
    "headers_for",
    "make_config",
    "session_token",
    "unauthed",
    "write_fake_dist",
]


class FakeBlenderGateway:
    """伪造 MCP 连接状态（仅供测试注入 ``create_app(gateway=...)``）。

    存在的理由：渲染环境诊断与「Blender 未连接不得创建渲染任务」（规格 §5.4）
    都依赖 ``gateway.status()``。不注入假 gateway 的话，这两条路径在 CI 里
    （没有真 Blender）永远只能覆盖「未连接」那一半。
    """

    def __init__(self, *, connected: bool = True, message: str | None = None) -> None:
        self.connected = connected
        self.message = message
        self.status_calls = 0

    async def status(self) -> dict:
        self.status_calls += 1
        if self.connected:
            return {
                "ok": True,
                "status": "connected",
                "target": "127.0.0.1:9876",
                "checked_at": "2026-10-13T00:00:00+08:00",
                "latency_ms": 1.0,
                "error": None,
            }
        return {
            "ok": False,
            "status": "refused",
            "target": "127.0.0.1:9876",
            "checked_at": "2026-10-13T00:00:00+08:00",
            "latency_ms": None,
            "error": {"code": "BLENDER_CONNECTION_REFUSED", "message": self.message or "连接被拒绝"},
        }


def make_config(
    port: int,
    *,
    presets_dir: Path | None = None,
    web_dir: Path | None = None,
    response_timeout: float = 3.0,
    connect_timeout: float = 1.0,
) -> AppConfig:
    """构造测试配置。

    ``presets_dir`` / ``web_dir`` 一律由调用方指向临时目录，绝不写进（也绝不读）
    开发者真实的 ``%LOCALAPPDATA%`` 与仓库里的 ``web/dist``。
    """
    return AppConfig(
        blender_mcp=BlenderMCPConfig(
            host="127.0.0.1",
            port=port,
            connect_timeout_seconds=connect_timeout,
            response_timeout_seconds=response_timeout,
        ),
        server=ServerConfig(host="127.0.0.1", port=8765),
        presets_dir=presets_dir,
        web_dir=web_dir,
    )


def session_token(app: FastAPI) -> str:
    """从应用状态取本次进程的会话令牌（仅测试使用）。"""
    return str(app.state.session_token)


def headers_for(app: FastAPI) -> dict[str, str]:
    return {TOKEN_HEADER: session_token(app)}


def authed(app: FastAPI) -> TestClient:
    """带会话令牌的 TestClient：所有请求自动携带令牌。"""
    return TestClient(app, headers=headers_for(app))


def unauthed(app: FastAPI) -> TestClient:
    """不带任何令牌的 TestClient（只用于校验拒绝路径）。"""
    return TestClient(app)


#: 伪造构建产物里 `index.html` 的令牌占位（与 `src/server/security.py` 同一份约定）。
TOKEN_PLACEHOLDER = "<!--TOON_TUNER_TOKEN-->"


def write_fake_dist(web_dir: Path) -> Path:
    """在临时目录里伪造一份 `web/dist`，返回该目录。

    为什么所有托管类用例都该用它，而不是读仓库里的 `web/dist`：

    * **pytest 不依赖 Node**（见 `.github/workflows/test.yml`）—— CI 的 Python job
      里根本没有 `dist/`，读真实产物会让用例在那里直接失败；
    * 断言「资源逐字节直出」需要一个我们**确切知道**内容的文件，真实产物的内容
      随构建而变；
    * 令牌注入用例需要一个**确定**的占位注释与一条**确定的**资源引用。

    形状与真实产物一致：`dist/index.html` + `dist/assets/{index-<hash>.js,.css,logo.svg}`。
    """
    dist = web_dir / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(
        "<!DOCTYPE html><html><head>"
        f"{TOKEN_PLACEHOLDER}"
        '<script type="module" crossorigin src="/assets/index-abc123.js"></script>'
        '<link rel="stylesheet" crossorigin href="/assets/index-abc123.css">'
        '</head><body><div id="app"></div></body></html>',
        encoding="utf-8",
    )
    (assets / "index-abc123.js").write_text(
        "window.__TOON_TUNER_TOKEN__;export const x = 1;", encoding="utf-8"
    )
    (assets / "index-abc123.css").write_text("body{margin:0}", encoding="utf-8")
    (assets / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    return dist
