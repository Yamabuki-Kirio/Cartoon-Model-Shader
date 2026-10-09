"""FastAPI 应用：本地卡通渲染调参台。

路由：
* ``GET  /``                       单页界面
* ``GET  /api/health``             仅检查本地控制服务
* ``GET  /api/blender/status``     快速探测端口与协议
* ``GET  /api/blender/scene``      执行完整只读场景探针
* ``POST /api/blender/reconnect``  重置连接状态并立即重新检测
* ``GET  /api/framing/context``    当前帧 / 相机 / 相机动画 / 角色是否完整入画
* ``GET  /api/params/schema``      L0 参数表
* ``GET  /api/color/looks``        某视图下 Blender 真正接受的 look 档位（依赖枚举）
* ``POST /api/color/looks/refresh`` 重新扫描 look 能力表
* ``POST /api/session/baseline``   采集内存基线（含取景快照）+ 首张预览
* ``POST /api/session/restore``    回滚到基线（走同一套依赖映射）
* ``POST /api/preview``            提交草稿 + 取景方式，产出预览任务
* ``GET  /api/jobs/{id}``          任务状态
* ``GET  /api/preview/{id}``       预览图（HTTP 端点，不暴露本机路径）
* ``GET  /api/presets``            本地预设列表 + 运行设置白名单
* ``GET  /api/presets/{id}``       读取单个预设
* ``POST /api/presets``            新建预设
* ``PUT  /api/presets/{id}``       覆盖保存预设（身份与创建时间保留）
* ``POST /api/presets/{id}/rename``      重命名（身份不变）
* ``POST /api/presets/{id}/duplicate``   复制为新预设
* ``DELETE /api/presets/{id}``     删除预设

安全边界：
* 只监听 127.0.0.1（见 config 回环校验）。
* 不提供任何接受任意 Python 的接口；Blender 侧代码只能来自内置模板。
* 自动取景只用**临时预览相机**，绝不改动用户相机；渲染后必定恢复 ``scene.camera``。
* look 是依赖 ``view_transform`` 的枚举：后端在**下发脚本前**完成依赖校验，
  非法组合返回稳定错误 ``INVALID_DEPENDENT_ENUM``，不退化成 ``BLENDER_SCRIPT_ERROR``。
* 预设存在 ``%LOCALAPPDATA%`` 下（不进仓库），写入前扫描并**拒绝**任何本机路径、
  模型/贴图路径与凭据；接口只回可展示位置，不回本机绝对路径。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (
    SERVICE_NAME,
    __version__,
    errors,
    framing as framing_module,
    params as params_module,
    presets as presets_module,
    scene_probe,
)
from .binder import BlenderBinder, preview_dir, preview_png_name
from .blender_mcp import BlenderMCPClient
from .config import AppConfig, ConfigError, load_config
from .models import (
    BaselineRequest,
    BaselineResponse,
    BlenderStatusResponse,
    ColorLooksResponse,
    ErrorResponse,
    FramingContextResponse,
    HealthResponse,
    JobResponse,
    ParamSchemaResponse,
    PresetDeleteResponse,
    PresetDetailResponse,
    PresetDuplicateRequest,
    PresetListResponse,
    PresetRenameRequest,
    PresetSaveRequest,
    PreviewSubmitRequest,
    PreviewSubmitResponse,
    RestoreResponse,
    SceneResponse,
)
from .presets import PresetStore
from .redact import redact
from .session import PreviewService, preview_url_for

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


def _preset_public(preset: dict[str, Any]) -> dict[str, Any]:
    """预设的对外形状：摘要 + 逐参数明细（字段名与存储格式一致）。"""
    return {**presets_module.summary_of(preset), "parameters": dict(preset.get("parameters") or {})}


def create_app(
    config: AppConfig | None = None, *, preset_store: PresetStore | None = None
) -> FastAPI:
    cfg = config or load_config()
    gateway = BlenderGateway(cfg)
    binder = BlenderBinder(cfg.blender_mcp)
    preview = PreviewService(binder)
    store = preset_store or PresetStore()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await preview.start()
        try:
            yield
        finally:
            await preview.stop()

    app = FastAPI(
        title="Cartoon-Model-Shader 本地控制服务",
        version=__version__,
        description="只读场景摘要 + L0 曝光调参与无污染预览（连接 Blender MCP 9876）。",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.gateway = gateway
    app.state.preview = preview
    app.state.presets = store

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

    # -- MVP-02：曝光调参 -------------------------------------------------
    @app.get("/api/params/schema", response_model=ParamSchemaResponse, tags=["params"])
    async def params_schema() -> dict[str, Any]:
        baseline = preview.baseline_public()
        options = dict(baseline["options"]) if baseline else {}
        schema = params_module.public_schema(options)
        return {
            "ok": True,
            "schema_version": schema["schema"],
            "groups": schema["groups"],
        }

    @app.post("/api/session/baseline", response_model=BaselineResponse, tags=["session"])
    async def create_baseline(body: BaselineRequest | None = None) -> dict[str, Any]:
        """采集内存基线（含取景快照），并**立即**用基线参数创建一次预览任务。

        基线本身不产生画面，之前前端因此停在「可开始调参」却看不到图。
        这里把「首张基线预览」纳入同一响应：返回的 ``job_id`` /
        ``preview_url`` 供前端轮询与取图。请求体可选，用于指定首张预览的取景方式。
        """
        options = framing_module.validate_options(
            body.framing.model_dump() if body is not None and body.framing is not None else None
        )
        baseline = await preview.capture_baseline()
        job = await preview.submit({}, options)
        return {
            "ok": True,
            **baseline,
            "job_id": job.job_id,
            "job_status": job.status,
            "preview_url": preview_url_for(job.job_id),
        }

    @app.get("/api/session/baseline", tags=["session"])
    async def read_baseline() -> Any:
        baseline = preview.baseline_public()
        if baseline is None:
            raise errors.ToonTunerError(errors.NO_BASELINE, "尚未建立内存基线。")
        return {"ok": True, **baseline}

    @app.get(
        "/api/framing/context",
        response_model=FramingContextResponse,
        responses={502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
        tags=["framing"],
    )
    async def framing_context() -> dict[str, Any]:
        """当前帧 / 当前相机 / 相机是否有动画 / 角色包围盒是否完整落在画面内。"""
        context = await preview.read_framing_context()
        return {
            "ok": True,
            **context,
            "framing_modes": {
                "default": framing_module.DEFAULT_MODE,
                "default_margin": framing_module.DEFAULT_MARGIN,
                "margin_min": framing_module.MARGIN_MIN,
                "margin_max": framing_module.MARGIN_MAX,
                "modes": [
                    {
                        "id": mode,
                        "label": label,
                        "uses_temporary_camera": mode in framing_module.AUTO_MODES,
                    }
                    for mode, label in framing_module.FRAMING_MODES.items()
                ],
            },
        }

    @app.get(
        "/api/color/looks",
        response_model=ColorLooksResponse,
        responses={502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
        tags=["color"],
    )
    async def color_looks(
        view_transform: str | None = None, identifier: str | None = None
    ) -> dict[str, Any]:
        """某个视图变换下 Blender **真正接受**的 look 档位。

        look 是依赖枚举：``getLookNames()`` 是 OCIO 全局名单，不是当前视图的合法集合。
        这里返回的是 ``{view_transform: [{value, label}]}`` 能力表；建立基线时已一次扫出，
        因此前端切换视图变换时通常是**零 Blender 调用**的。
        """
        payload = await preview.read_look_capability(view_transform, identifier)
        return {"ok": True, **payload}

    @app.post(
        "/api/color/looks/refresh",
        response_model=ColorLooksResponse,
        responses={502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
        tags=["color"],
    )
    async def color_looks_refresh() -> dict[str, Any]:
        """重新扫描 look 能力表（换了 OCIO 配置时用）。只改能力表，不动工程取值。"""
        payload = await preview.refresh_look_capability()
        return {"ok": True, **payload}

    @app.post("/api/session/restore", response_model=RestoreResponse, tags=["session"])
    async def restore_baseline() -> dict[str, Any]:
        return await preview.restore_baseline()

    @app.post("/api/preview", response_model=PreviewSubmitResponse, tags=["preview"])
    async def submit_preview(body: PreviewSubmitRequest) -> dict[str, Any]:
        options = framing_module.validate_options(
            body.framing.model_dump() if body.framing is not None else None
        )
        job = await preview.submit(body.draft, options)
        return {
            "ok": True,
            "job_id": job.job_id,
            "seq": job.seq,
            "status": job.status,
            "framing": framing_module.describe_options(dict(job.framing)),
        }

    @app.get("/api/jobs/{job_id}", response_model=JobResponse, tags=["preview"])
    async def read_job(job_id: str) -> dict[str, Any]:
        job = preview.get_job(job_id)
        if job is None:
            raise errors.ToonTunerError(errors.JOB_NOT_FOUND, f"任务不存在：{job_id}")
        return job.to_public()

    @app.get("/api/preview/{job_id}", tags=["preview"], response_class=FileResponse)
    async def preview_image(job_id: str) -> Any:
        job = preview.get_job(job_id)
        if job is None:
            raise errors.ToonTunerError(errors.JOB_NOT_FOUND, f"任务不存在：{job_id}")
        if job.status != "done":
            raise errors.ToonTunerError(
                errors.JOB_NOT_FOUND, f"任务尚未产出预览图（当前状态：{job.status}）。"
            )
        path = preview_dir() / preview_png_name(job.job_id)
        if not path.is_file():
            raise errors.ToonTunerError(
                errors.PREVIEW_FAILED, "预览图文件已不存在，请重新提交预览。"
            )
        return FileResponse(path, media_type="image/png", filename=path.name)

    # -- MVP-03：本地预设 -------------------------------------------------
    @app.get("/api/presets", response_model=PresetListResponse, tags=["presets"])
    async def list_presets() -> dict[str, Any]:
        """列出本地预设。

        预设存放在用户目录（``%LOCALAPPDATA%\\CartoonModelShader\\presets``），
        **不进仓库**，也不把浏览器 ``localStorage`` 当唯一存储。
        读不动的文件不会被静默忽略，而是进 ``skipped`` 并附错误码与原因。
        """
        payload = await asyncio.to_thread(store.list)
        return {"ok": True, **payload}

    @app.get(
        "/api/presets/{preset_id}",
        response_model=PresetDetailResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["presets"],
    )
    async def read_preset(preset_id: str) -> dict[str, Any]:
        preset = await asyncio.to_thread(store.get, preset_id)
        return {"ok": True, "preset": _preset_public(preset)}

    @app.post(
        "/api/presets",
        response_model=PresetDetailResponse,
        status_code=201,
        responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["presets"],
    )
    async def create_preset(body: PresetSaveRequest) -> dict[str, Any]:
        preset = await asyncio.to_thread(store.save, body.model_dump(by_alias=True))
        return {"ok": True, "preset": _preset_public(preset)}

    @app.put(
        "/api/presets/{preset_id}",
        response_model=PresetDetailResponse,
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["presets"],
    )
    async def update_preset(preset_id: str, body: PresetSaveRequest) -> dict[str, Any]:
        """覆盖保存：``preset_id`` 与 ``created_at`` 由服务端保留，客户端改不动。"""
        preset = await asyncio.to_thread(
            store.save, body.model_dump(by_alias=True), preset_id=preset_id
        )
        return {"ok": True, "preset": _preset_public(preset)}

    @app.post(
        "/api/presets/{preset_id}/rename",
        response_model=PresetDetailResponse,
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["presets"],
    )
    async def rename_preset(preset_id: str, body: PresetRenameRequest) -> dict[str, Any]:
        """重命名：**身份不变**，前端已有的引用不会失效。"""
        preset = await asyncio.to_thread(store.rename, preset_id, body.name)
        return {"ok": True, "preset": _preset_public(preset)}

    @app.post(
        "/api/presets/{preset_id}/duplicate",
        response_model=PresetDetailResponse,
        status_code=201,
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["presets"],
    )
    async def duplicate_preset(preset_id: str, body: PresetDuplicateRequest) -> dict[str, Any]:
        preset = await asyncio.to_thread(store.duplicate, preset_id, body.name)
        return {"ok": True, "preset": _preset_public(preset)}

    @app.delete(
        "/api/presets/{preset_id}",
        response_model=PresetDeleteResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["presets"],
    )
    async def delete_preset(preset_id: str) -> dict[str, Any]:
        deleted = await asyncio.to_thread(store.delete, preset_id)
        return {"ok": True, "deleted": deleted}

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
