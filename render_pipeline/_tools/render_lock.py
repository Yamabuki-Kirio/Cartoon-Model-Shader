# -*- coding: utf-8 -*-
"""
跨进程渲染锁（v3.1）
=====================================================================================
目的只有一个：**同一个模型不会同时有两个 Blender 在跑**。

为什么需要它：状态机里的 `threading.Lock` 只能保护一个进程内部。
但"自动续跑"和"页面按钮"如果分别落在两个进程里（例如用户把 开始渲染.py 跑了两次，
或者手工另开了一个确认服务），内存锁就挡不住了。所以再加一层文件系统锁：
`O_CREAT|O_EXCL` 是原子的，谁先建谁拿到。

约定：
- 每个模型一把锁，路径 = <locks_dir>/<指纹前16位>.render.lock
- 拿到锁的进程把 pid/来源/时间写进锁文件（便于事后取证）
- 退出时删除；异常退出留下的陈旧锁（超时且 pid 已不存在）会被回收

用法：
    from render_lock import RenderLock, LockBusy
    try:
        with RenderLock(path, tag="server:auto"):
            do_render()
    except LockBusy as e:
        ...
"""
import io
import json
import os
import time


class LockBusy(Exception):
    """锁被占用 —— 说明已经有另一个渲染在跑。"""

    def __init__(self, holder):
        self.holder = holder or {}
        super().__init__("已有渲染在进行中：%s" % json.dumps(self.holder, ensure_ascii=False))


def _pid_alive(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            k.CloseHandle(h)
            return True
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


class RenderLock(object):
    def __init__(self, path, tag="", stale_sec=6 * 3600):
        self.path = path
        self.tag = tag
        self.stale_sec = stale_sec
        self.fd = None
        self.holder = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        info = {"pid": os.getpid(), "tag": self.tag,
                "since": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time()}
        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self._read()
                # 陈旧锁回收：超过 stale_sec 且持有者进程已不存在
                age = time.time() - float(holder.get("ts") or 0)
                if age > self.stale_sec and not _pid_alive(holder.get("pid")):
                    try:
                        os.remove(self.path)
                        continue
                    except Exception:
                        pass
                self.holder = holder
                raise LockBusy(holder)
            else:
                with io.open(fd, "w", encoding="utf-8") as f:
                    json.dump(info, f, ensure_ascii=False)
                self.fd = fd
                return self
        raise LockBusy(self._read())

    def _read(self):
        try:
            with io.open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def release(self):
        try:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
        except Exception:
            pass
        try:
            os.remove(self.path)
        except Exception:
            pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
