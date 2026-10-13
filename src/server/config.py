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
class RenderConfig:
    """渲染工作流所需的本地固定资源（只由服务端决定，前端绝不可指定路径/脚本）。

    路径解析优先级：``TOON_<NAME>`` 环境变量（与 render_pipeline/开始渲染.py 一致）
    > ``TOON_TUNER_<NAME>`` 环境变量 > config 文件 > 默认 ``None``（由固定脚本回退）。
    ``pipeline_script`` 缺省为仓库内 ``render_pipeline/开始渲染.py``，永不被前端覆盖。
    """

    model_root: Path | None = None
    source_blend: Path | None = None
    blender: Path | None = None
    runtime_dir: Path | None = None
    maps_dir: Path | None = None
    output_dir: Path | None = None
    pipeline_script: Path | None = None

    @property
    def pipeline_script_path(self) -> Path:
        if self.pipeline_script is not None:
            return Path(self.pipeline_script)
        return REPO_ROOT / "render_pipeline" / "开始渲染.py"


@dataclass(frozen=True)
class AppConfig:
    blender_mcp: BlenderMCPConfig
    server: ServerConfig
    #: 预设落盘目录。``None`` 表示用默认位置（``%LOCALAPPDATA%\CartoonModelShader\presets``，
    #: 与环境变量 ``TOON_TUNER_PRESET_DIR`` 的优先级见 ``presets.presets_dir()``）。
    #: 这个值**只影响服务端自己**：接口层从不接受任何路径参数，客户端无法指定目录。
    presets_dir: Path | None = None
    #: v4 前端构建产物目录（``web`` 的上一级 + ``dist``）。
    #: ``None`` 表示用默认位置（``<仓库根>/web``）。同样只影响服务端自己。
    web_dir: Path | None = None
    #: 渲染工作流配置（模型根目录、源工程、Blender、运行目录、映射目录、输出目录）。
    #: 所有路径只由服务端决定，接口层从不接受客户端路径。
    render: RenderConfig = RenderConfig()


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
    presets_raw = merged.get("presets", {}) or {}
    web_raw = merged.get("web", {}) or {}
    if not isinstance(mcp_raw, dict) or not isinstance(server_raw, dict):
        raise ConfigError("配置文件的 blender_mcp / server 必须是对象")
    if not isinstance(presets_raw, dict):
        raise ConfigError("配置文件的 presets 必须是对象")
    if not isinstance(web_raw, dict):
        raise ConfigError("配置文件的 web 必须是对象")

    raw_presets_dir = _env("PRESETS_DIR") or presets_raw.get("dir")
    presets_dir = Path(str(raw_presets_dir)).expanduser() if raw_presets_dir else None
    raw_web_dir = _env("WEB_DIR") or web_raw.get("dir")
    web_dir = Path(str(raw_web_dir)).expanduser() if raw_web_dir else None

    # 渲染工作流：env（TOON_* 优先，兼容固定脚本；其次 TOON_TUNER_*）> config.render
    render_raw = merged.get("render", {}) or {}

    def _env_render(name: str) -> str | None:
        v = os.environ.get("TOON_" + name)
        if v:
            return v
        return _env(name)

    raw_model_root = _env_render("MODEL_ROOT") or render_raw.get("model_root")
    raw_source_blend = _env_render("SOURCE_BLEND") or render_raw.get("source_blend")
    raw_blender = _env_render("BLENDER") or render_raw.get("blender")
    raw_runtime_dir = _env_render("RUNTIME_DIR") or render_raw.get("runtime_dir")
    raw_maps_dir = _env_render("MAPS_DIR") or render_raw.get("maps_dir")
    raw_output_dir = _env_render("OUTPUT_DIR") or render_raw.get("output_dir")
    raw_pipeline_script = _env_render("PIPELINE_SCRIPT") or render_raw.get("pipeline_script")
    render = RenderConfig(
        model_root=Path(str(raw_model_root)).expanduser() if raw_model_root else None,
        source_blend=Path(str(raw_source_blend)).expanduser() if raw_source_blend else None,
        blender=Path(str(raw_blender)).expanduser() if raw_blender else None,
        runtime_dir=Path(str(raw_runtime_dir)).expanduser() if raw_runtime_dir else None,
        maps_dir=Path(str(raw_maps_dir)).expanduser() if raw_maps_dir else None,
        output_dir=Path(str(raw_output_dir)).expanduser() if raw_output_dir else None,
        pipeline_script=Path(str(raw_pipeline_script)).expanduser() if raw_pipeline_script else None,
    )

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
    return AppConfig(
        blender_mcp=mcp, server=server, presets_dir=presets_dir, web_dir=web_dir, render=render
    )
