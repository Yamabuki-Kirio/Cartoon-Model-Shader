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
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import render_state
from .redact import redact

_LOG_CAP = 200
_MEMORY_JOB_CAP = 200
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
    #: 去重键（PR #8）：模型指纹 + 设置指纹的合成键。
    #: ★ 与 ``fingerprint`` 分开存：指纹是「模型身份」，要去前端展示；
    #:   去重键是内部判定，两者混用会让前端看到一个拼接过的怪字符串。
    dedup_key: str = ""
    state: str = "queued"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    logs: list[str] = field(default_factory=list)
    #: 展示用材质清单（来自 main 预检产物；不在此接受客户端答案）
    materials: list[dict[str, Any]] = field(default_factory=list)
    confirm_url: str | None = None
    #: 用户固定：固定后的任务**绝不**被自动清理。
    pinned: bool = False
    #: 产物目录当前占用的字节数（惰性统计，用于容量治理与前端展示）。
    disk_bytes: int = 0
    #: 取消原因（取消时需要保留，供诊断）。
    cancel_reason: str | None = None
    #: 归一化运行终态（run_contract 词表：success / rejected / diagnostic / failed）。
    #: 仅终态任务有值；None 表示尚未落定。前端据此区分四种终态，**不再靠猜**。
    outcome: str | None = None
    #: 是否可交付。**只有 success 为 True**（规格 §3.3）。
    shippable: bool = False
    #: 稳定错误码（非成功时；成功为 None）。供前端给出可操作提示而非裸 traceback。
    error_code: str | None = None
    #: 是否为「兼容降级判定」：render.done 缺失，但退出码 0 且确有合法产物
    #: （规格 §3.2 第五行）。降级不等于造假，但必须如实披露。
    degraded: bool = False
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
            "pinned": self.pinned,
            "disk_bytes": self.disk_bytes,
            "retention_state": self.retention_state,
            "outcome": self.outcome,
            "shippable": self.shippable,
            "error_code": self.error_code,
            "degraded": self.degraded,
            "cancel_reason": self.cancel_reason,
            # ★ PR #8 规格 §五「任务响应」：任务详情要能展示使用的方案、版本与
            #   实际生效参数。settings 里已存着它们 —— 这里取出来单独成块，
            #   前端不必去猜 settings 的键名。
            "scheme": self.scheme_block(),
        }
        if include_logs:
            out["logs"] = list(self.logs)
        if include_artifacts:
            out["artifacts"] = self.list_artifacts()
        return out

    def scheme_block(self) -> dict[str, Any] | None:
        """任务使用的渲染方案（id / version / 规范化参数）。

        没有方案信息时返回 None（例如旧任务记录），前端据此显示「未记录方案」——
        而不是显示一个看起来正常的空块。
        """
        sid = self.settings.get("scheme")
        if not sid:
            return None
        return {
            "id": str(sid),
            "version": self.settings.get("scheme_version"),
            "parameters": dict(self.settings.get("scheme_parameters") or {}),
        }

    @property
    def retention_state(self) -> str:
        """保留状态（active / pinned / kept / expired / cancelled / failed）。"""
        if not self.is_terminal:
            return "active"
        if self.pinned:
            return "pinned"
        if self.state == "cancelled":
            return "cancelled"
        if self.state == "failed":
            return "failed"
        return "kept"

    def list_artifacts(self) -> list[dict[str, Any]]:
        """扫输出目录，返回 opaque artifact_id <-> 文件名 的映射。

        同时扫描任务根目录与 ``artifacts/`` 子目录：历史任务可能用扁平布局，
        新任务用分类布局，两种都要能列出来。
        """
        roots: list[Path] = []
        root = Path(self.output_dir)
        if root.is_dir():
            roots.append(root)
        sub = root / "artifacts"
        if sub.is_dir():
            roots.append(sub)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for base in roots:
            for p in sorted(base.iterdir()):
                if not p.is_file() or p.suffix.lower() not in _ARTIFACT_EXTS:
                    continue
                if p.name in seen:
                    continue
                seen.add(p.name)
                items.append({
                    "artifact_id": f"art-{p.name}",
                    "name": p.name,
                    "size": p.stat().st_size,
                })
        return items

    def refresh_disk_bytes(self) -> int:
        """重新统计产物目录占用字节数（不跟随符号链接）。"""
        from .render_storage import path_size_no_follow

        try:
            self.disk_bytes = path_size_no_follow(self.output_dir)
        except OSError:
            self.disk_bytes = 0
        return self.disk_bytes


class RenderJobManager:
    def __init__(
        self,
        output_root: str | os.PathLike,
        *,
        max_memory_jobs: int = _MEMORY_JOB_CAP,
        history_index_path: str | os.PathLike | None = None,
    ) -> None:
        self._output_root = Path(output_root)
        self._jobs: dict[str, RenderJob] = {}
        self._by_fingerprint: dict[str, str] = {}
        self._active: dict[str, Any] = {}  # job_id -> 子进程句柄（仅运行时）
        #: 内存中最多保留多少条任务（仅淘汰**终态**任务，活跃任务永不淘汰）。
        self._max_memory_jobs = max(1, int(max_memory_jobs))
        #: 历史任务摘要索引（小型 JSON；不存图片/视频/完整日志）。
        self._history_index_path = (
            Path(history_index_path) if history_index_path else (self._output_root / ".jobs_index.json")
        )

    # -- 建任务（同模型 + 同设置去重） -----------------------------------
    @staticmethod
    def settings_key(settings: dict[str, Any]) -> str:
        """设置指纹（PR #8 规格 §五）。

        规格点名去重指纹必须包含：模型身份、方案 ID、方案版本、规范化方案参数、
        画面模式、辉光参数、自动构图设置。

        「模型身份」由调用方通过 ``fingerprint`` 传入（与 ``_by_fingerprint`` 并置），
        这里负责把**其余**部分算成一个稳定字符串。

        ★ 为什么必须包含方案：过去去重只看模型指纹，于是「同一个模型先出经典灯光、
          再出体积雾」会被判定为同一任务而复用 —— 用户改了方案，却拿到上一版方案的
          产物。不同方案**绝不**能复用同一任务。
        排序的 key 保证字段顺序不影响结果；``scheme_parameters`` 已由注册表归一化
        （缺省项也补齐），因此「没给参数」与「显式给了默认值」指纹一致 —— 这是对的，
        两者跑出来的画面确实相同。
        """
        payload = {
            "scheme": str(settings.get("scheme") or ""),
            "scheme_version": settings.get("scheme_version"),
            "scheme_parameters": dict(settings.get("scheme_parameters") or {}),
            "mode": settings.get("mode"),
            "glow": {
                k: settings.get(k) for k in (
                    "glow_threshold", "glow_strength", "glow_size",
                    "glow_smoothness", "glow_quality",
                )
            },
            "auto_frame": bool(settings.get("auto_frame", False)),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def active_for_fingerprint(self, fingerprint: str) -> RenderJob | None:
        """返回该去重键下**仍在活动**的任务（用于在生成 ID/目录**之前**判断能否复用）。

        ★ 参数是「模型指纹 + 设置指纹」的合成键（见 :meth:`dedup_key`）——
        只传模型指纹会让不同方案错误复用。
        """
        existing = self._by_fingerprint.get(fingerprint)
        if not existing:
            return None
        old = self._jobs.get(existing)
        if old is not None and not old.is_terminal:
            return old
        return None

    @classmethod
    def dedup_key(cls, fingerprint: str, settings: dict[str, Any]) -> str:
        """模型身份 + 设置指纹的合成去重键。"""
        return "%s|%s" % (fingerprint, cls.settings_key(settings))

    def create(
        self,
        job_id: str,
        model_id: str,
        fingerprint: str,
        model_name: str,
        settings: dict[str, Any],
        output_dir: str,
        *,
        dedup_key: str | None = None,
    ) -> tuple[RenderJob, bool]:
        """建任务，返回 ``(job, created)``。

        ``created=False`` 表示**同模型 + 同设置**已有活跃任务，本次复用旧任务 ——
        调用方据此决定是否入队（绝不能对复用任务重复入队，否则同一任务会被执行两次）。

        ``fingerprint`` 是模型身份（前端展示用）；``dedup_key`` 是含方案与参数的
        合成键（PR #8）。不传 ``dedup_key`` 时退化为仅按模型指纹去重 ——
        那只适用于「设置无关」的调用方，渲染路径一律显式传。
        """
        key = dedup_key or fingerprint
        existing = self._by_fingerprint.get(key)
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
            dedup_key=key,
        )
        self._jobs[job_id] = job
        self._by_fingerprint[key] = job_id
        self._enforce_memory_cap()
        return job, True

    # -- 内存治理 --------------------------------------------------------
    def _enforce_memory_cap(self) -> None:
        """超出内存上限时淘汰最旧的**终态**任务；活跃任务永不淘汰。

        淘汰前把摘要写进小型 JSON 历史索引（不存图片/视频/完整日志）。
        """
        if len(self._jobs) <= self._max_memory_jobs:
            return
        terminal = [j for j in self._jobs.values() if j.is_terminal]
        # 最旧的在前（按 updated_at 字符串升序即时间升序）
        terminal.sort(key=lambda j: j.updated_at)
        overflow = len(self._jobs) - self._max_memory_jobs
        evicted = terminal[:overflow]
        for job in evicted:
            self._append_history_index(job)
            self._jobs.pop(job.job_id, None)
            key = job.dedup_key or job.fingerprint
            if self._by_fingerprint.get(key) == job.job_id:
                self._by_fingerprint.pop(key, None)

    def _append_history_index(self, job: RenderJob) -> None:
        """把一条任务摘要追加到小型 JSON 索引（失败不影响主流程）。"""
        try:
            self._history_index_path.parent.mkdir(parents=True, exist_ok=True)
            existing: list[dict[str, Any]] = []
            if self._history_index_path.is_file():
                import json

                try:
                    existing = json.loads(self._history_index_path.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except (json.JSONDecodeError, OSError):
                    existing = []
            existing.append(
                {
                    "job_id": job.job_id,
                    "model_id": job.model_id,
                    "model_name": job.model_name,
                    "state": job.state,
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                    "disk_bytes": job.disk_bytes,
                    "pinned": job.pinned,
                }
            )
            # 索引本身也有上限，避免无界增长。
            existing = existing[-self._max_memory_jobs :]
            import json

            self._history_index_path.write_text(
                json.dumps(existing, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def history_index(self) -> list[dict[str, Any]]:
        """读取历史索引（供审计/展示）。"""
        import json

        if not self._history_index_path.is_file():
            return []
        try:
            data = json.loads(self._history_index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []

    # -- 状态机 ----------------------------------------------------------
    def set_state(self, job_id: str, new_state: str) -> RenderJob:
        job = self._must(job_id)
        old = job.state
        if not render_state.can_transition(old, new_state):
            raise ValueError(f"非法状态迁移：{old} -> {new_state}")
        job.state = new_state
        job.updated_at = _now()
        return job

    def succeed(self, job_id: str, *, error_code: str | None = None) -> RenderJob:
        return self.settle(job_id, "success", error_code=error_code)

    def fail(
        self,
        job_id: str,
        reason: str = "failed",
        *,
        error_code: str | None = None,
    ) -> RenderJob:
        state = reason if reason in render_state.TERMINAL_STATES else "failed"
        return self.settle(job_id, state, error_code=error_code)

    def settle(
        self,
        job_id: str,
        outcome: str,
        *,
        error_code: str | None = None,
        degraded: bool = False,
    ) -> RenderJob:
        """按 run_contract 归一化终态落定任务，并同步 ``outcome`` / ``shippable``。

        ``outcome`` 接受大写 token（``SUCCESS`` / ``REJECTED`` / ``DIAGNOSTIC`` /
        ``FAILED``）或小写终态名；内部一律过 ``render_contract.normalize``，
        **未知 token 归为 failed**（规格 §3.3：未知终态 token 一律失败）。

        ``shippable`` 只由契约决定：**只有 success 为 True**。调用方无法用参数把它
        抬成 True —— 这正是 §3.3 硬约束要的「绝不以文件存在或退出码 0 单独判成功」。

        注意：``cancelled`` 不是 run_contract 的正式终态，取消走 ``cancel()`` →
        ``_force_terminal()``，**不经过本方法**（否则会被 normalize 成 failed）。
        """
        from . import render_contract

        job = self._must(job_id)
        if job.is_terminal:
            return job
        normalized = render_contract.normalize(outcome)
        job.state = normalized  # success/rejected/diagnostic/failed 与 render_state 同名
        job.outcome = normalized
        job.shippable = render_contract.is_shippable(normalized)
        job.error_code = None if normalized == "success" else (error_code or None)
        # 降级披露只对 success 有意义：非成功态本身就是失败，无需再标注降级。
        job.degraded = bool(degraded) and normalized == "success"
        job.updated_at = _now()
        return job

    def _force_terminal(self, job_id: str, state: str) -> RenderJob:
        """直接落到终止态：终止态是所有正常路径的合法终点，不受迁移表限制。

        仅用于 **cancelled**（run_contract 没有这个终态，不能走 ``settle``）。
        不写 ``outcome``：取消不是一次「运行终态」，前端据此不显示四种正式终态文案。
        """
        job = self._must(job_id)
        if job.is_terminal:
            return job
        job.state = state
        job.updated_at = _now()
        return job

    def cancel(self, job_id: str, *, reason: str | None = None) -> RenderJob:
        """标记取消（状态流转）。

        **不在此终止进程**：整棵进程树的终止由编排层（``RenderPipelineService.cancel_job``）
        通过进程注册表统一执行 —— 否则会漏掉 MCP 派发的真实 Blender。
        """
        job = self._must(job_id)
        if not render_state.can_cancel(job.state):
            raise ValueError(f"当前状态 {job.state} 不可取消")
        job.cancel_reason = reason
        return self._force_terminal(job_id, "cancelled")

    def retry(self, job_id: str) -> RenderJob:
        job = self._must(job_id)
        if not render_state.is_retryable(job.state):
            raise ValueError(f"当前状态 {job.state} 不可重试")
        # 重置为排队，交还给编排层重新执行。终态字段必须一并清空：
        # 否则旧 outcome/shippable 会在新跑完成前就把任务标成可交付。
        job.state = "queued"
        job.outcome = None
        job.shippable = False
        job.error_code = None
        job.degraded = False
        job.cancel_reason = None
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

    def merge_settings(self, job_id: str, patch: dict[str, Any]) -> None:
        """把回读到的**运行时事实**并进任务 settings（PR #8）。

        用于吸收 ``run_manifest.json`` 里的 ``render_scheme`` 与 ``scene_restore``：
        脚本实际用的方案与场景是否已恢复，只有渲染结束才知道。只做浅合并，
        且调用方须保证 patch 里的键是白名单（不含任何本机路径）。
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.settings.update(patch)

    def attach_proc(self, job_id: str, proc: Any) -> None:
        self._active[job_id] = proc

    def detach_proc(self, job_id: str) -> None:
        self._active.pop(job_id, None)

    def set_pinned(self, job_id: str, pinned: bool) -> RenderJob:
        """固定/取消固定任务。固定后的任务**绝不**被自动清理。"""
        job = self._must(job_id)
        job.pinned = bool(pinned)
        job.updated_at = _now()
        return job

    def purge(self, job_id: str) -> None:
        """从内存中移除任务记录（磁盘清理由调用方负责）。"""
        job = self._jobs.pop(job_id, None)
        key = (job.dedup_key or job.fingerprint) if job is not None else None
        if job is not None and self._by_fingerprint.get(key) == job_id:
            self._by_fingerprint.pop(key, None)
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
