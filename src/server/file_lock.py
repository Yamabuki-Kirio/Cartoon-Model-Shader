"""跨进程建议性文件锁（同一模型同时只允许一个渲染者）。

Windows 用 ``msvcrt.locking``，POSIX 用 ``fcntl.flock``。锁文件本身只是
协调信号，真正的「单渲染者」语义由调用方（渲染编排层）保证：拿到锁才启动
Blender 子进程，释放锁即代表本次渲染已结束。
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockBusy(Exception):
    """锁已被其它进程持有。"""

    def __init__(self, holder: str) -> None:
        super().__init__(f"渲染锁被占用：{holder}")
        self.holder = holder


class FileLockHolder:
    """上下文管理器：进入时抢锁，退出时释放。"""

    def __init__(self, path: str | os.PathLike, *, tag: str, timeout: float = 5.0) -> None:
        self._path = Path(path)
        self._tag = tag
        self._timeout = timeout
        self._fh = None
        self._platform_ctx = None

    def acquire(self) -> "FileLockHolder":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+", encoding="utf-8")
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._lock()
                # 把持有者信息写进锁文件，便于冲突时可读（不含敏感路径）
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(self._tag)
                self._fh.flush()
                return self
            except (OSError, RuntimeError):
                if time.monotonic() >= deadline:
                    holder = ""
                    try:
                        self._fh.seek(0)
                        holder = self._fh.read().strip()
                    except OSError:
                        pass
                    self._fh.close()
                    self._fh = None
                    raise LockBusy(holder or "unknown")
                time.sleep(0.2)

    def _lock(self) -> None:
        if os.name == "nt":
            import msvcrt

            # 锁住整个文件（长度 0 也能锁）
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def release(self) -> None:
        self._unlock()

    def __enter__(self) -> "FileLockHolder":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self._unlock()


@contextmanager
def file_lock(path: str | os.PathLike, *, tag: str, timeout: float = 5.0) -> Iterator[None]:
    """跨进程建议性文件锁上下文管理器。"""
    holder = FileLockHolder(path, tag=tag, timeout=timeout)
    holder.acquire()
    try:
        yield
    finally:
        holder.release()
