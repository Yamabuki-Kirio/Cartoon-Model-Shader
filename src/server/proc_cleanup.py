"""进程治理：登记渲染相关的进程树，并在取消 / 关闭时终止整棵进程树。

规格《资源治理》第 4 节：
* 入口脚本、确认服务、Blender 都必须登记 PID / 父 PID / 任务 ID / 启动时间 /
  可执行文件身份；
* Windows 用 **Job Object** 管理完整进程树，关闭 Job Object 即统一终止所有子进程；
* MCP 派发 Blender 后必须返回真实 PID（不能只记录入口 Python）。

**重要事实**：真实 Blender 由**常驻 Blender 进程**经 MCP 执行 ``Popen`` 拉起，
**不是入口 Python 的子进程**。因此它不会自动落进入口进程的 Job Object ——
必须先通过入口脚本 stdout 的 ``@@BLENDER_PID@@{"pid": N}`` 取得真实 PID，
再 ``AssignProcessToJobObject`` 显式纳入。``assign`` 可能失败（进程已属其他 Job /
权限不足），此时 ``ProcRecord.assigned=False``，取消/关闭必须走 PID/treekill 兜底。

本模块不依赖第三方库；Job Object 通过 ctypes 调用，非 Windows 或调用失败时
退化为「按登记的 PID 逐个终止 + 兜底 tree kill」，保证不会因为平台差异而完全不清理。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProcRecord:
    pid: int
    parent_pid: int | None
    job_id: str
    started_at: float
    exe: str
    kind: str = "entry"  # entry | confirm | blender
    #: 是否成功加入 Job Object。False 表示取消/关闭时须走 PID/treekill 兜底；
    #: 调用方应据此**明确告警**，不得表现为「治理成功」。
    assigned: bool = False


@dataclass
class ProcessRegistry:
    """内存中的进程登记表（服务生命周期内有效）。

    每个任务可绑定一个 Windows Job Object（``KILL_ON_JOB_CLOSE``）：登记入口脚本
    PID 后 assign 进 Job，Job 关闭即终止该任务**整棵进程树**。

    ⚠ 真实 Blender **不是**入口脚本的子进程 —— 它由**常驻 Blender 进程**经 MCP 执行
    ``Popen`` 拉起。因此它不会自动落进入口进程的 Job Object：必须先从入口脚本 stdout
    的 ``@@BLENDER_PID@@{"pid": N}`` 取得真实 PID，再 ``assign`` 显式纳入；
    assign 失败（``assigned=False``）时取消须走 PID/treekill 兜底。
    **请勿删除这段显式 PID 接线**。
    """

    records: dict[int, ProcRecord] = field(default_factory=dict)
    #: job_id -> WindowsJobObject（仅 Windows 且创建成功时有值）
    jobs: dict[str, Any] = field(default_factory=dict)

    def attach_job_object(self, job_id: str, job_object: Any) -> None:
        self.jobs[job_id] = job_object

    def job_object(self, job_id: str) -> Any:
        return self.jobs.get(job_id)

    def release_job_object(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)

    def register(
        self,
        pid: int,
        *,
        job_id: str,
        exe: str,
        parent_pid: int | None = None,
        kind: str = "entry",
    ) -> ProcRecord:
        rec = ProcRecord(
            pid=pid,
            parent_pid=parent_pid,
            job_id=job_id,
            started_at=time.time(),
            exe=exe,
            kind=kind,
            assigned=False,
        )
        self.records[pid] = rec
        # 若该任务已绑定 Job Object，立即把进程 assign 进去（整树终止的根基）。
        # 必须检查返回值：进程可能已属于其他 Job 或权限不足 —— 那种情况下
        # Job Object 不会收纳它，必须记录 assigned=False 供调用方兜底。
        job_object = self.jobs.get(job_id)
        if job_object is not None and job_object.available:
            try:
                rec.assigned = bool(job_object.assign(pid))
            except Exception as exc:  # noqa: BLE001 - assign 失败不阻断登记，但记录
                rec.assigned = False
                import logging

                logging.getLogger("toon_tuner").warning("AssignProcessToJobObject 失败：%s", exc)
        return rec

    def unregister(self, pid: int) -> None:
        self.records.pop(pid, None)

    def unregister_job(self, job_id: str) -> list[int]:
        """清除该任务**全部**进程记录（入口 + Blender + 确认服务），返回被清除的 PID。

        必须在确认 Job Object 已关闭且进程已回收之后调用：否则残留的 PID 记录会在
        重试后被再次 ``taskkill`` —— 若 Windows 已复用该 PID，可能误杀无关进程。
        """
        removed = [pid for pid, rec in self.records.items() if rec.job_id == job_id]
        for pid in removed:
            self.records.pop(pid, None)
        self.release_job_object(job_id)
        return removed

    def for_job(self, job_id: str) -> list[ProcRecord]:
        return [r for r in self.records.values() if r.job_id == job_id]

    def all(self) -> list[ProcRecord]:
        return list(self.records.values())

    def to_public(self) -> list[dict[str, Any]]:
        return [
            {
                "pid": r.pid,
                "parent_pid": r.parent_pid,
                "job_id": r.job_id,
                "exe": os.path.basename(r.exe),
                "kind": r.kind,
            }
            for r in self.records.values()
        ]


def _pid_alive(pid: int, proc: Any = None) -> bool:
    """判断进程是否仍在运行。

    * 若传入 ``proc``（``subprocess.Popen`` / ``asyncio`` 进程）且有 ``returncode``，
      直接以其为准 —— 已退出的子进程在未回收前，Windows 上 ``OpenProcess`` 仍会成功，
      只靠 OS 句柄会把「已退出但未回收」误判为存活。
    * Windows：``GetExitCodeProcess`` == ``STILL_ACTIVE(259)`` 才算存活。
    * POSIX：``os.kill(pid, 0)``；对自有子进程尝试 ``waitpid(WNOHANG)`` 回收。
    """
    if pid <= 0:
        return False
    if proc is not None:
        rc = getattr(proc, "returncode", None)
        if rc is not None:
            return False
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong(0)
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        else:
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
                if reaped == pid:
                    return False
            except ChildProcessError:
                pass
            os.kill(pid, 0)
            return True
    except (OSError, ValueError):
        return False


def terminate_pid(pid: int, *, force: bool = False) -> bool:
    """终止单个 PID。返回是否成功发出终止信号。"""
    if not _pid_alive(pid):
        return True
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_TERMINATE = 0x0001
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if not handle:
                return False
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
            return True
        sig = 9 if force else 15
        os.kill(pid, sig)
        return True
    except (OSError, ValueError):
        return False


def _tree_kill_by_taskkill(pid: int) -> bool:
    """Windows 兜底：用 taskkill /T 终止整棵进程树。"""
    if os.name != "nt":
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


class WindowsJobObject:
    """Windows Job Object 封装：把进程加入 Job，关闭 Job 即终止全部子进程。

    非 Windows 或创建失败时 ``available=False``，调用方退化为逐 PID 终止。
    """

    def __init__(self) -> None:
        self._handle: Any = None
        self.available = False
        if os.name != "nt":
            return
        try:
            self._handle = self._create()
            self.available = self._handle is not None
        except Exception:  # noqa: BLE001
            self._handle = None
            self.available = False

    @staticmethod
    def _create() -> Any:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        # KILL_ON_JOB_CLOSE：关闭句柄时终止 Job 内所有进程。
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(handle)
            return None
        return handle

    def assign(self, pid: int) -> bool:
        if not self.available or self._handle is None:
            return False
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_SET_QUOTA = 0x0100
            PROCESS_TERMINATE = 0x0001
            handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
            if not handle:
                return False
            try:
                return bool(kernel32.AssignProcessToJobObject(self._handle, handle))
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        if self._handle is not None and os.name == "nt":
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:  # noqa: BLE001
                pass
        self._handle = None
        self.available = False


def terminate_tree(
    pid: int,
    *,
    proc: Any = None,
    job_object: WindowsJobObject | None = None,
    grace_seconds: float = 5.0,
    sleep: Any = time.sleep,
) -> bool:
    """终止 ``pid`` 及其进程树，限时等待回收。

    优先关闭 Job Object（一次性终止整棵树）；否则 taskkill /T；再否则逐 PID terminate→kill。
    传入 ``proc``（``Popen``）时以其 ``returncode`` 判断是否已退出，避免把「已退出未回收」
    误判为存活。返回 True 表示（尽最大努力后）进程已不在。
    """
    if not _pid_alive(pid, proc):
        return True

    if job_object is not None and job_object.available:
        job_object.close()  # KILL_ON_JOB_CLOSE：终止 Job 内所有进程
    elif os.name == "nt":
        _tree_kill_by_taskkill(pid)
    terminate_pid(pid, force=False)

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid, proc):
            return True
        sleep(0.05)

    # 仍未退出：强杀
    terminate_pid(pid, force=True)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid, proc):
            return True
        sleep(0.05)
    return not _pid_alive(pid, proc)


def terminate_job_processes(
    registry: ProcessRegistry,
    job_id: str,
    *,
    grace_seconds: float = 5.0,
) -> list[int]:
    """终止某个任务的**整棵进程树**，返回仍未确认退出的 PID 列表。

    优先关闭该任务的 Job Object（``KILL_ON_JOB_CLOSE`` 一次性终止 Job 内所有进程，
    包含 MCP 派发出来的真实 Blender）；Job 不可用时退化为逐 PID 终止整树。
    """
    unresolved: list[int] = []
    job_object = registry.job_object(job_id)
    records = registry.for_job(job_id)

    if job_object is not None:
        try:
            job_object.close()  # 终止 Job 内所有进程
        except Exception:  # noqa: BLE001
            pass
        registry.release_job_object(job_id)

    for rec in records:
        # 关闭 Job 已覆盖 assigned=True 的进程；未成功 assign 的（含真实 Blender 与
        # 确认服务）必须逐 PID 走 treekill 兜底，否则会漏掉。
        ok = terminate_tree(rec.pid, job_object=None, grace_seconds=grace_seconds)
        if ok:
            registry.unregister(rec.pid)
        else:
            unresolved.append(rec.pid)
    return unresolved
