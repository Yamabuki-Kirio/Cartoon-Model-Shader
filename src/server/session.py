"""内存基线与预览任务。

核心不变量（需求文档）
----------------------
* 每次预览都 **先恢复内存基线 → 再一次性应用完整草稿**，绝不在上一次结果上叠加。
* 旧任务自动作废：新任务提交时，排队中与运行中的旧任务立即标记为 ``superseded``，
  其结果被丢弃。
* 不使用固定 ``sleep``：任务进度靠状态查询，Blender 侧一律等待响应。

任务通过一个**合并式单工作线程**执行：短时间内的多次提交只会跑最后一个，
既满足「旧任务作废」，又避免 Blender 被连续渲染请求压垮。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import math
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from . import errors, params
from .binder import BlenderBinder

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
    options: dict[str, list[str]]
    render: dict[str, Any]


@dataclass
class Job:
    job_id: str
    seq: int
    status: str
    created_at: str
    updated_at: str
    requested: dict[str, Any]
    effective: dict[str, Any]
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


def _options_from_read(payload: dict[str, Any]) -> dict[str, list[str]]:
    raw = payload.get("view_options") or {}
    options: dict[str, list[str]] = {}
    for spec in params.ALL_PARAMS:
        if not spec.options_dynamic:
            continue
        values = raw.get(spec.binding) or []
        options[spec.binding] = [str(v) for v in values] if values else list(spec.options)
    return options


def baseline_from_payload(payload: dict[str, Any]) -> Baseline:
    return Baseline(
        baseline_id=uuid.uuid4().hex[:12],
        captured_at=_now_iso(),
        blender=str(payload.get("blender", "")),
        glare_present=bool(payload.get("glare_present")),
        values=values_from_read(payload),
        options=_options_from_read(payload),
        render=payload.get("render") or {},
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
            "options": {k: list(v) for k, v in b.options.items()},
            "render": dict(b.render),
        }

    async def capture_baseline(self) -> dict[str, Any]:
        payload = await self._call(self._binder.read_exposure)
        baseline = baseline_from_payload(payload)
        async with self._state_lock:
            self._baseline = baseline
        return self.baseline_public() or {}

    async def _ensure_baseline(self) -> Baseline:
        async with self._state_lock:
            if self._baseline is not None:
                return self._baseline
        return baseline_from_payload(await self._call(self._binder.read_exposure))

    async def restore_baseline(self) -> dict[str, Any]:
        baseline = await self._ensure_baseline()
        async with self._state_lock:
            self._baseline = baseline
        readback = await self._call(self._binder.apply_values, dict(baseline.values))
        verified, mismatches = _verify(baseline.values, readback)
        return {
            "ok": True,
            "baseline_id": baseline.baseline_id,
            "verified": verified,
            "mismatches": mismatches,
            "readback": values_from_read(readback),
        }

    # -- 任务 ------------------------------------------------------------
    async def submit(self, draft: dict[str, Any]) -> Job:
        async with self._state_lock:
            if self._baseline is None:
                raise errors.ToonTunerError(
                    errors.NO_BASELINE, "尚未建立内存基线，无法预览。"
                )
            coerced = _validate_draft(draft, self._baseline)
            effective = dict(self._baseline.values)
            effective.update(coerced)

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
            )
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
            await self._call(self._binder.apply_values, dict(baseline.values))
            if self._is_superseded(job):
                return self._finish(job, JOB_SUPERSEDED, note="恢复基线后被取代")

            job.steps.append("应用完整草稿")
            applied = await self._call(self._binder.apply_values, dict(job.effective))
            if self._is_superseded(job):
                return self._finish(job, JOB_SUPERSEDED, note="应用草稿后被取代")

            job.steps.append("渲染预览")
            render = await self._call(self._binder.render_preview, job.job_id)
            if self._is_superseded(job):
                return self._finish(job, JOB_SUPERSEDED, note="渲染完成后被取代")

            job.steps.append("恢复基线并校验")
            readback = await self._call(self._binder.apply_values, dict(baseline.values))
            verified, mismatches = _verify(baseline.values, readback)

            result = {
                "preview_url": preview_url_for(job.job_id),
                "render_resolution": render.get("render_resolution"),
                "size_bytes": render.get("size_bytes"),
                "applied": values_from_read(applied),
                "restored": values_from_read(readback),
                "baseline_id": baseline.baseline_id,
                "restore_verified": verified,
                "restore_mismatches": mismatches,
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


def _validate_draft(draft: Any, baseline: Baseline) -> dict[str, Any]:
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
    return coerced


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
    allowed = baseline.options.get(spec.binding) or list(spec.options)
    if allowed and raw not in allowed:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID, f"{spec.id} = {raw!r} 不在允许取值内：{allowed}"
        )
    return raw


def _verify(expected: dict[str, Any], readback: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
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
