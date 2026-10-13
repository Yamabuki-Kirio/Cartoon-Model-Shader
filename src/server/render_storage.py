"""渲染资源治理：目录分类、保留策略、安全清理。

设计原则（规格《资源治理》）：
* 所有删除**必须**限制在服务端配置的 ``output/`` 与 ``runtime/`` 根目录内；
  拒绝路径穿越、符号链接与 sibling-prefix（例如 ``C:\\out-evil`` 对上 ``C:\\out``）。
* 正在运行、等待确认、被用户固定的任务**绝不**清理。
* 清理顺序：已取消 → 失败 → 超龄成功 → 超数量最旧 → 超容量最旧。
* 清理失败不得影响其他任务，也**绝不**虚报释放成功。

本模块不导入 FastAPI/Pydantic，纯逻辑 + 文件操作，便于单测。
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

#: 单任务目录内的子目录分类。
ARTIFACT_SUBDIR = "artifacts"
LOG_SUBDIR = "logs"
WORK_SUBDIR = "work"

#: 视为「大文件」的中间/临时产物（失败、取消时优先删除）。
_BIG_SUFFIXES = (".blend", ".blend1", ".mp4", ".mov", ".avi", ".exr", ".png", ".jpg", ".jpeg")


class RetentionState(str, Enum):
    ACTIVE = "active"      # 正在运行 / 等待确认：不得清理
    PINNED = "pinned"      # 用户固定：不得清理
    KEPT = "kept"          # 仍在保留期内
    EXPIRED = "expired"    # 超过保存天数
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class RetentionPolicy:
    max_jobs: int = 30
    max_age_days: int = 14
    max_total_gb: float = 20.0
    max_memory_jobs: int = 200
    keep_failed_logs: bool = True

    @property
    def max_total_bytes(self) -> int:
        return int(self.max_total_gb * (1024 ** 3))


@dataclass
class CleanupReport:
    """一次清理的结果。**绝不虚报**：released_bytes 只累计真正删掉的字节。"""

    removed_jobs: list[str] = field(default_factory=list)
    released_bytes: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        return {
            "removed_jobs": list(self.removed_jobs),
            "released_bytes": self.released_bytes,
            "failed": list(self.failed),
        }


# -- 路径安全 -------------------------------------------------------------


class UnsafePathError(Exception):
    """目标不在允许的根目录内（穿越 / sibling 前缀 / 符号链接）。"""


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def assert_safe_target(target: str | os.PathLike, roots: Sequence[str | os.PathLike]) -> Path:
    """确认目标位于某个允许根目录内，且路径上没有符号链接穿越。

    使用 ``Path.is_relative_to``（而非字符串前缀）—— 字符串前缀会让 ``C:\\out-evil``
    通过 ``C:\\out`` 的判断。
    """
    resolved = Path(target).resolve()
    allowed = [Path(r).resolve() for r in roots]
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise UnsafePathError(f"目标不在允许的根目录内：{target}")
    # 逐级检查符号链接：任一父级是 symlink 就拒绝（防 symlink 逃逸）。
    for parent in [resolved, *resolved.parents]:
        if parent.is_symlink():
            raise UnsafePathError(f"路径包含符号链接：{parent}")
        if parent in allowed:
            break
    return resolved


def safe_remove(path: str | os.PathLike, roots: Sequence[str | os.PathLike]) -> int:
    """删除文件/目录，返回释放的字节数。不在允许根内则拒绝（抛 UnsafePathError）。"""
    target = assert_safe_target(path, roots)
    if not target.exists():
        return 0
    size = dir_size(target) if target.is_dir() else target.stat().st_size
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return size


def dir_size(path: str | os.PathLike) -> int:
    total = 0
    p = Path(path)
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    for root, _dirs, files in os.walk(p):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def path_size_no_follow(path: str | os.PathLike) -> int:
    """计算大小但**不跟随符号链接**（避免把链接指向的外部文件算进来）。"""
    p = Path(path)
    if p.is_symlink():
        return 0
    return dir_size(p)


# -- 保留策略 -------------------------------------------------------------


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return time.mktime(time.strptime(value, fmt))
        except ValueError:
            continue
    return None


def choose_evictions(
    jobs: Iterable[Any],
    policy: RetentionPolicy,
    *,
    now: float | None = None,
) -> list[str]:
    """按规格顺序挑选应清理的 job_id。

    输入 ``jobs`` 是具备以下属性的对象序列：``job_id`` / ``state`` / ``is_terminal`` /
    ``pinned`` / ``updated_at`` / ``disk_bytes``。返回按**清理优先级**排序的 job_id 列表。
    """
    now = now if now is not None else time.time()
    jobs = list(jobs)

    def protected(job: Any) -> bool:
        # 正在运行 / 等待确认 / 固定：都不得清理。
        return (not job.is_terminal) or bool(getattr(job, "pinned", False))

    candidates = [j for j in jobs if not protected(j)]

    def age_key(j: Any) -> float:
        return _parse_iso(j.updated_at) or 0.0

    # 淘汰优先级：取消 / 失败排在前面（更早清理），但**不是无条件立即清理** ——
    # 它们仍需满足「超龄 / 超数量 / 超容量」中至少一个阈值才会被淘汰，
    # 否则失败诊断（keep_failed_logs）会在服务关闭时被立刻删除。
    def priority(j: Any) -> int:
        if j.state == "cancelled":
            return 0
        if j.state == "failed":
            return 1
        return 2

    def evict_order(j: Any) -> tuple[int, float]:
        return (priority(j), age_key(j))

    age_seconds = policy.max_age_days * 86400

    def is_expired(j: Any) -> bool:
        return bool(age_key(j)) and (now - age_key(j)) > age_seconds

    ordered: list[Any] = []

    # 1&2&3：超龄的任务一律优先淘汰（取消/失败排在最前）。
    expired = [j for j in candidates if is_expired(j)]
    ordered += sorted(expired, key=evict_order)

    # 尚未超龄的候选（取消/失败仍在前，但不因状态本身被淘汰）。
    remaining = [j for j in candidates if not is_expired(j)]
    remaining.sort(key=evict_order)

    # 4. 超出数量限制时，从淘汰优先级 + 最旧开始清理。
    if len(remaining) > policy.max_jobs:
        overflow = len(remaining) - policy.max_jobs
        ordered += remaining[:overflow]
        remaining = remaining[overflow:]

    # 5. 超出总容量时继续按同一优先级从最旧清理。
    total = sum(int(getattr(j, "disk_bytes", 0)) for j in remaining)
    for j in remaining:
        if total <= policy.max_total_bytes:
            break
        ordered.append(j)
        total -= int(getattr(j, "disk_bytes", 0))

    # 去重并保持顺序
    seen: set[str] = set()
    result: list[str] = []
    for j in ordered:
        if j.job_id in seen:
            continue
        seen.add(j.job_id)
        result.append(j.job_id)
    return result


def retention_state_of(job: Any, policy: RetentionPolicy, *, now: float | None = None) -> RetentionState:
    if not job.is_terminal:
        return RetentionState.ACTIVE
    if getattr(job, "pinned", False):
        return RetentionState.PINNED
    if job.state == "cancelled":
        return RetentionState.CANCELLED
    if job.state == "failed":
        return RetentionState.FAILED
    now = now if now is not None else time.time()
    ts = _parse_iso(job.updated_at) or 0
    if ts and (now - ts) > policy.max_age_days * 86400:
        return RetentionState.EXPIRED
    return RetentionState.KEPT


# -- 任务目录清理 ---------------------------------------------------------


@dataclass
class JobLayout:
    """单任务目录布局：artifacts / logs / work。"""

    root: Path

    @property
    def artifacts(self) -> Path:
        return self.root / ARTIFACT_SUBDIR

    @property
    def logs(self) -> Path:
        return self.root / LOG_SUBDIR

    @property
    def work(self) -> Path:
        return self.root / WORK_SUBDIR


def cleanup_work_dir(output_dir: str | os.PathLike, roots: Sequence[str | os.PathLike]) -> int:
    """删除任务的 ``work/`` 中间目录，返回释放字节数（成功后调用）。"""
    layout = JobLayout(Path(output_dir))
    if not layout.work.exists():
        return 0
    return safe_remove(layout.work, roots)


def cleanup_failed_job(
    output_dir: str | os.PathLike,
    roots: Sequence[str | os.PathLike],
    *,
    keep_logs: bool = True,
) -> int:
    """失败任务：删除工作目录与**大文件**，保留错误摘要与末尾日志。"""
    layout = JobLayout(Path(output_dir))
    released = cleanup_work_dir(output_dir, roots)
    if not layout.root.is_dir():
        return released
    for p in sorted(layout.root.iterdir()):
        if p.is_dir():
            continue
        if p.name in ("render.done", "job.json"):
            continue
        if p.name.endswith(".log") and keep_logs:
            continue
        if p.suffix.lower() in _BIG_SUFFIXES:
            try:
                released += safe_remove(p, roots)
            except UnsafePathError:
                continue
    return released


def cleanup_cancelled_job(output_dir: str | os.PathLike, roots: Sequence[str | os.PathLike]) -> int:
    """取消任务：清理临时文件与工作目录（不外带最终产物）。"""
    return cleanup_failed_job(output_dir, roots, keep_logs=True)


def purge_job_dir(output_dir: str | os.PathLike, roots: Sequence[str | os.PathLike]) -> int:
    """整目录移除（超保留策略时）。"""
    return safe_remove(output_dir, roots)


# -- 存储服务 -------------------------------------------------------------


class StorageService:
    """把保留策略、任务管理与允许根目录绑在一起，提供用量统计与安全清理。

    ``cleanup()`` 的每一步都单独 try/except：**一个任务清理失败不影响其他任务**，
    也绝不把失败的删除计入 released_bytes（不虚报释放成功）。
    """

    def __init__(self, manager: Any, policy: RetentionPolicy, roots: Sequence[str | os.PathLike]) -> None:
        self._manager = manager
        self._policy = policy
        self._roots = [str(Path(r)) for r in roots]

    def refresh_sizes(self) -> None:
        for job in self._manager.list_jobs():
            job.refresh_disk_bytes()

    def usage(self) -> dict[str, Any]:
        jobs = self._manager.list_jobs()
        total = sum(int(getattr(j, "disk_bytes", 0)) for j in jobs)
        pinned = sum(1 for j in jobs if j.pinned)
        evictable = choose_evictions(jobs, self._policy)
        reclaim = sum(
            int(getattr(self._manager.get(jid), "disk_bytes", 0)) for jid in evictable
        )
        return {
            "ok": True,
            "job_count": len(jobs),
            "pinned_count": pinned,
            "total_bytes": total,
            "max_total_bytes": self._policy.max_total_bytes,
            "max_jobs": self._policy.max_jobs,
            "max_age_days": self._policy.max_age_days,
            "evictable_jobs": len(evictable),
            "reclaimable_bytes": reclaim,
            "roots": [Path(r).name for r in self._roots],
        }

    def cleanup(self, *, dry_run: bool = False) -> CleanupReport:
        """执行（或预演）一次安全清理。

        ``dry_run=True`` 只计算不删除。返回 ``CleanupReport``：真实释放字节数、
        已移除任务、失败列表。失败项**不计入** released_bytes。
        """
        self.refresh_sizes()
        jobs = self._manager.list_jobs()
        evict = choose_evictions(jobs, self._policy)
        report = CleanupReport()
        for job_id in evict:
            job = self._manager.get(job_id)
            if job is None:
                continue
            size = int(getattr(job, "disk_bytes", 0))
            if dry_run:
                report.removed_jobs.append(job_id)
                report.released_bytes += size
                continue
            try:
                released = purge_job_dir(job.output_dir, self._roots)
            except UnsafePathError as exc:
                report.failed.append({"job_id": job_id, "error": str(exc)})
                continue
            except OSError as exc:
                report.failed.append({"job_id": job_id, "error": str(exc)})
                continue
            report.released_bytes += released
            report.removed_jobs.append(job_id)
            self._manager.purge(job_id)
        return report

    def cleanup_after_terminal(self, job: Any) -> int:
        """任务结束后的轻量清理（成功删 work/、失败/取消删大文件）。"""
        try:
            if job.state == "success":
                return cleanup_work_dir(job.output_dir, self._roots)
            if job.state == "cancelled":
                return cleanup_cancelled_job(job.output_dir, self._roots)
            if job.state in ("failed", "rejected", "diagnostic"):
                return cleanup_failed_job(
                    job.output_dir, self._roots, keep_logs=self._policy.keep_failed_logs
                )
        except (UnsafePathError, OSError):
            return 0
        return 0

    def effective_evictions(self) -> list[str]:
        return choose_evictions(self._manager.list_jobs(), self._policy)

    def cleanup_stale_work(self) -> int:
        """服务启动时清理遗留的 ``work/`` 临时目录。

        **从磁盘扫描**，而不是只遍历内存中的任务列表 —— 服务刚启动时任务管理器为空，
        上次异常退出留下的目录一个都发现不了，长期磁盘治理会在重启边界失效。

        扫描 ``<root>/**/work``，只删除名为 ``work`` 的目录（中间文件），**绝不**触碰
        ``artifacts/`` 或最终产物；所有删除限制在允许根目录内。
        """
        released = 0
        for root in self._roots:
            base = Path(root)
            if not base.is_dir():
                continue
            # 只扫任务目录层级（root/<model>/<stamp>/work），深度受限避免全盘遍历。
            for work_dir in base.glob("*/*/work"):
                if not work_dir.is_dir():
                    continue
                try:
                    released += safe_remove(work_dir, self._roots)
                except (UnsafePathError, OSError):
                    continue
        return released

    def recover_layouts(self) -> list[str]:
        """从磁盘恢复任务目录清单（供重启后清理与展示）。

        返回发现的输出目录相对路径列表；不依赖内存中的任务记录。
        """
        found: list[str] = []
        for root in self._roots:
            base = Path(root)
            if not base.is_dir():
                continue
            for candidate in base.glob("*/*"):
                if candidate.is_dir() and candidate.name != "work":
                    found.append(str(candidate))
        return found

    def cleanup_stale_locks(self) -> int:
        """清理过期的渲染锁文件（孤儿锁会让下次渲染误判「已被占用」）。"""
        removed = 0
        for root in self._roots:
            lock_dir = Path(root) / ".locks"
            if not lock_dir.is_dir():
                continue
            for p in lock_dir.glob("*.render.lock"):
                try:
                    if safe_remove(p, self._roots) >= 0:
                        removed += 1
                except (UnsafePathError, OSError):
                    continue
        return removed
