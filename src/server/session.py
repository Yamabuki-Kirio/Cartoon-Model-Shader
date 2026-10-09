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
import math
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from . import color_looks, errors, framing as framing_module, params
from .binder import PREVIEW_FALLBACK_RESOLUTION, BlenderBinder, preview_resolution_for

#: 保留的历史任务上限（防止长时间运行内存无界增长）
MAX_JOBS = 50

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


def preview_url_for(job_id: str) -> str:
    """预览图的唯一 HTTP 端点。

    落盘位置在系统临时目录（``%TEMP%/toon-tuner-previews``），那是**本机文件
    系统路径**，浏览器既不能也不应直接把它当作 ``img.src``。前端一律走这个
    端点取图，服务端按 ``job_id`` 决定实际文件，不接受任何路径参数。
    """
    return f"/api/preview/{job_id}"


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

    @property
    def preview_resolution(self) -> tuple[int, int, int]:
        return preview_resolution_for(self.render)

    @property
    def view_transform(self) -> str | None:
        value = self.values.get("color.view_transform")
        return str(value) if value is not None else None


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
    )


class PreviewService:
    """基线与预览任务的唯一权威。"""

    def __init__(self, binder: BlenderBinder) -> None:
        self._binder = binder
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
        }

    async def capture_baseline(self) -> dict[str, Any]:
        """采集内存基线：曝光/辉光现值 + 取景快照 + look 依赖枚举能力表。

        * 取景快照包含 ``frame_current`` / ``scene.camera`` / 相机 transform /
          ``lens`` / ``shift_x`` / ``shift_y`` / 角色世界变换与包围盒。
        * look 能力表是 **``view_transform`` -> 合法 look 档位** 的完整映射，
          由只读探针一次扫出（Blender 侧在 ``finally`` 恢复原状态）。
          有了它，前端切换视图变换时无需再问 Blender，后端也能在**下发脚本前**
          就判定 look 是否合法。
        """
        baseline = await self._capture()
        async with self._state_lock:
            self._baseline = baseline
        return self.baseline_public() or {}

    async def _capture(self) -> Baseline:
        payload = await self._call(self._binder.read_exposure)
        context = await self._call(self._binder.read_framing)
        capability = await self._call(self._binder.read_look_capability)
        return baseline_from_payload(
            payload,
            framing_module.baseline_snapshot(context),
            capability.get("looks") or {},
        )

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

    # -- 工具 ------------------------------------------------------------
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
