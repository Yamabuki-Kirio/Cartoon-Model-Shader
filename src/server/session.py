"""内存基线与预览任务。

核心不变量（需求文档）
----------------------
* 供预设 / 提交(commit) 复用的三个公开入口：``validate_draft``（白名单 + 范围 +
  **依赖枚举**校验）、``normalize_values``（按能力表把取值规范成可写入形式）、
  ``verify_values``（回读逐项比对）。三者与预览走**完全相同**的判定逻辑，
  避免「预览能过、保存却写坏工程」这种两条实现漂移出来的缺口。

* 每次预览都 **先恢复内存基线 → 再一次性应用完整草稿**，绝不在上一次结果上叠加。
* 旧任务自动作废：新任务提交时，排队中与运行中的旧任务立即标记为 ``superseded``，
  其结果被丢弃。
* 不使用固定 ``sleep``：任务进度靠状态查询，Blender 侧一律等待响应。
* **取景失效即停**：基线记录了帧 / 相机 / 焦距 / shift / 角色包围盒；一旦当前帧或
  相机被外部改动，提交立刻以 ``FRAMING_STALE`` 拒绝，绝不把新构图当成参数效果。

任务通过一个**合并式单工作线程**执行：短时间内的多次提交只会跑最后一个，
既满足「旧任务作废」，又避免 Blender 被连续渲染请求压垮。
"""

from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import logging
import math
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from . import color_looks, errors, framing as framing_module, params, surface_probe
from .binder import PREVIEW_FALLBACK_RESOLUTION, BlenderBinder, preview_resolution_for
from .surface_service import SurfaceService, param_values, verify_ops

#: 保留的历史任务上限（防止长时间运行内存无界增长）
MAX_JOBS = 50

logger = logging.getLogger("toon_tuner")

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_SUPERSEDED = "superseded"

_FLOAT_TOLERANCE = 1e-6


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _deep_copy(value: Any) -> Any:
    """深拷贝一份只读快照，避免外部拿到内部可变结构。"""
    return copy.deepcopy(value)


def _render_framing_summary(render: dict[str, Any], job_framing: dict[str, Any] | None) -> dict[str, Any]:
    """把 Blender 侧返回的取景结果整理成前端契约（含「是否已恢复原相机」）。"""
    raw = render.get("framing") or {}
    mode = raw.get("mode") or (job_framing or {}).get("mode")
    return {
        "mode": mode,
        "mode_label": raw.get("mode_label") or framing_module.FRAMING_MODES.get(mode, mode),
        "margin": raw.get("margin"),
        "temporary_camera": bool(raw.get("temporary")),
        "temporary_camera_name": raw.get("temporary_camera_name"),
        "original_camera": raw.get("original_camera"),
        "camera_used": raw.get("camera_used"),
        "camera_restored": raw.get("camera_restored"),
        "temporary_camera_leftovers": render.get("temporary_camera_leftovers"),
        "distance": raw.get("distance"),
        "frame": render.get("frame"),
        "frame_after": render.get("frame_after"),
        "fit": framing_module.normalize_fit(raw.get("fit")),
        "character": framing_module.normalize_character(raw.get("character")),
    }


def _render_output_summary(render: dict[str, Any]) -> dict[str, Any]:
    """整理「预览的输出设置有没有被污染」。

    Blender 侧在渲染前整份记下 ``media_type`` / ``file_format`` / ``color_mode`` /
    ``color_depth`` / ``filepath``，渲染结束（含异常、含中止）后逐项回读。这里只做三件事：

    * 如实透出改了没有、还原了没有（``applied`` / ``restore_steps``）；
    * 给出 ``restored_ok`` —— 「逐项写回都成功且不再有差异」；
    * 给出 ``verified`` 闸门 —— **必须同时拿到改前与回读两份值并且逐项相等**。
      拿不到（载荷缺字段）一律算**没通过**，不报假绿。
    """
    raw = render.get("output")
    empty = {
        "target_format": None,
        "applied": None,
        "unavailable_reason": None,
        "original": None,
        "restored": None,
        "restore_steps": None,
        "mismatches": None,
        "restored_ok": None,
        "verified": False,
    }
    if not isinstance(raw, dict):
        return empty

    original = raw.get("original")
    restored = raw.get("restored")
    mismatches = raw.get("mismatches") or {}
    verified = bool(original) and isinstance(original, dict) and restored == original
    return {
        "target_format": raw.get("target_format"),
        "applied": raw.get("applied"),
        "unavailable_reason": raw.get("unavailable_reason"),
        "original": original if isinstance(original, dict) else None,
        "restored": restored if isinstance(restored, dict) else None,
        "restore_steps": raw.get("restore_steps"),
        "mismatches": mismatches,
        "restored_ok": bool(raw.get("restored_ok")) and not mismatches,
        "verified": verified,
    }


def _project_dirty_summary(
    dirty_at_baseline: Any, state: dict[str, Any] | None
) -> dict[str, Any]:
    """预览结束后的工程脏标记快照（O3）。

    Blender 的 ``bpy.data.is_dirty`` 是**粘性**的：把参数写回原值并不会清除它。
    本工具又只在用户显式走「保存」流程时才写盘 —— 所以「基线时干净、预览后变脏」
    几乎必然发生，**这不是缺陷，也不需要（更不能）靠自动保存去「修」**。
    页面据此给出准确措辞：值/节点/相机/输出设置已恢复，工程没被保存。

    只回布尔值与 basename —— 绝对路径一律不进公开响应。
    """
    payload = state or {}
    dirty_after = payload.get("is_dirty")
    dirty_after = bool(dirty_after) if dirty_after is not None else None
    return {
        "dirty_at_baseline": dirty_at_baseline,
        "dirty_after_preview": dirty_after,
        #: 仅当「基线干净 → 预览后变脏」时为真：这才是需要向用户解释的情形。
        "dirty_flagged": dirty_at_baseline is False and dirty_after is True,
        "file_name": payload.get("file_name"),
    }


def preview_url_for(job_id: str) -> str:
    """预览图的唯一 HTTP 端点。

    落盘位置在系统临时目录（``%TEMP%/toon-tuner-previews``），那是**本机文件
    系统路径**，浏览器既不能也不应直接把它当作 ``img.src``。前端一律走这个
    端点取图，服务端按 ``job_id`` 决定实际文件，不接受任何路径参数。
    """
    return f"/api/preview/{job_id}"


def _empty_restore(reason: str | None = None) -> dict[str, Any]:
    """失败/回滚状态块的统一形状（L0 与 Cel 两部分分开报）。"""
    return {
        "attempted": False,
        "verified": False,
        "code": None,
        "reason": reason,
        "l0": {
            "attempted": False,
            "verified": False,
            "mismatches": [],
            "code": None,
            "error": None,
        },
        "cel": {
            "attempted": False,
            "verified": False,
            "mismatches": [],
            "code": None,
            "error": None,
        },
    }


def _restore_suffix(restore: dict[str, Any]) -> str:
    """把恢复结果说清楚：成功就断言成功，失败就直说没恢复成。"""
    if restore.get("verified"):
        return "已恢复到提交前的基线（L0 与 Cel 均回读校验通过）。"
    if restore.get("attempted"):
        return (
            "**恢复基线后回读不一致或恢复失败**（rollback.code = "
            f"{restore.get('code')}）：Blender 里的取值可能仍不是提交前的状态，请人工确认。"
        )
    return "**未能恢复基线**：Blender 里的取值可能仍不是提交前的状态，请人工确认。"


def _surface_error_payload(
    exc: errors.ToonTunerError,
    *,
    restore: dict[str, Any],
    steps: list[str],
    external_changes: list[dict[str, Any]],
) -> dict[str, Any]:
    """v4 失败响应：保留原始错误码与内层细节，附上步骤 / 恢复 / 外部改动。"""
    error = exc.to_payload()["error"]
    details: dict[str, Any] = dict(error.get("details") or {})
    details["steps"] = list(steps)
    details["restore"] = restore
    details["external_changes"] = list(external_changes)
    error["details"] = details
    error["message"] = f"{exc.message} {_restore_suffix(restore)}"
    return error


def _baseline_restore_values(baseline: Baseline) -> dict[str, Any]:
    """恢复基线用的取值（走同一套依赖映射，不盲目回写旧字符串）。"""
    values, _ = normalize_values(dict(baseline.values), baseline.look_map)
    return values


@dataclass
class Baseline:
    baseline_id: str
    captured_at: str
    blender: str
    glare_present: bool
    values: dict[str, Any]
    options: dict[str, list[Any]]
    render: dict[str, Any]
    #: 建立基线时的取景快照（帧 / 相机 / transform / lens / shift / 角色包围盒）
    framing: dict[str, Any] = field(default_factory=dict)
    #: ``view_transform`` -> ``[{value, label}]``：Blender 真实接受的 look 档位。
    #: look 是**依赖枚举**，不查这张表就写值必然踩 enum not found。
    look_map: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    #: 建立基线时的**工程脏标记**（`bpy.data.is_dirty`）与工程文件名。
    #:
    #: Blender 的脏标记是**粘性的**：写回原值也不会自动清除，而本工具不保存工程
    #: （保存是另一个需要二次确认的动作）。因此「基线时干净、预览后变脏」是
    #: **预期行为**，必须在页面上说清楚，否则用户会以为工程内容真的被改了。
    #: 这里只存布尔值与 basename —— **绝不**把绝对路径带进公开响应。
    project: dict[str, Any] = field(default_factory=dict)

    @property
    def preview_resolution(self) -> tuple[int, int, int]:
        return preview_resolution_for(self.render)

    @property
    def view_transform(self) -> str | None:
        value = self.values.get("color.view_transform")
        return str(value) if value is not None else None


@dataclass
class SurfacePlan:
    """一次 v4 预览要写进 Blender 的 Cel 计划（提交时编译好，运行期不再重算）。"""

    ops: list[Any] = field(default_factory=list)
    #: 恢复 Cel 基线用的计划（采集基线的同时算好，与应用走同一条编译路径）
    baseline_ops: list[Any] = field(default_factory=list)
    structure_hash: str = ""
    surface_baseline_id: str = ""

    @property
    def empty(self) -> bool:
        return not self.ops and not self.baseline_ops


@dataclass
class Job:
    job_id: str
    seq: int
    status: str
    created_at: str
    updated_at: str
    requested: dict[str, Any]
    effective: dict[str, Any]
    framing: dict[str, Any] = field(default_factory=dict)
    #: 调用方**原样**提交的草稿（未经规范化），用于 run/preset 的 configured_value
    raw_requested: dict[str, Any] = field(default_factory=dict)
    superseded: bool = False
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    steps: list[str] = field(default_factory=list)
    #: v4 任务：Cel 写入计划。为 ``None`` 表示这是旧的纯 L0 任务。
    surface: SurfacePlan | None = None
    #: 值层外部改动（只报告，不作废草稿与令牌）
    external_changes: list[dict[str, Any]] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "seq": self.seq,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "superseded": self.superseded,
            "steps": list(self.steps),
            "framing_request": (
                framing_module.describe_options(self.framing) if self.framing else None
            ),
        }
        if self.surface is not None:
            payload["kind"] = "surface"
        if self.external_changes:
            payload["external_changes"] = _deep_copy(self.external_changes)
        if self.result is not None:
            payload["result"] = self.result
        if self.error is not None:
            payload["error"] = self.error
        return payload


def values_from_read(payload: dict[str, Any]) -> dict[str, Any]:
    """把 Blender 的只读回读结构映射成 ``param_id -> value``。"""
    view = payload.get("view") or {}
    glare = payload.get("glare") or {}
    result: dict[str, Any] = {}
    for spec in params.ALL_PARAMS:
        binding = spec.binding
        if binding in ("view.exposure", "view.gamma", "view.view_transform", "view.look"):
            result[spec.id] = view.get(binding.split(".", 1)[1])
        elif binding.startswith("glare."):
            result[spec.id] = glare.get(binding.split(".", 1)[1]) if glare else None
    return result


def _options_from_read(
    payload: dict[str, Any],
    look_map: dict[str, list[dict[str, str]]] | None = None,
    view_transform: str | None = None,
) -> dict[str, list[Any]]:
    """把只读回读里的动态枚举候选整理成 ``binding -> [{value,label}]``。

    ``view.look`` 不取只读回读（那里没有它），而是取**按当前视图探测出的能力表**：
    OCIO 的全局 ``getLookNames()`` 会给出当前视图并不接受的名字，不能用。
    """
    raw = payload.get("view_options") or {}
    options: dict[str, list[Any]] = {}
    for spec in params.ALL_PARAMS:
        if not spec.options_dynamic:
            continue
        if spec.binding == "view.look":
            mapped = list((look_map or {}).get(view_transform or "") or [])
            options[spec.binding] = mapped or [
                {"value": value, "label": value} for value in spec.options
            ]
            continue
        values = raw.get(spec.binding) or []
        options[spec.binding] = (
            [{"value": str(v), "label": str(v)} for v in values]
            if values
            else [{"value": str(v), "label": str(v)} for v in spec.options]
        )
    return options


def baseline_from_payload(
    payload: dict[str, Any],
    framing_snapshot: dict[str, Any] | None = None,
    look_map: dict[str, list[dict[str, str]]] | None = None,
    project: dict[str, Any] | None = None,
) -> Baseline:
    values = values_from_read(payload)
    view_transform = values.get("color.view_transform")
    view_transform = str(view_transform) if view_transform is not None else None
    return Baseline(
        baseline_id=uuid.uuid4().hex[:12],
        captured_at=_now_iso(),
        blender=str(payload.get("blender", "")),
        glare_present=bool(payload.get("glare_present")),
        values=values,
        options=_options_from_read(payload, look_map, view_transform),
        render=payload.get("render") or {},
        framing=framing_snapshot or {},
        look_map=dict(look_map or {}),
        project=dict(project or {}),
    )


class PreviewService:
    """基线与预览任务的唯一权威。"""

    def __init__(self, binder: BlenderBinder, surface: SurfaceService | None = None) -> None:
        self._binder = binder
        #: v4 参数面状态。为 ``None`` 时本服务退化成纯 L0（旧行为不变）。
        self._surface = surface or SurfaceService()
        self._baseline: Baseline | None = None
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._seq = 0
        self._state_lock = asyncio.Lock()
        self._blender_lock = asyncio.Lock()
        self._pending: Job | None = None
        self._current: Job | None = None
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

    # -- 生命周期 --------------------------------------------------------
    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop(), name="preview-worker")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
            self._worker = None

    # -- 基线 ------------------------------------------------------------
    @property
    def baseline(self) -> Baseline | None:
        return self._baseline

    @property
    def surface(self) -> SurfaceService:
        """v4 参数面状态（保存流程也用它做结构绑定与失效判定）。"""
        return self._surface

    def baseline_public(self) -> dict[str, Any] | None:
        if self._baseline is None:
            return None
        b = self._baseline
        return {
            "baseline_id": b.baseline_id,
            "captured_at": b.captured_at,
            "blender": b.blender,
            "glare_present": b.glare_present,
            "values": dict(b.values),
            "options": {k: _deep_copy(v) for k, v in b.options.items()},
            "render": dict(b.render),
            "framing": _deep_copy(b.framing),
            "look_map": _deep_copy(b.look_map),
            "preview_resolution": list(b.preview_resolution),
            # 工程脏标记快照（只有 dirty / file_name，不含任何绝对路径）
            "project": _deep_copy(b.project),
            # v4：**新增的可选字段**。旧页面不看它，因此 L0 契约不变。
            "surface": self._surface.public(),
        }

    async def capture_baseline(self) -> dict[str, Any]:
        """采集内存基线：曝光/辉光现值 + 取景快照 + look 依赖枚举能力表 + v4 拓扑。

        * 取景快照包含 ``frame_current`` / ``scene.camera`` / 相机 transform /
          ``lens`` / ``shift_x`` / ``shift_y`` / 角色世界变换与包围盒。
        * look 能力表是 **``view_transform`` -> 合法 look 档位** 的完整映射，
          由只读探针一次扫出（Blender 侧在 ``finally`` 恢复原状态）。
          有了它，前端切换视图变换时无需再问 Blender，后端也能在**下发脚本前**
          就判定 look 是否合法。
        * v4 拓扑探针同样是只读的，采集到的身份/结构/值快照供 Cel 竖切使用。
        """
        baseline = await self._capture()
        async with self._state_lock:
            self._baseline = baseline
        return self.baseline_public() or {}

    def baseline_project_public(self) -> dict[str, Any] | None:
        """基线那一刻的工程脏标记快照（**只有布尔值与文件名**）。

        无基线时返回 ``None``。给 ``/api/v4/session/baseline`` 用：页面据此判断
        「原本干净、预览后才变脏」。刻意不返回任何绝对路径。
        """
        if self._baseline is None:
            return None
        return _deep_copy(self._baseline.project)

    async def _capture(self) -> Baseline:
        payload = await self._call(self._binder.read_exposure)
        context = await self._call(self._binder.read_framing)
        capability = await self._call(self._binder.read_look_capability)
        baseline = baseline_from_payload(
            payload,
            framing_module.baseline_snapshot(context),
            capability.get("looks") or {},
            project=self._baseline_project_snapshot(await self._safe_project_state()),
        )
        await self._capture_surface()
        return baseline

    async def _capture_surface(self) -> None:
        """采集 v4 参数面基线（只读拓扑探针 + 递归 schema + 三层指纹）。

        探针**失败**时不清空 L0 基线：L0 与 v4 是两条独立的能力，探针挂了不该让
        老页面一起不可用。但也不假装「工程里没有 Cel 组」—— 失败原因原样记下，
        由 ``/api/v4/session/baseline`` 明确报出。
        """
        try:
            raw = await self._call(self._binder.describe_surface)
        except errors.ToonTunerError as exc:
            self._surface.record_error(exc.to_payload()["error"])
            logger.warning("v4 拓扑探针失败：%s", exc.code)
            return
        describe = surface_probe.describe_groups(surface_probe.redact_describe(raw))
        self._surface.capture(describe)

    async def read_framing_context(self) -> dict[str, Any]:
        """读取当前取景上下文，并附带与基线的比对结论。"""
        context = await self._call(self._binder.read_framing)
        baseline = self._baseline
        verdict = framing_module.compare_context(
            baseline.framing if baseline is not None else None, context
        )
        context["baseline"] = {
            "established": baseline is not None,
            "baseline_id": baseline.baseline_id if baseline is not None else None,
            "captured_at": baseline.captured_at if baseline is not None else None,
            "frame_current": (baseline.framing or {}).get("frame_current") if baseline else None,
            "camera": (baseline.framing or {}).get("camera") if baseline else None,
            "stale": verdict["stale"],
            "reasons": verdict["reasons"],
            "warnings": verdict["warnings"],
        }
        return context

    async def _ensure_baseline(self) -> Baseline:
        async with self._state_lock:
            if self._baseline is not None:
                return self._baseline
        return await self._capture()

    async def read_look_capability(
        self, view_transform: str | None = None, identifier: str | None = None
    ) -> dict[str, Any]:
        """look 能力查询。

        * 基线里已有完整映射时直接返回（**零 Blender 调用**）；
        * 指定了 ``view_transform`` 且映射里没有它（或要校验某个 ``identifier``），
          才跑单视图只读探针 —— 探针在 ``finally`` 恢复原状态。
        """
        baseline = self._baseline
        look_map: dict[str, list[dict[str, str]]] = dict(baseline.look_map) if baseline is not None else {}
        current = baseline.view_transform if baseline is not None else None
        probed: dict[str, Any] | None = None

        if view_transform and (identifier is not None or view_transform not in look_map):
            probed = await self._call(self._binder.probe_look_for, view_transform, identifier)
            capability = color_looks.describe_capability(probed)
            if capability["options"]:
                look_map[view_transform] = capability["options"]

        target = view_transform or current
        return {
            "source": "probe" if probed is not None else ("baseline" if baseline is not None else "empty"),
            "current_view_transform": current,
            "view_transform": target,
            "options": list(look_map.get(target or "") or []),
            "probe": (probed or {}).get("probe"),
            "restored": (probed or {}).get("restored"),
            "look_map": look_map,
        }

    async def refresh_look_capability(self) -> dict[str, Any]:
        """重新扫描 look 能力表（用户换了 OCIO 配置时用），并回写进基线。

        只改**能力表**，不碰工程里的任何取值。
        """
        capability = await self._call(self._binder.read_look_capability)
        look_map = capability.get("looks") or {}
        async with self._state_lock:
            if self._baseline is not None and look_map:
                self._baseline.look_map = dict(look_map)
                current = self._baseline.view_transform or ""
                mapped = list(look_map.get(current) or [])
                if mapped:
                    self._baseline.options["view.look"] = mapped
        return await self.read_look_capability()

    async def restore_baseline(self) -> dict[str, Any]:
        baseline = await self._ensure_baseline()
        async with self._state_lock:
            self._baseline = baseline
        # 恢复基线也要走**同一套映射逻辑**，不能盲目回写旧字符串：
        # 若 OCIO 配置或视图变换已变，旧 look 可能不再合法。
        values, notes = normalize_values(baseline.values, baseline.look_map)
        readback = await self._call(self._binder.apply_values, values)
        verified, mismatches = verify_values(baseline.values, readback)
        return {
            "ok": True,
            "baseline_id": baseline.baseline_id,
            "verified": verified,
            "mismatches": mismatches,
            "readback": values_from_read(readback),
            "normalized": notes,
        }

    async def _assert_framing_fresh(self) -> None:
        """提交前的取景闸门：帧 / 相机被外部改动时拒绝预览。

        出错信息里带上「原来是什么 / 现在是什么」，便于直接定位用户那边做了什么。
        """
        async with self._state_lock:
            baseline = self._baseline
        if baseline is None or not baseline.framing:
            return
        context = await self._call(self._binder.read_framing)
        verdict = framing_module.compare_context(baseline.framing, context)
        if verdict["stale"]:
            payload = framing_module.stale_error_payload(verdict)
            raise errors.ToonTunerError(
                errors.FRAMING_STALE,
                payload["message"],
                details={"reasons": verdict["reasons"], "warnings": verdict["warnings"]},
            )

    # -- 任务 ------------------------------------------------------------
    async def submit(
        self, draft: dict[str, Any], framing_options: dict[str, Any] | None = None
    ) -> Job:
        options = framing_module.validate_options(framing_options)
        async with self._state_lock:
            if self._baseline is None:
                raise errors.ToonTunerError(
                    errors.NO_BASELINE, "尚未建立内存基线，无法预览。"
                )
        # 帧/相机被外部改动 => 直接拒绝，绝不把新构图当成参数效果（且不产生任务）
        await self._assert_framing_fresh()

        async with self._state_lock:
            baseline = self._baseline
            if baseline is None:  # pragma: no cover - 与上面同锁，仅防御
                raise errors.ToonTunerError(errors.NO_BASELINE, "尚未建立内存基线，无法预览。")
            coerced = validate_draft(draft, baseline)
            effective, notes = normalize_values(
                {**baseline.values, **coerced}, baseline.look_map
            )

            self._seq += 1
            now = _now_iso()
            job = Job(
                job_id=uuid.uuid4().hex[:16],
                seq=self._seq,
                status=JOB_QUEUED,
                created_at=now,
                updated_at=now,
                requested=coerced,
                effective=effective,
                framing=dict(options),
                raw_requested=dict(draft),
            )
            for note in notes:
                job.steps.append(f"依赖迁移：{note['parameter']} {note['from']!r} -> {note['to']!r}")
            return self._enqueue_locked(job)

    async def submit_surface(
        self,
        l0_draft: dict[str, Any],
        surface_draft: dict[str, Any],
        framing_options: dict[str, Any] | None = None,
        expected_structure_hash: str | None = None,
    ) -> Job:
        """提交一个 **v4 任务**：L0 与 Cel 在同一任务生命周期内应用、渲染、恢复。

        三条约束在这里落地：

        1. **混合草稿只跑一次** —— L0 与 Cel 编译进同一个任务，只渲染一张图；
        2. **结构指纹先对账** —— 客户端手里的 ``expected_structure_hash`` 与当前基线
           不一致时**在提交阶段**就拒绝（不产生任务）。真正的身份/结构闸门在任务里
           再跑一遍（提交与执行之间工程可能被改动），那一步在**任何写入之前**；
        3. **草稿在提交时编译完** —— 运行期不再重新校验，避免「排队期间草稿被换掉」。
        """
        options = framing_module.validate_options(framing_options)
        async with self._state_lock:
            baseline = self._baseline
        if baseline is None:
            raise errors.ToonTunerError(errors.NO_BASELINE, "尚未建立内存基线，无法预览。")

        surface_service = self._surface
        surface_baseline = surface_service.baseline
        if surface_baseline is None:
            raise errors.ToonTunerError(
                errors.NO_BASELINE,
                "尚未建立 v4 参数面基线（只读拓扑探针未成功），无法预览 Cel 参数。",
                details={"probe_error": surface_service.error},
            )
        if expected_structure_hash and expected_structure_hash != surface_baseline.structure_hash:
            raise errors.ToonTunerError(
                errors.STRUCTURE_CHANGED,
                "客户端持有的结构指纹与当前基线不一致：工程结构已变化，请刷新基线后再提交。",
                details={
                    "expected": expected_structure_hash,
                    "current": surface_baseline.structure_hash,
                },
            )

        await self._assert_framing_fresh()

        async with self._state_lock:
            baseline = self._baseline
            if baseline is None:  # pragma: no cover - 与上面同锁，仅防御
                raise errors.ToonTunerError(errors.NO_BASELINE, "尚未建立内存基线，无法预览。")
            coerced = validate_draft(l0_draft, baseline)
            effective, notes = normalize_values(
                {**baseline.values, **coerced}, baseline.look_map
            )
            # 草稿在这里编译成计划；非法取值（越界 / 色标数量变化 / 只读项）直接抛出，
            # 不产生任务 —— 与既有 L0 路径的失败时机保持一致。
            ops = surface_service.plan(surface_draft)

            self._seq += 1
            now = _now_iso()
            job = Job(
                job_id=uuid.uuid4().hex[:16],
                seq=self._seq,
                status=JOB_QUEUED,
                created_at=now,
                updated_at=now,
                requested=coerced,
                effective=effective,
                framing=dict(options),
                raw_requested=dict(l0_draft),
                surface=SurfacePlan(
                    ops=list(ops),
                    baseline_ops=list(surface_baseline.baseline_ops),
                    structure_hash=surface_baseline.structure_hash,
                    surface_baseline_id=surface_baseline.baseline_id,
                ),
            )
            for note in notes:
                job.steps.append(f"依赖迁移：{note['parameter']} {note['from']!r} -> {note['to']!r}")
            return self._enqueue_locked(job)

    def _enqueue_locked(self, job: Job) -> Job:
        """入队并把排队中 / 运行中的旧任务标记为作废。**调用方须持有状态锁。**"""
        self._jobs[job.job_id] = job
        for other in (self._pending, self._current):
            if other is None or other.status not in (JOB_QUEUED, JOB_RUNNING):
                continue
            other.superseded = True
            if other.status == JOB_QUEUED:
                # 尚未被工作线程取走：直接落到终态，避免前端一直轮询
                self._finish(other, JOB_SUPERSEDED, note="提交时被更新的任务取代")
        self._pending = job
        self._trim_locked()
        self._wake.set()
        return job

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def _trim_locked(self) -> None:
        while len(self._jobs) > MAX_JOBS:
            old_id, old = next(iter(self._jobs.items()))
            if old.status in (JOB_QUEUED, JOB_RUNNING):
                break
            self._jobs.pop(old_id, None)

    # -- 工作线程 --------------------------------------------------------
    async def _worker_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            async with self._state_lock:
                job = self._pending
                self._pending = None
                self._current = job
            if job is None:
                continue
            try:
                if job.superseded:
                    self._finish(job, JOB_SUPERSEDED, note="提交时被更新的任务取代")
                    continue
                if job.surface is not None:
                    await self._run_surface_job(job)
                else:
                    await self._run_job(job)
            finally:
                self._current = None

    def _finish(self, job: Job, status: str, *, error: dict | None = None,
                result: dict | None = None, note: str | None = None) -> None:
        job.status = status
        job.error = error
        job.result = result
        job.updated_at = _now_iso()
        if note:
            job.steps.append(note)

    async def _run_job(self, job: Job) -> None:
        job.status = JOB_RUNNING
        job.updated_at = _now_iso()
        try:
            baseline = await self._ensure_baseline()
            async with self._state_lock:
                self._baseline = baseline

            job.steps.append("恢复基线")
            # 恢复也走同一套依赖映射（不能盲目回写旧字符串）
            restore_values, _ = normalize_values(dict(baseline.values), baseline.look_map)
            await self._call(self._binder.apply_values, restore_values)
            if self._is_superseded(job):
                return self._finish(job, JOB_SUPERSEDED, note="恢复基线后被取代")

            job.steps.append("应用完整草稿")
            applied = await self._call(self._binder.apply_values, dict(job.effective))
            if self._is_superseded(job):
                return self._finish(job, JOB_SUPERSEDED, note="应用草稿后被取代")

            job.steps.append("渲染预览")
            expected = {
                "frame_current": (baseline.framing or {}).get("frame_current"),
                "camera": (baseline.framing or {}).get("camera"),
            }
            render = await self._call(
                self._binder.render_preview,
                job.job_id,
                job.framing or None,
                baseline.preview_resolution,
                expected,
            )
            if self._is_superseded(job):
                return self._finish(job, JOB_SUPERSEDED, note="渲染完成后被取代")

            job.steps.append("恢复基线并校验")
            final_values, _ = normalize_values(dict(baseline.values), baseline.look_map)
            readback = await self._call(self._binder.apply_values, final_values)
            verified, mismatches = verify_values(baseline.values, readback)

            result = {
                "preview_url": preview_url_for(job.job_id),
                "render_resolution": render.get("render_resolution"),
                "size_bytes": render.get("size_bytes"),
                "applied": values_from_read(applied),
                "restored": values_from_read(readback),
                "baseline_id": baseline.baseline_id,
                "restore_verified": verified,
                "restore_mismatches": mismatches,
                # configured / effective / display_label 分开保存（需求 9）
                "parameters": _parameter_records(baseline, job, applied),
                "view_transform": baseline.view_transform,
                "framing": _render_framing_summary(render, job.framing),
                # 输出设置（PNG 隔离）有没有被污染 —— 可审计、且计入闸门
                "output": _render_output_summary(render),
                # 工程脏标记（O3），与 v4 任务同一形状
                "project": _project_dirty_summary(
                    (baseline.project or {}).get("dirty"),
                    await self._safe_project_state(),
                ),
            }
            self._finish(job, JOB_DONE, result=result)
        except errors.ToonTunerError as exc:
            self._finish(job, JOB_FAILED, error=exc.to_payload()["error"])
        except Exception as exc:  # noqa: BLE001 - 兜底，避免工作线程静默死亡
            self._finish(
                job,
                JOB_FAILED,
                error={
                    "code": errors.INTERNAL_ERROR,
                    "message": f"{type(exc).__name__}: {exc}",
                    "retryable": True,
                },
            )

    @staticmethod
    def _is_superseded(job: Job) -> bool:
        return job.superseded

    # -- v4 任务（L0 + Cel 同一生命周期）----------------------------------
    async def _run_surface_job(self, job: Job) -> None:
        """v4 任务的固定序列：

        检查身份与结构 → 恢复基线（L0 + Cel）→ 应用完整草稿 → 回读校验 →
        渲染预览 → 恢复基线并校验。

        三条不变量：

        * **身份 / 结构闸门在任何写入之前** —— 结构变了就不能写，这条比「渲染出图」重要；
        * **失败即恢复** —— 任何一步失败都尝试恢复 L0 与 Cel 两条基线，并把两部分的
          恢复结果分开报出（``restore.l0`` / ``restore.cel``）；
        * **失败画面不覆盖最后一张成功预览** —— 图片端点只服务 ``done`` 任务，
          失败任务的 ``job_id`` 取不到图，前端自然保留上一张成功的。
        """
        job.status = JOB_RUNNING
        job.updated_at = _now_iso()
        plan = job.surface
        steps = job.steps
        scene_touched = False
        try:
            baseline = await self._ensure_baseline()
            async with self._state_lock:
                self._baseline = baseline
            self._surface.require()

            # 1) 身份与结构闸门。提交时的指纹对账只能证明「客户端手里的指纹是新的」；
            #    工程在提交与执行之间仍可能被改动，因此这里用只读探针再对一次账。
            steps.append("检查身份与结构")
            verdict = await self._check_surface()
            job.external_changes = list(verdict.get("value_changed") or [])
            if verdict.get("identity_missing"):
                raise errors.ToonTunerError(
                    errors.IDENTITY_MISSING,
                    "受管对象被重命名或删除（本工具不做猜测迁移）："
                    + "、".join(verdict["identity_missing"][:4]),
                    details={
                        "identity_missing": verdict["identity_missing"],
                        "identity_added": verdict.get("identity_added") or [],
                    },
                )
            if verdict.get("structure_changed"):
                raise errors.ToonTunerError(
                    errors.STRUCTURE_CHANGED,
                    "工程结构已变化（节点增删或色标数量）：草稿与保存确认令牌已作废，"
                    "请刷新基线后再继续。",
                    details={"structure_changed": verdict["structure_changed"]},
                )

            # 2) 恢复基线（L0 + Cel）。从此刻起场景被写过，失败就必须回滚。
            steps.append("恢复基线")
            scene_touched = True
            await self._call(self._binder.apply_values, _baseline_restore_values(baseline))
            await self._call(self._binder.apply_surface_ops, plan.baseline_ops)
            if self._is_superseded(job):
                return self._finish(job, JOB_SUPERSEDED, note="恢复基线后被取代")

            # 3) 应用完整草稿（L0 与 Cel 都在这一步，之后只渲染一次）
            steps.append("应用完整草稿")
            applied = await self._call(self._binder.apply_values, dict(job.effective))
            applied_surface = await self._call(self._binder.apply_surface_ops, plan.ops)
            if self._is_superseded(job):
                return self._finish(job, JOB_SUPERSEDED, note="应用草稿后被取代")

            # 4) 回读校验：两部分都逐项一致才允许渲染
            steps.append("回读校验")
            verified, mismatches = verify_values(job.effective, applied)
            surface_verified, surface_mismatches = verify_ops(
                plan.ops, applied_surface.get("values") or {}
            )
            if not (verified and surface_verified):
                raise errors.ToonTunerError(
                    errors.APPLY_VERIFY_FAILED,
                    "草稿写入后回读不一致，已中止本次预览（磁盘与工程文件未被写入）。",
                    details={
                        "mismatches": mismatches,
                        "surface_mismatches": surface_mismatches,
                    },
                )

            # 5) 渲染（复用既有安全取景：临时预览相机，渲染后必定恢复原相机）
            steps.append("渲染预览")
            expected = {
                "frame_current": (baseline.framing or {}).get("frame_current"),
                "camera": (baseline.framing or {}).get("camera"),
            }
            render = await self._call(
                self._binder.render_preview,
                job.job_id,
                job.framing or None,
                baseline.preview_resolution,
                expected,
            )
            if self._is_superseded(job):
                return self._finish(job, JOB_SUPERSEDED, note="渲染完成后被取代")

            # 6) 恢复基线并**再次回读**（「已恢复」必须有依据）
            steps.append("恢复基线并校验")
            readback = await self._call(
                self._binder.apply_values, _baseline_restore_values(baseline)
            )
            restore_verified, restore_mismatches = verify_values(baseline.values, readback)
            restored_surface = await self._call(
                self._binder.apply_surface_ops, plan.baseline_ops
            )
            surface_restore_verified, surface_restore_mismatches = verify_ops(
                plan.baseline_ops, restored_surface.get("values") or {}
            )

            result = {
                "preview_url": preview_url_for(job.job_id),
                "render_resolution": render.get("render_resolution"),
                "size_bytes": render.get("size_bytes"),
                "applied": values_from_read(applied),
                "applied_surface": param_values(plan.ops),
                "restored": values_from_read(readback),
                "restored_surface": param_values(plan.baseline_ops),
                "baseline_id": baseline.baseline_id,
                "surface_baseline_id": plan.surface_baseline_id,
                "structure_hash": plan.structure_hash,
                "restore_verified": restore_verified,
                "restore_mismatches": restore_mismatches,
                "surface_restore_verified": surface_restore_verified,
                "surface_restore_mismatches": surface_restore_mismatches,
                "external_changes": list(job.external_changes),
                "parameters": _parameter_records(baseline, job, applied),
                "view_transform": baseline.view_transform,
                "framing": _render_framing_summary(render, job.framing),
                # 输出设置（PNG 隔离）有没有被污染 —— 可审计、且计入闸门
                "output": _render_output_summary(render),
                # 工程脏标记（O3）：本工具不保存工程；写回原值也不会清除 Blender
                # 的脏标记。这里如实报出「基线时是否干净 / 预览后是否变脏」。
                "project": _project_dirty_summary(
                    (baseline.project or {}).get("dirty"),
                    await self._safe_project_state(),
                ),
            }
            self._finish(job, JOB_DONE, result=result)
        except errors.ToonTunerError as exc:
            restore = await self._restore_all(plan, scene_touched=scene_touched)
            self._finish(
                job,
                JOB_FAILED,
                error=_surface_error_payload(
                    exc,
                    restore=restore,
                    steps=steps,
                    external_changes=job.external_changes,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，避免工作线程静默死亡
            restore = await self._restore_all(plan, scene_touched=scene_touched)
            self._finish(
                job,
                JOB_FAILED,
                error=_surface_error_payload(
                    errors.ToonTunerError(
                        errors.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
                    ),
                    restore=restore,
                    steps=steps,
                    external_changes=job.external_changes,
                ),
            )

    async def _check_surface(self) -> dict[str, Any]:
        """只读探针 → 三层比对（身份 / 结构 / 值）。"""
        raw = await self._call(self._binder.describe_surface)
        describe = surface_probe.describe_groups(surface_probe.redact_describe(raw))
        return self._surface.check(describe).to_public()

    async def check_surface_structure(self) -> dict[str, Any]:
        """公开的 v4 结构复核（保存流程在写入之前调用）。

        返回 ``Verdict.to_public()``：``identity_missing`` / ``structure_changed``
        非空即表示草稿与确认令牌都该作废。
        """
        return await self._check_surface()

    async def _restore_all(self, plan: SurfacePlan | None, *, scene_touched: bool) -> dict[str, Any]:
        """失败路径：恢复 L0 与 Cel 两条基线，并分别回读校验。"""
        block = _empty_restore(None if scene_touched else "尚未写入场景，无需回滚。")
        if not scene_touched:
            return block
        block["attempted"] = True
        block["l0"] = await self._restore_l0_block()
        block["cel"] = await self._restore_cel_block(plan)
        verified = bool(block["l0"]["verified"]) and bool(block["cel"]["verified"])
        block["verified"] = verified
        block["code"] = None if verified else errors.ROLLBACK_FAILED
        return block

    async def _restore_l0_block(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "attempted": False,
            "verified": False,
            "mismatches": [],
            "code": None,
            "error": None,
        }
        baseline = self._baseline
        out["attempted"] = True
        if baseline is None:
            out["code"] = errors.ROLLBACK_FAILED
            out["error"] = {
                "code": errors.NO_BASELINE,
                "message": "基线已丢失，无法恢复到提交前的 L0 取值。",
            }
            return out
        try:
            readback = await self._call(
                self._binder.apply_values, _baseline_restore_values(baseline)
            )
        except errors.ToonTunerError as exc:
            out["code"] = errors.ROLLBACK_FAILED
            out["error"] = exc.to_payload()["error"]
            return out
        except Exception as exc:  # noqa: BLE001 - 回滚自身不得再抛，否则会盖掉原始失败原因
            out["code"] = errors.ROLLBACK_FAILED
            out["error"] = {
                "code": errors.INTERNAL_ERROR,
                "message": f"{type(exc).__name__}: {exc}",
            }
            return out
        verified, mismatches = verify_values(baseline.values, readback)
        out["verified"] = verified
        out["mismatches"] = mismatches
        if not verified:
            out["code"] = errors.ROLLBACK_FAILED
        return out

    async def _restore_cel_block(self, plan: SurfacePlan | None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "attempted": False,
            "verified": False,
            "mismatches": [],
            "code": None,
            "error": None,
        }
        if plan is None or not plan.baseline_ops:
            # 「无事可做」不等于「没恢复成」：整体 verified 取两者的与，
            # 这里若留 False，一次没有 Cel 写入的回滚就会被误报成失败。
            out["verified"] = True
            out["applicable"] = False
            out["reason"] = "该任务没有 Cel 写入，无需恢复。"
            return out
        out["attempted"] = True
        try:
            payload = await self._call(self._binder.apply_surface_ops, plan.baseline_ops)
        except errors.ToonTunerError as exc:
            out["code"] = errors.ROLLBACK_FAILED
            out["error"] = exc.to_payload()["error"]
            return out
        except Exception as exc:  # noqa: BLE001 - 同上
            out["code"] = errors.ROLLBACK_FAILED
            out["error"] = {
                "code": errors.INTERNAL_ERROR,
                "message": f"{type(exc).__name__}: {exc}",
            }
            return out
        verified, mismatches = verify_ops(plan.baseline_ops, payload.get("values") or {})
        out["verified"] = verified
        out["mismatches"] = mismatches
        if not verified:
            out["code"] = errors.ROLLBACK_FAILED
        return out

    # -- 工具 ------------------------------------------------------------
    @staticmethod
    def _baseline_project_snapshot(state: dict[str, Any] | None) -> dict[str, Any]:
        """基线时的工程状态 → 只保留布尔值与 basename（绝对路径不入库）。"""
        payload = state or {}
        dirty = payload.get("is_dirty")
        return {
            "dirty": bool(dirty) if dirty is not None else None,
            "file_name": payload.get("file_name"),
        }

    async def _safe_project_state(self) -> dict[str, Any] | None:
        """读工程状态。**纯展示用**：读不到就返回 ``None``，绝不因此让预览任务失败。"""
        try:
            return await self._call(self._binder.read_project)
        except Exception:
            return None

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        async with self._blender_lock:
            return await asyncio.to_thread(fn, *args)

    async def call_binder(self, fn: Callable[..., Any], *args: Any) -> Any:
        """让「应用到工程」等其它服务复用**同一把 Blender 串行锁**。

        保存流程必须与预览排队在同一条串行通道上：否则一次保存可以和一次预览
        同时写场景，回读校验会读到对方刚写进去的值。
        """
        return await self._call(fn, *args)


def validate_draft(draft: Any, baseline: Baseline) -> dict[str, Any]:
    if not isinstance(draft, dict):
        raise errors.ToonTunerError(errors.PARAM_INVALID, "draft 必须是对象。")

    unknown = [k for k in draft if k not in params.BY_ID]
    if unknown:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID, f"存在不在白名单内的参数：{sorted(unknown)}"
        )

    coerced: dict[str, Any] = {}
    for param_id, raw in draft.items():
        spec = params.BY_ID[param_id]
        if spec.type == "float":
            coerced[param_id] = _coerce_float(spec, raw)
        elif spec.type == "enum":
            coerced[param_id] = _coerce_enum(spec, raw, baseline)
        else:  # pragma: no cover - 白名单只有两种类型
            raise errors.ToonTunerError(errors.PARAM_INVALID, f"{param_id} 类型未知。")

    _assert_dependent_enums(coerced, baseline)
    return coerced


def _assert_dependent_enums(coerced: dict[str, Any], baseline: Baseline) -> None:
    """依赖枚举校验：**在调用 Blender 之前**完成（需求 6）。

    ``color.look`` 的合法集合由 ``color.view_transform`` 决定，所以要用**本次草稿
    生效后**的视图变换去查能力表。查不上任何等价项时抛稳定错误
    ``INVALID_DEPENDENT_ENUM``，绝不落成通用的 ``BLENDER_SCRIPT_ERROR``。
    """
    for spec in params.ALL_PARAMS:
        if not spec.depends_on or spec.id not in coerced:
            continue
        parent_value = coerced.get(spec.depends_on, baseline.values.get(spec.depends_on))
        if spec.id == "color.look":
            outcome = color_looks.validate_look(
                coerced[spec.id], parent_value, baseline.look_map
            )
            # 规范化后的值才是真正要写进 Blender 的 value（label 永不直写）
            coerced[spec.id] = outcome["value"]


def _coerce_float(spec: params.ParamSpec, raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise errors.ToonTunerError(errors.PARAM_INVALID, f"{spec.id} 必须是数字。")
    value = float(raw)
    if not math.isfinite(value):
        raise errors.ToonTunerError(errors.PARAM_INVALID, f"{spec.id} 必须是有限数值。")
    if spec.minimum is not None and value < spec.minimum:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID, f"{spec.id} = {value} 低于下限 {spec.minimum}。"
        )
    if spec.maximum is not None and value > spec.maximum:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID, f"{spec.id} = {value} 高于上限 {spec.maximum}。"
        )
    return value


def _coerce_enum(spec: params.ParamSpec, raw: Any, baseline: Baseline) -> str:
    if not isinstance(raw, str):
        raise errors.ToonTunerError(errors.PARAM_INVALID, f"{spec.id} 必须是字符串。")
    if spec.depends_on:
        # 依赖枚举（look）：合法集合由依赖参数决定，静态候选列表说了不算，
        # 统一交给 color_looks.validate_look 在依赖校验阶段判。
        return raw
    allowed = baseline.options.get(spec.binding) or list(spec.options)
    if allowed:
        values = [opt["value"] if isinstance(opt, dict) else str(opt) for opt in allowed]
        if raw not in values:
            raise errors.ToonTunerError(
                errors.PARAM_INVALID, f"{spec.id} = {raw!r} 不在允许取值内：{values}"
            )
    return raw


def normalize_values(
    values: dict[str, Any], look_map: dict[str, list[dict[str, str]]] | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """按依赖映射把一组取值规范成「可直接写入 Blender」的形式（需求 8）。

    look 依赖 ``view_transform``，所以恢复基线、应用草稿都必须走同一套规则，
    **不能盲目回写旧字符串**。
    """
    out = dict(values)
    notes: list[dict[str, Any]] = []
    if "color.look" not in out:
        return out, notes
    view_transform = out.get("color.view_transform")
    outcome = color_looks.resolve_look(out["color.look"], view_transform, look_map)
    if outcome["ok"]:
        if outcome["migrated"]:
            notes.append(
                {
                    "parameter": "color.look",
                    "from": out["color.look"],
                    "to": outcome["value"],
                    "reason": outcome["reason"],
                    "view_transform": view_transform,
                }
            )
        out["color.look"] = outcome["value"]
        return out, notes
    allowed = outcome.get("allowed") or []
    fallback = color_looks.NONE_LOOK if color_looks.NONE_LOOK in allowed else (allowed[0] if allowed else None)
    notes.append(
        {
            "parameter": "color.look",
            "from": out["color.look"],
            "to": fallback,
            "reason": outcome["reason"],
            "view_transform": view_transform,
            "warning": f"look 取值在视图 {view_transform!r} 下无等价项，已回退到 {fallback!r}。",
        }
    )
    out["color.look"] = fallback
    return out, notes


def _display_label(spec: params.ParamSpec, value: Any, look_map: dict[str, list[dict[str, str]]] | None,
                   view_transform: Any) -> Any:
    if spec.binding == "view.look" and isinstance(value, str):
        return color_looks.look_label(
            str(view_transform) if view_transform is not None else None, value
        )
    if isinstance(value, str):
        return value
    return value


def _parameter_records(baseline: Baseline, job: Job, readback: dict[str, Any]) -> dict[str, Any]:
    """run/preset 记录：``configured_value`` / ``effective_value`` / ``display_label`` 分开保存（需求 9）。

    * ``configured_value`` —— 调用方**原样**提交的值（可能是旧预设里的显示标签）；
    * ``effective_value``  —— 实际写进 Blender 的值（一定是真实 identifier）；
    * ``display_label``    —— 界面上该显示成什么。
    """
    actual = values_from_read(readback)
    records: dict[str, Any] = {}
    for spec in params.ALL_PARAMS:
        if spec.id in job.raw_requested:
            configured = job.raw_requested[spec.id]
        else:
            configured = baseline.values.get(spec.id)
        effective_value = actual.get(spec.id, job.effective.get(spec.id))
        records[spec.id] = {
            "configured_value": configured,
            "effective_value": effective_value,
            "display_label": _display_label(spec, effective_value, baseline.look_map, baseline.view_transform),
            "migrated": configured != effective_value,
        }
    return records


def verify_values(expected: dict[str, Any], readback: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    actual = values_from_read(readback)
    mismatches: list[dict[str, Any]] = []
    for param_id, want in expected.items():
        got = actual.get(param_id)
        if isinstance(want, (int, float)) and not isinstance(want, bool) and isinstance(got, (int, float)):
            if abs(float(want) - float(got)) > _FLOAT_TOLERANCE:
                mismatches.append({"id": param_id, "expected": want, "actual": got})
        elif want != got:
            mismatches.append({"id": param_id, "expected": want, "actual": got})
    return (not mismatches), mismatches
