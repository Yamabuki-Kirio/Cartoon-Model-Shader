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
    "authed",
    "headers_for",
    "make_config",
    "session_token",
    "unauthed",
]


def make_config(
    port: int,
    *,
    presets_dir: Path | None = None,
    response_timeout: float = 3.0,
    connect_timeout: float = 1.0,
) -> AppConfig:
    """构造测试配置；``presets_dir`` 一律指向临时目录，绝不写进真实的 LOCALAPPDATA。"""
    return AppConfig(
        blender_mcp=BlenderMCPConfig(
            host="127.0.0.1",
            port=port,
            connect_timeout_seconds=connect_timeout,
            response_timeout_seconds=response_timeout,
        ),
        server=ServerConfig(host="127.0.0.1", port=8765),
        presets_dir=presets_dir,
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
