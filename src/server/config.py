"""非敏感配置加载：host / port / 超时。

优先级：内置默认值 < config.example.json < config.local.json < 环境变量。
本阶段（MVP-01）强制仅本地回环，任何远程地址都会被拒绝。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# 仓库根目录（src/server/config.py -> parents[2]）
REPO_ROOT = Path(__file__).resolve().parents[2]

MCP_DEFAULT_HOST = "127.0.0.1"
MCP_DEFAULT_PORT = 9876
MCP_DEFAULT_CONNECT_TIMEOUT = 1.5
MCP_DEFAULT_RESPONSE_TIMEOUT = 5.0

SERVER_DEFAULT_HOST = "127.0.0.1"
SERVER_DEFAULT_PORT = 8765

# MVP 只允许本机回环地址（安全要求）。
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

ENV_PREFIX = "TOON_TUNER_"


class ConfigError(ValueError):
    """配置非法（例如把 MCP host 指到了远程地址）。"""


@dataclass(frozen=True)
class BlenderMCPConfig:
    host: str = MCP_DEFAULT_HOST
    port: int = MCP_DEFAULT_PORT
    connect_timeout_seconds: float = MCP_DEFAULT_CONNECT_TIMEOUT
    response_timeout_seconds: float = MCP_DEFAULT_RESPONSE_TIMEOUT

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class ServerConfig:
    host: str = SERVER_DEFAULT_HOST
    port: int = SERVER_DEFAULT_PORT


@dataclass(frozen=True)
class AppConfig:
    blender_mcp: BlenderMCPConfig
    server: ServerConfig


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigError(f"无法解析配置文件 {path.name}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _env(name: str) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)
    return value if value not in (None, "") else None


def _as_float(value: object, field: str) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} 必须是数字，收到 {value!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{field} 必须为正数，收到 {parsed}")
    return parsed


def _as_port(value: object, field: str) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} 必须是整数端口，收到 {value!r}") from exc
    if not (1 <= parsed <= 65535):
        raise ConfigError(f"{field} 超出端口范围：{parsed}")
    return parsed


def _validate_loopback(host: str, field: str) -> str:
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"{field} 只允许本机回环地址 {sorted(LOOPBACK_HOSTS)}，收到 {host!r}；"
            "MVP 阶段不允许连接或监听远程地址。"
        )
    return host


def load_config(repo_root: Path | None = None) -> AppConfig:
    """按优先级合并配置并做回环校验。"""
    root = repo_root or REPO_ROOT
    merged: dict = {}
    for name in ("config.example.json", "config.local.json"):
        merged.update(_read_json(root / name))

    mcp_raw = merged.get("blender_mcp", {}) or {}
    server_raw = merged.get("server", {}) or {}
    if not isinstance(mcp_raw, dict) or not isinstance(server_raw, dict):
        raise ConfigError("配置文件的 blender_mcp / server 必须是对象")

    mcp = BlenderMCPConfig(
        host=str(_env("MCP_HOST") or mcp_raw.get("host", MCP_DEFAULT_HOST)),
        port=_as_port(_env("MCP_PORT") or mcp_raw.get("port", MCP_DEFAULT_PORT), "blender_mcp.port"),
        connect_timeout_seconds=_as_float(
            _env("MCP_CONNECT_TIMEOUT")
            or mcp_raw.get("connect_timeout_seconds", MCP_DEFAULT_CONNECT_TIMEOUT),
            "blender_mcp.connect_timeout_seconds",
        ),
        response_timeout_seconds=_as_float(
            _env("MCP_RESPONSE_TIMEOUT")
            or mcp_raw.get("response_timeout_seconds", MCP_DEFAULT_RESPONSE_TIMEOUT),
            "blender_mcp.response_timeout_seconds",
        ),
    )
    server = ServerConfig(
        host=str(_env("SERVER_HOST") or server_raw.get("host", SERVER_DEFAULT_HOST)),
        port=_as_port(_env("SERVER_PORT") or server_raw.get("port", SERVER_DEFAULT_PORT), "server.port"),
    )

    _validate_loopback(mcp.host, "blender_mcp.host")
    _validate_loopback(server.host, "server.host")
    return AppConfig(blender_mcp=mcp, server=server)
