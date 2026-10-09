"""FastAPI 应用：本地卡通渲染调参台。

路由：
* ``GET  /``                       旧单页界面（原生 HTML/CSS/JS，行为不变）
* ``GET  /next``                   v4 工作台（构建产物；未构建 ⇒ 503 FRONTEND_NOT_BUILT）
* ``GET  /next/assets/{path}``     v4 构建资源（内容哈希命名，长缓存；越界 ⇒ 404）
* ``GET  /api/health``             仅检查本地控制服务
* ``GET  /api/blender/status``     快速探测端口与协议
* ``GET  /api/blender/scene``      执行完整只读场景探针
* ``POST /api/blender/reconnect``  重置连接状态并立即重新检测
* ``GET  /api/framing/context``    当前帧 / 相机 / 相机动画 / 角色是否完整入画
* ``GET  /api/params/schema``      L0 参数表
* ``GET  /api/color/looks``        某视图下 Blender 真正接受的 look 档位（依赖枚举）
* ``POST /api/color/looks/refresh`` 重新扫描 look 能力表
* ``POST /api/session/baseline``   采集内存基线（含取景快照与 v4 拓扑）+ 首张预览
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
* ``POST /api/session/commit/prepare`` 校验基线/草稿/模式/目标路径，签发一次性确认令牌
* ``POST /api/session/commit``     消费令牌 → 应用完整草稿 → 回读校验 → 备份 → 保存工程
* ``GET  /api/diagnostics/describe`` 只读拓扑探测（脱敏）
* ``GET  /api/v4/surface/schema``  v4 递归 schema（Cel 竖切）
* ``GET  /api/v4/session/baseline`` v4 基线（身份 + 结构指纹 + 基线值）
* ``POST /api/v4/preview``         v4 预览：L0 + Cel 编进同一任务，只渲染一次
* ``GET  /api/v4/jobs/{id}``       v4 任务状态（与 ``/api/jobs/{id}`` 同一份存储）

安全边界：
* 只监听 127.0.0.1（见 config 回环校验）。
* **所有写接口**（本文件里全部 POST）都必须带上 ``X-Toon-Tuner-Token``：
  进程启动时随机生成，只通过 ``GET /`` 的页面响应注入，不落盘、不进日志。
* 不提供任何接受任意 Python 的接口；Blender 侧代码只能来自内置模板。
* 自动取景只用**临时预览相机**，绝不改动用户相机；渲染后必定恢复 ``scene.camera``。
* 预设接口**不接受任何路径参数**：目录与文件名全部由服务端决定。
* 保存工程只接受白名单化的模式与「绝对 .blend 路径」；覆盖前必先生成同目录备份，
  且确认令牌与基线/草稿/模式/目标路径逐项绑定，不可复用或篡改。
* look 是依赖 ``view_transform`` 的枚举：后端在**下发脚本前**完成依赖校验，
  非法组合返回稳定错误 ``INVALID_DEPENDENT_ENUM``，不退化成 ``BLENDER_SCRIPT_ERROR``。
* 预设存在 ``%LOCALAPPDATA%`` 下（不进仓库），写入前扫描并**拒绝**任何本机路径、
  模型/贴图路径与凭据；接口只回可展示位置，不回本机绝对路径。
* ``/next`` 的前端构建产物由本服务托管；**令牌只注入 HTML**，静态资源逐字节直出，
  且 ``index.html`` 明确 ``no-store``（它含本次进程的会话令牌）；资源路径越界一律 404。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (
    SERVICE_NAME,
    __version__,
    errors,
    framing as framing_module,
    frontend,
    params as params_module,
    presets as presets_module,
    scene_probe,
    security,
    surface,
    surface_probe,
)
from .binder import BlenderBinder, preview_dir, preview_png_name
from .blender_mcp import BlenderMCPClient
from .commit import CommitService
from .config import AppConfig, ConfigError, load_config
from .models import (
    BaselineRequest,
    BaselineResponse,
    BlenderStatusResponse,
    ColorLooksResponse,
    CommitPrepareRequest,
    CommitPrepareResponse,
    CommitRequest,
    CommitResponse,
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
    V4PreviewRequest,
)
from .presets import PresetStore
from .redact import redact
from .session import PreviewService, preview_url_for, validate_draft
from .surface_service import SurfaceService
from . import config as config_module

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

logger = logging.getLogger("toon_tuner")


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _error_detail(exc: errors.ToonTunerError) -> dict[str, Any]:
    return exc.to_payload()["error"]


def _split_v4_draft(
    draft: dict[str, Any], surface_service: SurfaceService
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把 v4 草稿按**基线 schema 里的 id 集合**拆成 L0 与 Cel 两部分。

    判定依据是 id 集合而不是字符串前缀：Cel 的 id 来自探测结果（组名是工程里的事实），
    前缀规则无法假装覆盖它，也会在将来新增参数族时悄悄失准。
    两边都不认识的 id 一律拒绝 —— 不静默丢弃用户提交的参数。
    """
    baseline = surface_service.baseline
    surface_ids = set(surface.flatten(baseline.nodes)) if baseline is not None else set()

    l0_draft: dict[str, Any] = {}
    surface_draft: dict[str, Any] = {}
    unknown: list[str] = []
    for param_id, value in (draft or {}).items():
        if param_id in surface_ids:
            surface_draft[param_id] = value
        elif param_id in params_module.BY_ID:
            l0_draft[param_id] = value
        else:
            unknown.append(param_id)
    if unknown:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID,
            "存在不在当前 v4 schema 内的参数：" + str(sorted(unknown)),
            details={"unknown": sorted(unknown), "known_surface": sorted(surface_ids)[:24]},
        )
    return l0_draft, surface_draft


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
    surface_service = SurfaceService()
    preview = PreviewService(binder, surface_service)
    #: 所有写接口的统一闸门；令牌只在进程启动时生成一次。
    guard = security.SessionGuard()
    # 预设实现以 PR#2 的完整版为准；PR#3 的 cfg.presets_dir 作为额外覆盖入口接进来。
    store = preset_store or PresetStore(cfg.presets_dir or presets_module.presets_dir())
    commit = CommitService(binder, preview)

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
    app.state.session_guard = guard
    #: 测试与「GET /」注入用；**不**经任何 API 暴露。
    app.state.session_token = guard.token
    app.state.presets = store
    app.state.commit = commit
    app.state.surface = surface_service

    # 写接口统一的依赖：缺失/错误令牌一律 401 拒绝。
    require_token = [Depends(security.require_session_token)]

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
        dependencies=require_token,
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

    @app.post(
        "/api/session/baseline",
        response_model=BaselineResponse,
        tags=["session"],
        dependencies=require_token,
    )
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
        dependencies=require_token,
    )
    async def color_looks_refresh() -> dict[str, Any]:
        """重新扫描 look 能力表（换了 OCIO 配置时用）。只改能力表，不动工程取值。"""
        payload = await preview.refresh_look_capability()
        return {"ok": True, **payload}

    @app.post(
        "/api/session/restore",
        response_model=RestoreResponse,
        tags=["session"],
        dependencies=require_token,
    )
    async def restore_baseline() -> dict[str, Any]:
        return await preview.restore_baseline()

    @app.post(
        "/api/preview",
        response_model=PreviewSubmitResponse,
        tags=["preview"],
        dependencies=require_token,
    )
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
        dependencies=require_token,
    )
    async def create_preset(body: PresetSaveRequest) -> dict[str, Any]:
        preset = await asyncio.to_thread(store.save, body.model_dump(by_alias=True))
        return {"ok": True, "preset": _preset_public(preset)}

    @app.put(
        "/api/presets/{preset_id}",
        response_model=PresetDetailResponse,
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["presets"],
        dependencies=require_token,
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
        dependencies=require_token,
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
        dependencies=require_token,
    )
    async def duplicate_preset(preset_id: str, body: PresetDuplicateRequest) -> dict[str, Any]:
        preset = await asyncio.to_thread(store.duplicate, preset_id, body.name)
        return {"ok": True, "preset": _preset_public(preset)}

    @app.delete(
        "/api/presets/{preset_id}",
        response_model=PresetDeleteResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["presets"],
        dependencies=require_token,
    )
    async def delete_preset(preset_id: str) -> dict[str, Any]:
        deleted = await asyncio.to_thread(store.delete, preset_id)
        return {"ok": True, "deleted": deleted}
    # -- 应用到工程 -------------------------------------------------------
    @app.post(
        "/api/session/commit/prepare",
        response_model=CommitPrepareResponse,
        tags=["commit"],
        dependencies=require_token,
    )
    async def commit_prepare(body: CommitPrepareRequest) -> dict[str, Any]:
        """校验基线 / 草稿 / 保存模式 / 目标路径，签发**一次性短效**确认令牌。

        覆盖已有文件时**不传 confirm 不签发令牌**，而是回 ``SAVE_CONFIRM_REQUIRED``，
        其 details 里带着目标绝对路径与预计备份路径 —— 这正是「先看清再确认」那一步。
        """
        return await commit.prepare(
            mode=body.mode,
            draft=body.draft,
            target_path=body.target_path,
            confirm=body.confirm,
            surface_draft=body.surface_draft,
            structure_hash=body.structure_hash,
        )

    @app.post(
        "/api/session/commit",
        response_model=CommitResponse,
        tags=["commit"],
        dependencies=require_token,
    )
    async def commit_apply(body: CommitRequest) -> dict[str, Any]:
        """消费令牌 → 应用完整草稿 → 回读校验 → 备份 → 保存工程。

        任一步失败都**不会保存**，草稿保留在界面上；失败响应的 ``details.status``
        带有应用 / 回读 / 备份 / 保存四个状态。
        """
        return await commit.commit(
            token=body.token,
            mode=body.mode,
            draft=body.draft,
            target_path=body.target_path,
            surface_draft=body.surface_draft,
            structure_hash=body.structure_hash,
        )

    # -- 诊断（只读，不需要令牌）------------------------------------------
    @app.get("/api/diagnostics/describe", tags=["diagnostics"])
    async def diagnostics_describe() -> dict[str, Any]:
        """只读拓扑描述：受管节点组、ColorRamp 结构、对象/材质/贴图清单。

        用途：Cel 色带的真实节点与插座名无法凭空确定，也不能靠猜。先在真机跑这个接口，
        拿到拓扑后 schema 生成器**以探测结果为准**生成可编辑节点；探不到的组一律降级为
        ``supported: false`` 的只读节点。

        **脱敏**：不返回工程路径、贴图路径、令牌或用户目录；键名像路径的字段整体丢弃。
        只读接口，因此按既有规则不校验会话令牌。
        """
        payload = await preview.call_binder(binder.describe_surface)
        return {"ok": True, **surface_probe.describe_groups(
            surface_probe.redact_describe(payload)
        )}

    # -- v4 参数面（Cel 竖切）---------------------------------------------
    @app.get("/api/v4/surface/schema", tags=["v4"])
    async def v4_surface_schema() -> dict[str, Any]:
        """v4 递归 schema：每个节点都带 supported / editable / active / readonly_reason。

        色带是**单一复合节点**（``kind: ramp``，内含 ``elements`` 与 ``interpolation``），
        刻意不展开成 2N 个独立 id —— ColorRamp 是单个 datablock，整体写入才原子。
        只读接口，因此按既有规则不校验会话令牌。
        """
        return {"ok": True, **surface_service.schema_public()}

    @app.get("/api/v4/session/baseline", tags=["v4"])
    async def v4_session_baseline() -> dict[str, Any]:
        """v4 基线：身份记录 + 结构指纹 + 基线值 + 降级清单。

        建立基线仍走 ``POST /api/session/baseline``（它会顺带采集这里的 v4 状态）；
        本接口只读取当前 v4 基线。只读接口，不校验令牌。
        """
        payload = surface_service.public()
        if not payload.get("available"):
            raise errors.ToonTunerError(
                errors.NO_BASELINE,
                "尚未建立 v4 参数面基线。请先调用 POST /api/session/baseline。",
                details={"probe_error": payload.get("error")},
            )
        return {"ok": True, **payload}

    @app.post(
        "/api/v4/preview",
        response_model=PreviewSubmitResponse,
        tags=["v4"],
        dependencies=require_token,
    )
    async def v4_preview(body: V4PreviewRequest) -> dict[str, Any]:
        """提交 v4 预览：L0 与 Cel 编进**同一个任务**，因此只渲染一次、一起回滚。

        提交阶段只做「客户端自己能发现的错」：草稿越界、色标数量变化、
        客户端持有的结构指纹过期。真正的身份 / 结构闸门在任务里、**任何写入之前**再跑一遍。
        """
        options = framing_module.validate_options(
            body.framing.model_dump() if body.framing is not None else None
        )
        l0_draft, surface_draft = _split_v4_draft(body.draft, surface_service)
        job = await preview.submit_surface(
            l0_draft=l0_draft,
            surface_draft=surface_draft,
            framing_options=options,
            expected_structure_hash=body.expected_structure_hash,
        )
        return {
            "ok": True,
            "job_id": job.job_id,
            "seq": job.seq,
            "status": job.status,
            "framing": framing_module.describe_options(dict(job.framing)),
        }

    @app.get("/api/v4/jobs/{job_id}", response_model=JobResponse, tags=["v4"])
    async def v4_read_job(job_id: str) -> dict[str, Any]:
        """v4 任务状态。与 ``/api/jobs/{id}`` 共用同一份任务存储（排队与取代机制同一套）。"""
        job = preview.get_job(job_id)
        if job is None:
            raise errors.ToonTunerError(errors.JOB_NOT_FOUND, f"任务不存在：{job_id}")
        return job.to_public()

    # -- 静态页面 ---------------------------------------------------------
    index_file = WEB_DIR / "index.html"
    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        """旧单页界面（原生 HTML/CSS/JS）。

        令牌在**响应时**注入：磁盘上的 ``index.html`` 只有占位注释，
        因此令牌永远不会出现在静态文件、构建产物或日志里。
        """
        if index_file.is_file():
            html = index_file.read_text(encoding="utf-8")
            return HTMLResponse(security.inject_token(html, guard.token))
        return JSONResponse(
            status_code=500,
            content=errors.ToonTunerError(
                errors.INTERNAL_ERROR, "前端页面缺失（src/web/index.html）"
            ).to_payload(),
        )

    # -- v4 工作台（/next）------------------------------------------------
    #: 构建产物目录：`web/dist`。只影响服务端自己；接口层不接受任何路径参数。
    web_root = cfg.web_dir or (config_module.REPO_ROOT / "web")

    @app.get("/next", include_in_schema=False, response_class=HTMLResponse)
    async def next_index() -> Any:
        """v4 工作台页面。

        未构建时**明确报 503 FRONTEND_NOT_BUILT 与构建步骤**，
        绝不回退到旧页面 —— 那会让人以为「新工作台就是这样」。
        页面含会话令牌，因此 ``no-store``。
        """
        html = frontend.read_index(web_root)
        response = HTMLResponse(security.inject_token(html, guard.token))
        response.headers["Cache-Control"] = frontend.INDEX_CACHE_CONTROL
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/next/assets/{asset_path:path}", include_in_schema=False)
    async def next_asset(asset_path: str) -> Any:
        """v4 构建资源。

        * **逐字节直出**：不做任何令牌注入（令牌必须只出现在 HTML 里）；
        * 文件名带内容哈希 ⇒ 长缓存；
        * 路径越界与不存在一律 404。
        """
        path = frontend.resolve_asset(web_root, asset_path)
        response = FileResponse(path, media_type=frontend.content_type_for(path))
        response.headers["Cache-Control"] = frontend.ASSET_CACHE_CONTROL
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return app


app = create_app()


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")


if __name__ == "__main__":
    main()
