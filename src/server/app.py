"""FastAPI 应用：只读连接闭环（MVP-01）。

路由：
* ``GET  /``                       单页界面
* ``GET  /api/health``             仅检查本地控制服务
* ``GET  /api/blender/status``     快速探测端口与协议
* ``GET  /api/blender/scene``      执行完整只读场景探针
* ``POST /api/blender/reconnect``  重置连接状态并立即重新检测

安全边界：
* 只监听 127.0.0.1（见 config 回环校验）。
* 不提供任何接受任意 Python 的接口；Blender 侧代码只能来自内置探针模板。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import SERVICE_NAME, __version__, errors, scene_probe
from .blender_mcp import BlenderMCPClient
from .config import AppConfig, ConfigError, load_config
from .models import (
    BlenderStatusResponse,
    ErrorResponse,
    HealthResponse,
    SceneResponse,
)
from .redact import redact

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

logger = logging.getLogger("toon_tuner")


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _error_detail(exc: errors.ToonTunerError) -> dict[str, Any]:
    return exc.to_payload()["error"]


class BlenderGateway:
    """封装对 Blender MCP 的访问。

    底层是短连接，因此这里不做长连接管理；`reconnect` 只承担「清空状态 +
    立即重新检测」的语义，保证前端调用契约稳定。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._generation = 0

    @property
    def target(self) -> str:
        return self._config.blender_mcp.target

    def reset(self) -> int:
        """清空连接状态，返回新的连接代次。"""
        self._generation += 1
        return self._generation

    def _client(self) -> BlenderMCPClient:
        return BlenderMCPClient(self._config.blender_mcp)

    async def status(self) -> dict[str, Any]:
        """返回 status payload（永不抛异常）。"""
        async with self._lock:
            checked_at = _now_iso()
            try:
                latency = await asyncio.to_thread(self._ping_sync)
            except errors.ToonTunerError as exc:
                return {
                    "ok": False,
                    "status": errors.status_for_error(exc.code),
                    "target": self.target,
                    "checked_at": checked_at,
                    "latency_ms": None,
                    "error": _error_detail(exc),
                }
            return {
                "ok": True,
                "status": errors.STATUS_CONNECTED,
                "target": self.target,
                "checked_at": checked_at,
                "latency_ms": round(latency, 1),
                "error": None,
            }

    def _ping_sync(self) -> float:
        client = self._client()
        started = time.perf_counter()
        client.ping()
        return (time.perf_counter() - started) * 1000.0

    async def scene(self) -> dict[str, Any]:
        """执行只读探针；失败时抛出 ToonTunerError，由全局处理器转成错误响应。"""
        async with self._lock:
            checked_at = _now_iso()
            summary = await asyncio.to_thread(self._scene_sync)
            payload = {
                "ok": True,
                "status": errors.STATUS_CONNECTED,
                "target": self.target,
                "checked_at": checked_at,
                **summary,
            }
            return payload

    def _scene_sync(self) -> dict[str, Any]:
        return scene_probe.collect_scene(self._client())

    async def reconnect(self) -> dict[str, Any]:
        self.reset()
        return await self.status()


def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(
        title="Cartoon-Model-Shader 本地控制服务",
        version=__version__,
        description="MVP-01：只读连接 Blender MCP 9876 并展示场景摘要。",
    )
    app.state.config = cfg
    gateway = BlenderGateway(cfg)
    app.state.gateway = gateway

    # -- 全局异常处理：把内部异常统一成稳定错误码 -------------------------
    @app.exception_handler(errors.ToonTunerError)
    async def _handle_known(_: Request, exc: errors.ToonTunerError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(ConfigError)
    async def _handle_config(_: Request, exc: ConfigError) -> JSONResponse:
        err = errors.ToonTunerError(errors.CONFIG_INVALID, redact(str(exc)))
        return JSONResponse(status_code=err.http_status, content=err.to_payload())

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常")
        err = errors.ToonTunerError(errors.INTERNAL_ERROR, redact(str(exc)) or "内部错误")
        return JSONResponse(status_code=err.http_status, content=err.to_payload())

    # -- API --------------------------------------------------------------
    @app.get("/api/health", response_model=HealthResponse, tags=["system"])
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": SERVICE_NAME, "version": __version__}

    @app.get(
        "/api/blender/status",
        response_model=BlenderStatusResponse,
        responses={200: {"model": BlenderStatusResponse}},
        tags=["blender"],
    )
    async def blender_status() -> dict[str, Any]:
        return await gateway.status()

    @app.get(
        "/api/blender/scene",
        response_model=SceneResponse,
        responses={502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
        tags=["blender"],
    )
    async def blender_scene() -> dict[str, Any]:
        return await gateway.scene()

    @app.post(
        "/api/blender/reconnect",
        response_model=BlenderStatusResponse,
        tags=["blender"],
    )
    async def blender_reconnect() -> dict[str, Any]:
        return await gateway.reconnect()

    # -- 静态页面 ---------------------------------------------------------
    index_file = WEB_DIR / "index.html"
    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        if index_file.is_file():
            return FileResponse(index_file)
        return JSONResponse(
            status_code=500,
            content=errors.ToonTunerError(
                errors.INTERNAL_ERROR, "前端页面缺失（src/web/index.html）"
            ).to_payload(),
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")


if __name__ == "__main__":
    main()
