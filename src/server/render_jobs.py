"""渲染任务管理（HTTP 编排层的内存 + 落盘状态）。

职责：
* 建任务、同模型去重（已有活跃任务则直接复用）。
* 状态流转校验（借 render_state 的合法迁移表）。
* 日志脱敏 + 行数上限（沿用主工程的 ``redact``）。
* 产物列举（扫输出目录里的图片/视频）。
* 取消（终止底层子进程）。

**不**实现材质逐条应答 —— 确认由 ``main`` 既有确认页完成，本层只负责展示
「等待确认 + 确认页 URL」。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import render_state
from .redact import redact

_LOG_CAP = 200
_ARTIFACT_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".gif")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


@dataclass
class RenderJob:
    job_id: str
    model_id: str
    fingerprint: str
    model_name: str
    settings: dict[str, Any]
    output_dir: str
    state: str = "queued"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    logs: list[str] = field(default_factory=list)
    #: 展示用材质清单（来自 main 预检产物；不在此接受客户端答案）
    materials: list[dict[str, Any]] = field(default_factory=list)
    confirm_url: str | None = None
    _proc: Any = field(default=None, repr=False, compare=False)

    # -- 派生属性 --------------------------------------------------------
    @property
    def step(self) -> int:
        return render_state.stage_info(self.state)[0]

    @property
    def stage(self) -> str:
        return render_state.stage_info(self.state)[1]

    @property
    def step_count(self) -> int:
        return render_state.STEP_COUNT

    @property
    def is_terminal(self) -> bool:
        return render_state.is_terminal(self.state)

    @property
    def confirmed_count(self) -> int:
        """已确认的**需人工确认**材质数量（不含自动判定的）。"""
        return sum(
            1 for m in self.materials if m.get("requires_user") and m.get("confirmed")
        )

    @property
    def required_count(self) -> int:
        """需要人工确认的材质总数（`requires_user=true`）。

        只统计需人工确认的集合：把自动判定的材质也算进来会让「已确认 x / 共 y」
        与确认页实际要求处理的数量对不上，误导进度。
        """
        return sum(1 for m in self.materials if m.get("requires_user"))

    # -- 序列化（绝不泄漏路径到前端） -----------------------------------
    def to_public(self, include_logs: bool = False, include_artifacts: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "fingerprint": self.fingerprint[:16],
            "state": self.state,
            "step": self.step,
            "stage": self.stage,
            "step_count": self.step_count,
            "settings": dict(self.settings),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "is_terminal": self.is_terminal,
            "materials": list(self.materials),
            "confirm_url": self.confirm_url,
            "confirmed_count": self.confirmed_count,
            "required_count": self.required_count,
        }
        if include_logs:
            out["logs"] = list(self.logs)
        if include_artifacts:
            out["artifacts"] = self.list_artifacts()
        return out

    def list_artifacts(self) -> list[dict[str, Any]]:
        """扫输出目录，返回 opaque artifact_id <-> 文件名 的映射。"""
        root = Path(self.output_dir)
        if not root.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for p in sorted(root.iterdir()):
            if not p.is_file() or p.suffix.lower() not in _ARTIFACT_EXTS:
                continue
            items.append({
                "artifact_id": f"art-{p.name}",
                "name": p.name,
                "size": p.stat().st_size,
            })
        return items


class RenderJobManager:
    def __init__(self, output_root: str | os.PathLike) -> None:
        self._output_root = Path(output_root)
        self._jobs: dict[str, RenderJob] = {}
        self._by_fingerprint: dict[str, str] = {}
        self._active: dict[str, Any] = {}  # job_id -> 子进程句柄（仅运行时）

    # -- 建任务（同模型去重） -------------------------------------------
    def active_for_fingerprint(self, fingerprint: str) -> RenderJob | None:
        """返回该指纹下**仍在活动**的任务（用于在生成 ID/目录**之前**判断能否复用）。"""
        existing = self._by_fingerprint.get(fingerprint)
        if not existing:
            return None
        old = self._jobs.get(existing)
        if old is not None and not old.is_terminal:
            return old
        return None

    def create(
        self,
        job_id: str,
        model_id: str,
        fingerprint: str,
        model_name: str,
        settings: dict[str, Any],
        output_dir: str,
    ) -> tuple[RenderJob, bool]:
        """建任务，返回 ``(job, created)``。

        ``created=False`` 表示同模型已有活跃任务，本次**复用**旧任务 —— 调用方据此
        决定是否入队（绝不能对复用任务重复入队，否则同一任务会被执行两次）。
        """
        existing = self._by_fingerprint.get(fingerprint)
        if existing:
            old = self._jobs.get(existing)
            if old is not None and not old.is_terminal:
                return old, False  # 复用活跃任务，不重复起渲染
        job = RenderJob(
            job_id=job_id,
            model_id=model_id,
            fingerprint=fingerprint,
            model_name=model_name,
            settings=settings,
            output_dir=output_dir,
        )
        self._jobs[job_id] = job
        self._by_fingerprint[fingerprint] = job_id
        return job, True

    # -- 状态机 ----------------------------------------------------------
    def set_state(self, job_id: str, new_state: str) -> RenderJob:
        job = self._must(job_id)
        old = job.state
        if not render_state.can_transition(old, new_state):
            raise ValueError(f"非法状态迁移：{old} -> {new_state}")
        job.state = new_state
        job.updated_at = _now()
        return job

    def succeed(self, job_id: str) -> RenderJob:
        return self._force_terminal(job_id, "success")

    def fail(self, job_id: str, reason: str = "failed") -> RenderJob:
        state = reason if reason in render_state.TERMINAL_STATES else "failed"
        return self._force_terminal(job_id, state)

    def _force_terminal(self, job_id: str, state: str) -> RenderJob:
        """直接落到终止态：终止态是所有正常路径的合法终点，不受迁移表限制。"""
        job = self._must(job_id)
        if job.is_terminal:
            return job
        job.state = state
        job.updated_at = _now()
        return job

    def cancel(self, job_id: str) -> RenderJob:
        job = self._must(job_id)
        if not render_state.can_cancel(job.state):
            raise ValueError(f"当前状态 {job.state} 不可取消")
        proc = self._active.get(job_id)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        return self._force_terminal(job_id, "cancelled")

    def retry(self, job_id: str) -> RenderJob:
        job = self._must(job_id)
        if not render_state.is_retryable(job.state):
            raise ValueError(f"当前状态 {job.state} 不可重试")
        # 重置为排队，交还给编排层重新执行
        job.state = "queued"
        job.logs = []
        job.updated_at = _now()
        return job

    # -- 日志 ------------------------------------------------------------
    def append_log(self, job_id: str, line: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        clean = redact(line)
        if clean:
            job.logs.append(clean)
        if len(job.logs) > _LOG_CAP:
            job.logs = job.logs[-_LOG_CAP:]

    def set_materials(self, job_id: str, materials: list[dict[str, Any]], confirm_url: str | None = None) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.materials = materials
        job.confirm_url = confirm_url

    def attach_proc(self, job_id: str, proc: Any) -> None:
        self._active[job_id] = proc

    def detach_proc(self, job_id: str) -> None:
        self._active.pop(job_id, None)

    def active_procs(self) -> list[Any]:
        """当前活动的渲染子进程句柄（供服务关闭时终止，避免孤儿进程）。"""
        return list(self._active.values())

    # -- 查询 ------------------------------------------------------------
    def get(self, job_id: str) -> RenderJob | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[RenderJob]:
        return list(self._jobs.values())

    def _must(self, job_id: str) -> RenderJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    @property
    def output_root(self) -> Path:
        return self._output_root
