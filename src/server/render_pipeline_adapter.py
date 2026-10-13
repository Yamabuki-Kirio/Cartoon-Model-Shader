"""渲染流水线编排服务（HTTP 层）。

设计约束（裁决 1=A / 2=A）：
* **不复制、不覆盖** ``render_pipeline/开始渲染.py``；本服务只通过
  ``asyncio.create_subprocess_exec(sys.executable, 固定脚本, 白名单参数)`` 调用它。
* 分类、确认页、渲染锁、``render.done`` 终态判定**全部由固定脚本负责**；
  本层只做：去重建任务、跨进程单执行者锁、监控 stdout 推进状态、读取产物、暴露确认 URL。
* 前端永远只提交 opaque model_id + 白名单设置；真实路径/指纹由服务端从私有注册表解析。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from . import errors, render_contract
from .file_lock import FileLockHolder, LockBusy
from .proc_cleanup import WindowsJobObject, terminate_job_processes
from .render_jobs import RenderJobManager

logger = logging.getLogger("toon_tuner")

CONFIRM_PORT = 8770
_VALID_MODES = ("faithful", "enhanced")
_VALID_LIGHTING = ("volume_fog", "legacy")
_VALID_GLOW_QUALITY = ("High", "Medium", "Low")
_WHITELIST = {"mode", "auto_frame", "lighting", "fog_density",
              "glow_threshold", "glow_strength", "glow_size",
              "glow_smoothness", "glow_quality"}
#: 设置键 → 驱动 CLI 开关（辉光增量；仅 enhanced 模式有意义）。
_GLOW_FLAGS = (("glow_threshold", "--glow-threshold"),
               ("glow_strength", "--glow-strength"),
               ("glow_size", "--glow-size"),
               ("glow_smoothness", "--glow-smoothness"),
               ("glow_quality", "--glow-quality"))

#: 可视为「合法成品图」的扩展名（规格 §6.4：成功必须至少存在一张成品图）。
#: 视频不算成品图：先出图再出视频的管线里，视频缺失不应阻断交付判定。
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

#: 入口脚本（开始渲染.py）驱动的二选一开关：.blend 直开，其余按 PMX 导入。
_BLEND_SUFFIX = ".blend"


def _sha256_file(path: str | Path) -> str:
    """整文件 sha256 —— 与入口脚本/主脚本的 ``sha256_of`` 同口径。

    ★ 与 Web 注册表指纹（大小 + 前 1MB）**不是**同一口径：确认清单文件名、
      侧车映射文件名用的都是整文件 sha256，拿注册表指纹去拼会永远找不到文件。
    """
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _deliverable_images(job: Any) -> list[dict[str, Any]]:
    """任务输出目录里的成品图（规格 §6.4 的「至少一张合法成品图」）。"""
    return [
        art
        for art in job.list_artifacts()
        if Path(str(art.get("name", ""))).suffix.lower() in _IMAGE_SUFFIXES
    ]


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """严格白名单：mode / auto_frame + 灯光辉光增量参数。多余键一律拒绝。"""
    extra = set(settings) - _WHITELIST
    if extra:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID, f"渲染设置含未知字段：{sorted(extra)}", retryable=False
        )
    mode = settings.get("mode", "faithful")
    if mode not in _VALID_MODES:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID, f"mode 必须是 {_VALID_MODES}，收到 {mode!r}", retryable=False
        )
    auto_frame = bool(settings.get("auto_frame", False))
    lighting = settings.get("lighting", "volume_fog")
    if lighting not in _VALID_LIGHTING:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID,
            f"lighting 必须是 {_VALID_LIGHTING}，收到 {lighting!r}", retryable=False,
        )
    try:
        fog_density = float(settings.get("fog_density", 0.018))
    except (TypeError, ValueError):
        raise errors.ToonTunerError(
            errors.PARAM_INVALID,
            f"fog_density 必须是数字，收到 {settings.get('fog_density')!r}", retryable=False,
        )
    if not (0.0 <= fog_density <= 0.1):
        raise errors.ToonTunerError(
            errors.PARAM_INVALID,
            f"fog_density 必须在 0.0–0.1 之间，收到 {fog_density!r}", retryable=False,
        )
    out: dict[str, Any] = {"mode": mode, "auto_frame": auto_frame,
                           "lighting": lighting, "fog_density": fog_density}
    for key in ("glow_threshold", "glow_strength", "glow_size", "glow_smoothness"):
        v = settings.get(key)
        if v is None:
            continue
        try:
            out[key] = float(v)
        except (TypeError, ValueError):
            raise errors.ToonTunerError(
                errors.PARAM_INVALID, f"{key} 必须是数字，收到 {v!r}", retryable=False
            )
    quality = settings.get("glow_quality")
    if quality is not None:
        if quality not in _VALID_GLOW_QUALITY:
            raise errors.ToonTunerError(
                errors.PARAM_INVALID,
                f"glow_quality 必须是 {_VALID_GLOW_QUALITY}，收到 {quality!r}", retryable=False,
            )
        out["glow_quality"] = quality
    return out


class RenderPipelineService:
    def __init__(
        self,
        job_manager: RenderJobManager,
        registry: Any,
        *,
        fixed_render_script: str | Path,
        model_root: str | Path | None = None,
        source_blend: str | Path | None = None,
        blender: str | Path | None = None,
        runtime_dir: str | Path | None = None,
        maps_dir: str | Path | None = None,
        output_root: str | Path | None = None,
        storage: Any = None,
        proc_registry: Any = None,
    ) -> None:
        self._manager = job_manager
        self._registry = registry
        self._fixed = Path(fixed_render_script)
        self._model_root = Path(model_root) if model_root else None
        self._source_blend = Path(source_blend) if source_blend else None
        self._blender = Path(blender) if blender else None
        self._runtime_dir = Path(runtime_dir) if runtime_dir else None
        self._maps_dir = Path(maps_dir) if maps_dir else None
        self._output_root = Path(output_root) if output_root else Path("output")
        self._holders: dict[str, Any] = {}
        #: 资源治理（可选注入）：任务结束后的轻量清理。
        self._storage = storage
        #: 进程登记表（可选注入）：登记入口脚本 PID，供取消/关闭时终止。
        self._proc_registry = proc_registry
        #: 渲染在**常驻后台 worker**里串行执行：由 lifespan 启动，不依赖单个请求的
        #: 事件循环（`asyncio.create_task` 在请求上下文中不可靠）。同一时刻只跑一个渲染，
        #: 跨模型的并发由各模型的跨进程文件锁再兜一层。
        self._queue: asyncio.Queue[str] | None = None
        self._worker: asyncio.Task | None = None

    # -- 生命周期（由 create_app 的 lifespan 调用） ----------------------
    async def start_worker(self) -> None:
        if self._worker is not None:
            return
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._queue = queue
        self._worker = asyncio.create_task(self._run_worker(queue))

    async def stop_worker(self) -> None:
        """关闭：先终止在跑的子进程，再取消 worker，最后清空队列。

        顺序很关键：
        1. **先终止子进程**（并限时等待回收）—— 否则取消 worker 只会执行 finally 里的
           释放锁，而 Blender 子进程仍在渲染，留下孤儿进程且锁已释放。
        2. 再取消 worker。worker 的 `finally` 里用的是**捕获到的局部 queue**，因此
           即使这里随后把 `self._queue` 置空也不会触发 AttributeError。
        3. 最后才清空字段并排空队列。
        """
        await self.terminate_active_processes()

        task = self._worker
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - 关闭阶段不抛出，但记录下来
                logger.warning("渲染 worker 关闭时异常：%s", exc)
        self._worker = None

        # worker 已结束，此时清空队列不会与它的 finally 竞争。
        queue = self._queue
        self._queue = None
        if queue is not None:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def terminate_active_processes(self, *, grace_seconds: float = 5.0) -> list[str]:
        """终止所有活动渲染子进程：先 terminate，限时等待，再 kill 并**再等待回收**。

        返回无法确认退出的作业描述（正常情况下为空）。kill 之后必须再 await
        ``proc.wait()``，否则进程可能尚未回收（Windows 上还会继续占用文件句柄）。
        """
        unresolved: list[str] = []
        deadline = time.monotonic() + grace_seconds

        # 先关闭所有任务的 Job Object —— 一次终止各任务整棵进程树（含真实 Blender）。
        if self._proc_registry is not None:
            for job_id, job_object in list(self._proc_registry.jobs.items()):
                try:
                    job_object.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("关闭任务 %s 的 Job Object 失败：%s", job_id, exc)
                self._proc_registry.release_job_object(job_id)

        for proc in self._manager.active_procs():
            if proc.returncode is not None:
                continue
            try:
                proc.terminate()
            except ProcessLookupError:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("terminate 渲染子进程失败：%s", exc)

        for proc in self._manager.active_procs():
            if proc.returncode is not None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            try:
                await asyncio.wait_for(proc.wait(), timeout=remaining)
                continue
            except asyncio.TimeoutError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("等待渲染子进程退出失败：%s", exc)
            # 超时：强杀，并**再等待一次回收**。
            try:
                proc.kill()
            except ProcessLookupError:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("kill 渲染子进程失败：%s", exc)
            if not await self._await_reaped(proc, timeout=grace_seconds):
                unresolved.append(f"pid={getattr(proc, 'pid', '?')}")
        return unresolved

    async def _await_reaped(self, proc: Any, *, timeout: float) -> bool:
        """等子进程真正被回收（returncode 已置位）。

        `kill()` 之后 `wait()` 可能因为事件循环的子进程回调尚未派发而短暂不返回，
        因此这里直接 `await proc.wait()`（不带 wait_for 包裹，避免与回调竞态），
        再辅以 returncode 轮询兜底。返回 True 表示已回收。
        """
        if proc.returncode is not None:
            return True
        waiter = asyncio.ensure_future(proc.wait())
        deadline = time.monotonic() + max(timeout, 0.5)
        try:
            while proc.returncode is None and time.monotonic() < deadline:
                if waiter.done():
                    break
                await asyncio.sleep(0.02)
            if proc.returncode is None and waiter.done():
                # wait() 已返回但 returncode 尚未可见：直接读结果。
                try:
                    waiter.result()
                except Exception:  # noqa: BLE001
                    pass
            return proc.returncode is not None or waiter.done()
        finally:
            if not waiter.done():
                waiter.cancel()
                try:
                    await waiter
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    def enqueue(self, job_id: str) -> None:
        """把任务交给常驻 worker。worker 未启动时回退为直接后台调度。"""
        if self._queue is not None:
            self._queue.put_nowait(job_id)
        else:  # pragma: no cover - 仅在未走 lifespan 的测试装配下发生
            asyncio.create_task(self._start_guarded(job_id))

    async def _run_worker(self, queue: asyncio.Queue[str]) -> None:
        # 捕获**局部 queue 引用**：即使 stop_worker 之后把 self._queue 置空，
        # finally 里的 task_done 也不会 AttributeError。
        while True:
            job_id = await queue.get()
            try:
                await self._start_guarded(job_id)
            finally:
                queue.task_done()

    async def _start_guarded(self, job_id: str) -> None:
        """执行一个任务；任何异常都必须落到任务状态，绝不静默丢弃。"""
        try:
            await self.start(job_id)
        except errors.ToonTunerError as exc:
            self._manager.append_log(job_id, f"[render] 启动失败：{exc.code} {exc.message}")
            self._manager.fail(job_id)
        except Exception as exc:  # noqa: BLE001 - 兜底
            self._manager.append_log(job_id, f"[render] 未预期错误：{exc}")
            self._manager.fail(job_id)

    # -- 建任务 ----------------------------------------------------------
    def create_job(self, model_id: str, settings: dict[str, Any]) -> tuple[Any, bool]:
        """建任务，返回 ``(job, created)``。

        任务 ID 含**随机后缀**（毫秒时间戳 + uuid4 片段）：仅靠「指纹 + 秒级时间」会在
        同一秒内重建、快速失败重试或并发请求时发生冲突，覆盖旧记录并复用输出目录。
        输出目录使用**同一个唯一 ID**，因此复用/新建都能一一对应。
        """
        settings = validate_settings(settings)
        try:
            _real_path, fingerprint, name = self._registry.resolve(model_id)
        except KeyError:
            raise errors.ToonTunerError(
                errors.MODEL_NOT_FOUND, f"模型不存在：{model_id}", retryable=False
            )
        # 先查活跃任务：能复用就**不生成新 ID、也不创建空目录**（否则并发/重复请求
        # 会留下一堆垃圾目录）。
        existing = self._manager.active_for_fingerprint(fingerprint)
        if existing is not None:
            return existing, False

        stamp = time.strftime("%m%d_%H%M%S") + "-" + uuid.uuid4().hex[:8]
        job_id = f"rj-{fingerprint[:12]}-{stamp}"
        # 输出目录沿用任务 ID（同一唯一标识），新建任务绝不与既有目录重合。
        out_dir = str(self._output_root / model_id / stamp)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        job, created = self._manager.create(
            job_id=job_id,
            model_id=model_id,
            fingerprint=fingerprint,
            model_name=name,
            settings=settings,
            output_dir=out_dir,
        )
        self._registry.remember_last_job(model_id, job.job_id)
        return job, created

    # -- 启动渲染（后台任务调用） ---------------------------------------
    async def start(self, job_id: str) -> None:
        job = self._manager.get(job_id)
        if job is None:
            raise errors.ToonTunerError(errors.JOB_NOT_FOUND, f"任务不存在：{job_id}")
        fp = job.fingerprint
        lock_path = self._lock_path(fp)
        try:
            holder = await asyncio.to_thread(
                FileLockHolder(lock_path, tag=f"http:{job_id}", timeout=5.0).acquire
            )
        except LockBusy as exc:
            raise errors.ToonTunerError(
                errors.RENDER_EXECUTOR_LOCKED,
                "已有同模型的渲染在进行，拒绝重复启动。",
                retryable=True,
                details={"holder": exc.holder},
            )
        self._holders[job_id] = holder
        # 为本次渲染建立进程树治理：Windows 下创建 Job Object 并绑定该任务。
        # 入口脚本会被 assign 进 Job；但真实 Blender 由**常驻 Blender 进程**经 MCP 拉起，
        # 不是入口子进程，必须在解析到 @@BLENDER_PID@@ 后**显式 assign** 才能纳入。
        job_object = None
        if self._proc_registry is not None:
            try:
                job_object = WindowsJobObject()
                if job_object.available:
                    self._proc_registry.attach_job_object(job_id, job_object)
            except Exception as exc:  # noqa: BLE001
                logger.warning("创建 Job Object 失败，将退化为逐 PID 终止：%s", exc)
                job_object = None
        try:
            await self._run_fixed_script(job, fp, job.output_dir)
        finally:
            # 回收 Job（Job 内进程已自行退出，close 仅释放句柄）。
            if job_object is not None:
                try:
                    job_object.close()
                except Exception:  # noqa: BLE001
                    pass
            # **清除该任务全部 PID 记录**（入口 + Blender + 确认服务）。必须在 Job 关闭
            # 与进程回收之后：否则重试后可能对**已被系统复用**的旧 PID 再次 taskkill，
            # 误杀无关进程。
            if self._proc_registry is not None:
                self._proc_registry.unregister_job(job_id)
            try:
                holder.release()
            except Exception:
                pass
            self._holders.pop(job_id, None)
            self._manager.detach_proc(job_id)

    # -- 调用固定脚本 ----------------------------------------------------
    async def _run_fixed_script(self, job: Any, fp: str, out_dir: str) -> None:
        self._manager.set_state(job.job_id, "preflight")
        # 真实路径只在服务端注册表里解析（前端永远只给 opaque model_id）。
        # ★ 过去这里传 ``--fingerprint <注册表指纹>``，而入口脚本的 locate() 按
        #   **整文件 sha256** 反查 —— 两套指纹口径不同，Web 一键渲染对任何模型都会
        #   死在「找不到模型」。改为显式传解析出的路径（--pmx/--blend 二选一）。
        try:
            model_path, _resolved_fp, _name = self._registry.resolve(job.model_id)
        except KeyError:
            raise errors.ToonTunerError(
                errors.MODEL_NOT_FOUND, f"模型已不在注册表中：{job.model_id}", retryable=False
            )
        kind_blend = Path(model_path).suffix.lower() == _BLEND_SUFFIX
        cmd: list[str] = [
            sys.executable,
            str(self._fixed),
            "--blend" if kind_blend else "--pmx",
            str(model_path),
            "--out",
            out_dir,
            "--mode",
            str(job.settings.get("mode", "faithful")),
            "--lighting",
            str(job.settings.get("lighting", "volume_fog")),
            "--fog-density",
            str(job.settings.get("fog_density", 0.018)),
            "--auto-frame",
            "1" if job.settings.get("auto_frame") else "0",
            "--port",
            str(CONFIRM_PORT),
            "--runtime-dir",
            str(self._runtime_dir) if self._runtime_dir else str(Path(out_dir).parent / "runtime"),
        ]
        # 辉光增量：仅在设置了对应字段时透传（缺省让主脚本用默认修复值）。
        for key, flag in _GLOW_FLAGS:
            v = job.settings.get(key)
            if v is not None:
                cmd += [flag, str(v)]
        # 可选参数：仅在服务端确有配置时才透传（缺省让固定脚本回退到环境变量）。
        if self._model_root:
            cmd += ["--model-root", str(self._model_root)]
        if self._source_blend:
            cmd += ["--source-blend", str(self._source_blend)]
        if self._blender:
            cmd += ["--blender", str(self._blender)]
        if self._maps_dir:
            cmd += ["--maps-dir", str(self._maps_dir)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        self._manager.attach_proc(job.job_id, proc)
        # 登记入口脚本进程（PID/父 PID/任务/启动时间/可执行文件身份），供取消与关闭时
        # 终止整棵进程树（含 Blender / 确认服务）。
        if self._proc_registry is not None:
            try:
                self._proc_registry.register(
                    proc.pid,
                    job_id=job.job_id,
                    exe=str(self._fixed),
                    parent_pid=os.getpid(),
                    kind="entry",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("登记渲染进程失败：%s", exc)
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", "replace").rstrip("\n")
            if line:
                self._manager.append_log(job.job_id, line)
                self._observe_line(job.job_id, line, fp)
        code = await proc.wait()
        self._manager.detach_proc(job.job_id)
        if self._proc_registry is not None:
            self._proc_registry.unregister(proc.pid)
        self._manager.append_log(job.job_id, f"[render] subprocess exit code = {code}")
        self._finalize(job.job_id, out_dir, code)

    def _observe_line(self, job_id: str, line: str, fp: str) -> None:
        """把固定脚本的 stdout 映射成**展示用**的中间状态。

        注意：这只是尽力而为的进度提示，**不是事实来源**。任务终态一律由 `_finalize`
        依据 ``render.done`` 与产物文件判定 —— 因此这里即使匹配不到任何关键词，
        也不会导致任务卡在非终止态。
        """
        # 固定的开始渲染.py 通过 MCP 派发 Blender 后会打印稳定事件
        # `@@BLENDER_PID@@{"pid": N}`；必须登记**真实 Blender PID**（不是只记入口
        # Python）才能保证整树终止。`pid` 为 null 表示未能取得 PID。
        if "@@BLENDER_PID@@" in line:
            self._record_dispatched_pid(job_id, line)
        if "预检与自动分类" in line:
            self._safe_transition(job_id, "analyzing_materials")
        elif "材质需要确认" in line:
            self._safe_transition(job_id, "waiting_confirmation")
            # 确认清单文件名用**整文件 sha256**（入口脚本 preflight 的口径），
            # 不是注册表指纹 —— 必须先解析模型路径再算，否则永远读不到文件。
            pipe_fp = self._pipeline_fingerprint(job_id)
            items = self._read_confirmation(pipe_fp) if pipe_fp else None
            if items:
                self._manager.set_materials(job_id, items, confirm_url=self._confirm_url())
        elif "直接渲染" in line or "渲染中" in line:
            self._safe_transition(job_id, "rendering")
        elif "完成（输出" in line:
            self._safe_transition(job_id, "verifying")

    def _record_dispatched_pid(self, job_id: str, line: str) -> None:
        """从 ``@@BLENDER_PID@@`` 行解析并登记真实 Blender PID。

        固定脚本用 ``@@BLENDER_PID@@{"pid": N}`` 报告**真实 Blender**（常驻 Blender 经
        MCP 派发，不是本入口脚本的子进程）。``pid`` 为 null / 解析失败时**明确记录
        「不保证已终止」**，而不是假装成功。
        """
        try:
            payload = line.split("@@BLENDER_PID@@", 1)[1]
            data = json.loads(payload)
            raw_pid = data.get("pid")
            if raw_pid is None:
                raise ValueError("pid is null")
            pid = int(raw_pid)
        except (IndexError, ValueError, KeyError, json.JSONDecodeError):
            # 取不到 PID 时必须**明确告警**（与是否装配了注册表无关）：
            # 这是任务级事实，不能被静默吞掉。
            self._manager.append_log(
                job_id, "[render] 警告：已派发 Blender 但未能取得其 PID，不保证取消/关闭时能终止。"
            )
            return
        if self._proc_registry is None:
            return
        rec = self._proc_registry.register(
            pid,
            job_id=job_id,
            exe=str(self._blender) if self._blender else "blender",
            parent_pid=None,
            kind="blender",
        )
        # Job Object 绑定可能失败（进程已属于其他 Job / 权限不足）：必须显式记录，
        # 否则系统会表现为「治理成功」而实际无法整树终止。
        if not getattr(rec, "assigned", True):
            self._manager.append_log(
                job_id,
                f"[render] 警告：Blender PID {pid} 未能加入 Job Object，取消时将走 PID/treekill 兜底。",
            )

    async def cancel_job(self, job_id: str) -> list[int]:
        """取消任务：终止该任务**整棵进程树**（入口 + MCP 派发的 Blender + 确认服务）。

        返回未能确认退出的 PID 列表（空表示全部已终止）。
        """
        if self._proc_registry is None:
            return []
        return await asyncio.to_thread(
            terminate_job_processes, self._proc_registry, job_id, grace_seconds=5.0
        )

    def _finalize(self, job_id: str, out_dir: str, code: int) -> None:
        """终态判定：规格 §3.2 映射表的**唯一实现点**。

        权威来源是固定脚本写在 ``render.done`` 首行的 token；词表与可交付白名单来自
        ``render_contract``（即 ``render_pipeline/run_contract.py``），**不在本地复制**。

        硬约束（规格 §3.3）：
        * 只有 ``success`` 可 ``shippable=True``，且本方法没有任何参数能把它抬真；
        * ``diagnostic`` 绝不显示为成品，``rejected`` 绝不显示为「渲染失败」；
        * 未知 token 一律失败，并单独标 ``RENDER_UNKNOWN_TERMINAL``；
        * **绝不**以「文件存在」或「退出码 0」单独判断成功 —— token 说 SUCCESS
          但退出码非 0、或没有成品图时，同样判失败。
        """
        job = self._manager.get(job_id)
        if job is None:
            return
        raw_token = self._read_done_token(out_dir)
        images = _deliverable_images(job)
        artifacts = job.list_artifacts()

        if raw_token is not None:
            token = raw_token.strip().upper()
            if not render_contract.is_known_token(token):
                self._settle(
                    job_id,
                    "failed",
                    errors.RENDER_UNKNOWN_TERMINAL,
                    "固定脚本返回无法识别的终态 %r（合法值：%s；退出码 %d，产物 %d 个），"
                    "判定为失败。"
                    % (raw_token, sorted(render_contract.valid_tokens()), code, len(artifacts)),
                )
                return
            outcome = render_contract.token_to_outcome(token)
            if outcome == "success":
                # token 与事实冲突：token 说成功也不行，必须退出码 0 且有成品图。
                if code != 0:
                    self._settle(
                        job_id,
                        "failed",
                        errors.RENDER_SUBPROCESS_FAILED,
                        "固定脚本终态=SUCCESS 但退出码为 %d（token 与退出码冲突），判定为失败。" % code,
                    )
                    return
                if not images:
                    self._settle(
                        job_id,
                        "failed",
                        errors.RENDER_OUTPUT_MISSING,
                        "固定脚本终态=SUCCESS 但未产出任何合法成品图，判定为失败。",
                    )
                    return
                self._manager.append_log(
                    job_id, f"[render] 终态 SUCCESS：成品图 {len(images)} 张，可交付。"
                )
                self._manager.settle(job_id, "success")
                self._after_terminal(job_id)
                return
            if outcome == "rejected":
                self._settle(
                    job_id,
                    "rejected",
                    errors.RENDER_REJECTED,
                    "固定脚本终态=REJECTED（退出码 %d，产物 %d 个）：渲染前被材质准入检查拦下，"
                    "尚未开始正式渲染。需完成材质确认后重试。" % (code, len(artifacts)),
                )
                return
            if outcome == "diagnostic":
                self._settle(
                    job_id,
                    "diagnostic",
                    errors.RENDER_DIAGNOSTIC,
                    "固定脚本终态=DIAGNOSTIC（退出码 %d）：本次产出的是诊断图，不是成品。"
                    % code,
                )
                return
            # 明确的 FAILED。
            self._settle(
                job_id,
                "failed",
                errors.RENDER_SUBPROCESS_FAILED,
                "固定脚本终态=FAILED（退出码 %d，产物 %d 个），判定为失败。" % (code, len(artifacts)),
            )
            return

        # ---- 没有 render.done：按规格 §3.2 后两行做兼容降级 -----------------
        # 旧版入口可能不写 token。此时**只有退出码 0 且确有成品图**才判成功，
        # 且必须如实记录「这是降级判定」；缺任一条件即失败。
        if code == 0 and images:
            self._manager.append_log(
                job_id,
                "[render] 未收到 render.done 终态标记；按兼容规则判定为成功"
                f"（退出码 0，成品图 {len(images)} 张）。这是降级判定，已记录。",
            )
            self._manager.settle(job_id, "success", degraded=True)
            self._after_terminal(job_id)
            return
        if code != 0 and not images:
            self._settle(
                job_id,
                "failed",
                errors.RENDER_SUBPROCESS_FAILED,
                "渲染结束：退出码 %d 且未产出任何合法成品图，判定为失败。" % code,
            )
            return
        if not images:
            self._settle(
                job_id,
                "failed",
                errors.RENDER_OUTPUT_MISSING,
                "渲染结束：退出码 0 但未产出任何合法成品图，判定为失败。",
            )
            return
        self._settle(
            job_id,
            "failed",
            errors.RENDER_SUBPROCESS_FAILED,
            "渲染结束：退出码 %d 且未收到终态标记，判定为失败。" % code,
        )

    def _settle(
        self,
        job_id: str,
        outcome: str,
        error_code: str,
        message: str,
    ) -> None:
        """落终态 + 记日志 + 收尾清理。三件事必须一起做，避免任一步漏掉。"""
        self._manager.append_log(job_id, "[render] " + message)
        self._manager.settle(job_id, outcome, error_code=error_code)
        self._after_terminal(job_id)

    def _after_terminal(self, job_id: str) -> None:
        """任务结束后的轻量清理：成功删 work/、失败/取消删大文件（保留诊断摘要）。"""
        job = self._manager.get(job_id)
        if job is None:
            return
        if self._storage is not None:
            try:
                self._storage.cleanup_after_terminal(job)
            except Exception as exc:  # noqa: BLE001 - 清理失败不得影响任务结果
                logger.warning("任务 %s 结束后清理失败：%s", job_id, exc)
        # 刷新占用字节，供容量治理与前端展示（不含中间目录后应更准确）。
        job.refresh_disk_bytes()

    # -- 辅助 ------------------------------------------------------------
    def _safe_transition(self, job_id: str, new_state: str) -> None:
        try:
            self._manager.set_state(job_id, new_state)
        except (ValueError, KeyError):
            pass

    def _lock_path(self, fp: str) -> Path:
        base = self._runtime_dir or (self._output_root / "runtime")
        return base / ".locks" / f"{fp[:16]}.render.lock"

    def _confirm_url(self) -> str:
        return f"http://127.0.0.1:{CONFIRM_PORT}/"

    def _pipeline_fingerprint(self, job_id: str) -> str | None:
        """任务对应模型文件的**整文件 sha256**（入口脚本/主脚本口径）。

        Web 注册表指纹是「大小 + 前 1MB」的哈希，与管线口径不同；确认清单、
        侧车映射的文件名都用整文件 sha256，混用会永远找不到文件。
        """
        job = self._manager.get(job_id)
        if job is None:
            return None
        try:
            path, _fp, _name = self._registry.resolve(job.model_id)
            return _sha256_file(path)
        except (KeyError, OSError):
            return None

    def _read_confirmation(self, fp: str) -> list[dict[str, Any]] | None:
        base = self._runtime_dir or (self._output_root / "runtime")
        p = base / "confirmation" / f"{fp[:16]}_confirmation.json"
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        items = data.get("items", [])
        return [
            {
                "material": it.get("material"),
                "proposed_class": it.get("proposed_class"),
                "requires_user": bool(it.get("requires_user", False)),
                "confirmed": False,
            }
            for it in items
        ]

    def _read_done_token(self, out_dir: str) -> str | None:
        p = Path(out_dir) / "render.done"
        if not p.is_file():
            return None
        try:
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln:
                    return ln.split()[0]
        except OSError:
            return None
        return None
