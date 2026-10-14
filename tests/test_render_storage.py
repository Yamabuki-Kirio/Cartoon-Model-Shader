"""资源治理测试（规格《资源治理》第 7 节）。

覆盖：
* 连续 100 次任务后数量与容量受限；
* 成功只删中间文件、不删最终产物；
* 失败/取消清理大文件并保留诊断摘要；
* 正在运行与被固定任务不被清理；
* 路径穿越、符号链接、sibling-prefix 无法越界删除；
* 入口子进程退出（进程登记表）；
* 服务异常重启后识别并清理遗留任务；
* 任务历史超限后内存不持续增长；
* 清理失败不影响其他任务，也不虚报释放成功。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.server import render_state
from src.server.app import create_app
from src.server.config import AppConfig, BlenderMCPConfig, RenderConfig, ServerConfig
from src.server.proc_cleanup import ProcessRegistry, terminate_tree
from src.server.render_jobs import RenderJobManager
from src.server.render_storage import (
    RetentionPolicy,
    StorageService,
    UnsafePathError,
    assert_safe_target,
    choose_evictions,
    cleanup_failed_job,
    cleanup_work_dir,
    dir_size,
    purge_job_dir,
    safe_remove,
)
from tests.support import FakeBlenderGateway, authed

FAKE_LAYOUT = Path(__file__).resolve().parent / "fake_render_layout.py"


def _manager_with_jobs(tmp_path: Path, specs):
    """构造一个带若干任务的 manager（直接落到指定位）。"""
    manager = RenderJobManager(tmp_path / "out")
    for spec in specs:
        job, _ = manager.create(
            spec["job_id"], spec.get("model_id", "m"), spec.get("fp", spec["job_id"]),
            "name", {}, str(tmp_path / "out" / spec["job_id"]),
        )
        if spec.get("state", "success") != "queued":
            manager._force_terminal(job.job_id, spec.get("state", "success"))
        job.pinned = spec.get("pinned", False)
        job.disk_bytes = spec.get("disk", 0)
        if spec.get("updated"):
            job.updated_at = spec["updated"]
    manager._by_fingerprint.clear()  # 避免测试间指纹复用干扰
    return manager


# -- 1. 连续 100 次任务后数量与容量受限 -----------------------------------


def test_hundred_jobs_bounded_by_count_and_size(tmp_path: Path) -> None:
    policy = RetentionPolicy(max_jobs=10, max_age_days=3650, max_total_gb=1000)
    manager = RenderJobManager(tmp_path / "out")
    for i in range(100):
        jid = f"j{i:03d}"
        job, _ = manager.create(jid, "ma", f"fp{i}", "n", {}, str(tmp_path / "out" / jid))
        manager._force_terminal(jid, "success")
        job.disk_bytes = 1024
        # 越早的任务更新时间越早（用合法时间戳：按时:分:秒递增）
        job.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(1_700_000_000 + i))
    evict = choose_evictions(manager.list_jobs(), policy)
    # 100 个任务、上限 10 ⇒ 应淘汰 90 个（最旧优先）
    assert len(evict) == 90
    assert "j000" in evict and "j099" not in evict
    # 最旧的必须排在前面
    assert evict[0] == "j000"


def test_size_limit_forces_eviction(tmp_path: Path) -> None:
    policy = RetentionPolicy(max_jobs=1000, max_age_days=3650, max_total_gb=0)  # 容量 0
    manager = _manager_with_jobs(
        tmp_path,
        [{"job_id": f"k{i}", "disk": 10, "updated": f"2026-01-01T00:00:0{i}"} for i in range(5)],
    )
    evict = choose_evictions(manager.list_jobs(), policy)
    assert len(evict) == 5  # 全部超出容量 ⇒ 全清


# -- 2. 成功只删中间文件，不删最终产物 -----------------------------------


def test_success_cleanup_removes_work_keeps_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "out" / "job1"
    (out / "work").mkdir(parents=True)
    (out / "artifacts").mkdir()
    (out / "work" / "intermediate.exr").write_bytes(b"\0" * 4096)
    final = out / "artifacts" / "result.png"
    final.write_bytes(b"png")

    roots = [str(tmp_path / "out")]
    released = cleanup_work_dir(out, roots)
    assert released >= 4096
    assert not (out / "work").exists(), "work/ 应被删除"
    assert final.is_file(), "最终产物必须保留"


# -- 3. 失败/取消清理大文件并保留诊断摘要 --------------------------------


def test_failed_cleanup_removes_big_files_keeps_summary_and_logs(tmp_path: Path) -> None:
    out = tmp_path / "out" / "jobf"
    (out / "work").mkdir(parents=True)
    (out / "work" / "intermediate.exr").write_bytes(b"\0" * 8192)
    (out / "big.blend").write_bytes(b"\0" * 4096)
    (out / "render.done").write_text("FAILED\n", encoding="utf-8")
    (out / "confirm_server.log").write_text("err tail\n", encoding="utf-8")

    roots = [str(tmp_path / "out")]
    released = cleanup_failed_job(out, roots, keep_logs=True)
    assert released >= 8192
    assert not (out / "work").exists()
    assert not (out / "big.blend").exists(), "大文件应被删除"
    assert (out / "render.done").is_file(), "终态标记应保留"
    assert (out / "confirm_server.log").is_file(), "诊断日志应保留"


# -- 4. 正在运行 / 固定任务不被清理 --------------------------------------


def test_running_and_pinned_jobs_not_evicted(tmp_path: Path) -> None:
    policy = RetentionPolicy(max_jobs=0, max_age_days=0, max_total_gb=0)
    manager = _manager_with_jobs(
        tmp_path,
        [
            {"job_id": "active", "state": "queued"},
            {"job_id": "pinned", "state": "success", "pinned": True},
            {"job_id": "keepme", "state": "success", "pinned": True},
            {"job_id": "old", "state": "success"},
        ],
    )
    evict = choose_evictions(manager.list_jobs(), policy)
    assert "active" not in evict, "运行中任务绝不能被清理"
    assert "pinned" not in evict
    assert "keepme" not in evict
    assert "old" in evict


# -- 5. 路径穿越 / 符号链接 / sibling-prefix -----------------------------


def test_safe_delete_rejects_sibling_prefix_and_traversal(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    sibling = tmp_path / "out-evil"
    sibling.mkdir()
    victim = sibling / "secret.txt"
    victim.write_bytes(b"secret")

    roots = [str(out)]
    # sibling 前缀：字符串 startswith 会误放行，is_relative_to 必须拒绝
    with pytest.raises(UnsafePathError):
        safe_remove(victim, roots)
    # .. 穿越
    with pytest.raises(UnsafePathError):
        safe_remove(out / ".." / "out-evil" / "secret.txt", roots)
    assert victim.is_file(), "越界目标绝不能被删除"
    # 允许根内的正常删除
    inside = out / "ok.bin"
    inside.write_bytes(b"x")
    assert safe_remove(inside, roots) == 1


def test_safe_delete_rejects_symlink(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.bin").write_bytes(b"data")
    link = out / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    # **必须确认链接真的建成**：Windows 上某些配置下 ``symlink_to`` 不报错却落成一个
    # 普通空目录，此时后面的断言毫无意义（还会假红）。宁可跳过，不可假绿/假红。
    if not link.is_symlink():
        pytest.skip("符号链接未被真实创建（缺少 Developer Mode 或管理员权限）")

    with pytest.raises(UnsafePathError):
        assert_safe_target(link / "f.bin", [str(out)])
    assert (outside / "f.bin").is_file(), "符号链接目标不得被删除"


# -- 6. 入口子进程退出（进程登记表） -------------------------------------


def test_process_registry_and_terminate(tmp_path: Path) -> None:
    import subprocess
    import sys

    registry = ProcessRegistry()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    registry.register(proc.pid, job_id="jX", exe=sys.executable, kind="entry")
    assert registry.for_job("jX")
    assert registry.to_public()[0]["pid"] == proc.pid

    ok = terminate_tree(proc.pid, proc=proc, grace_seconds=5.0)
    assert ok, "进程树应被终止并回收"
    assert proc.poll() is not None, "入口子进程必须已退出"
    registry.unregister(proc.pid)
    assert registry.for_job("jX") == []


# -- 7. 服务异常重启后识别并清理遗留任务 ---------------------------------


def test_startup_cleans_stale_work(tmp_path: Path) -> None:
    out = tmp_path / "out"
    job_dir = out / "model-x" / "0720-abc"
    (job_dir / "work").mkdir(parents=True)
    (job_dir / "work" / "tmp.exr").write_bytes(b"\0" * 2048)
    (job_dir / "artifacts").mkdir()
    (job_dir / "artifacts" / "final.png").write_bytes(b"png")

    manager = RenderJobManager(out)
    # 用一个已知任务目录模拟「上次异常退出遗留」
    job, _ = manager.create("rj", "model-x", "fpx", "n", {}, str(job_dir))
    storage = StorageService(manager, RetentionPolicy(), [str(out)])
    released = storage.cleanup_stale_work()
    assert released >= 2048
    assert not (job_dir / "work").exists()
    assert (job_dir / "artifacts" / "final.png").is_file()


# -- 8. 任务历史超限后内存不持续增长 -------------------------------------


def test_memory_history_bounded(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out", max_memory_jobs=5)
    for i in range(50):
        jid = f"m{i:03d}"
        manager.create(jid, "ma", f"fp{i}", "n", {}, str(tmp_path / "out" / jid))
        manager._force_terminal(jid, "success")
    assert len(manager.list_jobs()) <= 5, "内存中任务数必须受上限约束"
    # 超限部分应进入历史索引
    assert len(manager.history_index()) > 0


def test_memory_keeps_active_jobs(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out", max_memory_jobs=2)
    manager.create("act1", "ma", "fpa", "n", {}, str(tmp_path / "out" / "act1"))
    manager.create("act2", "mb", "fpb", "n", {}, str(tmp_path / "out" / "act2"))
    manager.create("act3", "mc", "fpc", "n", {}, str(tmp_path / "out" / "act3"))
    # 三个都是活跃任务：不得淘汰任何活跃任务
    ids = {j.job_id for j in manager.list_jobs()}
    assert {"act1", "act2", "act3"} <= ids


# -- 9. 清理失败不影响其他任务，也不虚报 ---------------------------------


def test_cleanup_failure_does_not_abort_or_overcount(tmp_path: Path) -> None:
    out = tmp_path / "out"
    manager = RenderJobManager(out)
    # 一个正常任务 + 一个「目录已被外部删除」的任务（purge 不应报错，也不虚增字节）
    good, _ = manager.create("good", "ma", "fpa", "n", {}, str(out / "good"))
    manager._force_terminal("good", "success")
    good.disk_bytes = 100
    (out / "good").mkdir(parents=True, exist_ok=True)
    (out / "good" / "f.png").write_bytes(b"x")

    ghost, _ = manager.create("ghost", "mb", "fpb", "n", {}, str(out / "ghost"))
    manager._force_terminal("ghost", "failed")
    ghost.disk_bytes = 999999  # 声称很大，但目录根本不存在

    policy = RetentionPolicy(max_jobs=0, max_age_days=0, max_total_gb=0)
    storage = StorageService(manager, policy, [str(out)])
    storage.refresh_sizes()  # ghost 目录不存在 ⇒ disk_bytes 归零
    report = storage.cleanup()
    # 不虚报：ghost 目录不存在，不应把 999999 计入释放量
    assert report.released_bytes < 999999
    assert not report.failed, "目录不存在不应算失败"
    # good 仍被正常清理，未受 ghost 影响
    assert "good" in report.removed_jobs


# -- API 集成 -------------------------------------------------------------


def _config(tmp_path: Path, model_dir: Path) -> AppConfig:
    (model_dir / "m.pmx").write_bytes(b"model")
    return AppConfig(
        blender_mcp=BlenderMCPConfig(host="127.0.0.1", port=1, connect_timeout_seconds=0.01, response_timeout_seconds=0.01),
        server=ServerConfig(host="127.0.0.1", port=8765),
        render=RenderConfig(
            model_root=model_dir,
            runtime_dir=tmp_path / "runtime",
            output_dir=tmp_path / "out",
            pipeline_script=FAKE_LAYOUT,
            max_jobs=30,
        ),
    )


def _render_app(tmp_path: Path, model_dir: Path, *, gateway=None):
    """渲染工作流装配：注入**已连接**的假 MCP。

    规格 §5.4 起，创建渲染任务前会校验 Blender MCP 是否连接；这些用例都在跑
    取消/清理流程，必须先建任务，因此统一注入 FakeBlenderGateway。
    """
    return create_app(_config(tmp_path, model_dir), gateway=gateway or FakeBlenderGateway())


def test_api_storage_and_pin_and_cleanup(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    app = _render_app(tmp_path, model_dir)
    with authed(app) as client:
        storage = client.get("/api/render/storage")
        assert storage.status_code == 200
        assert storage.json()["ok"] is True

        model_id = client.get("/api/render/models").json()["models"][0]["model_id"]
        created = client.post(
            "/api/render/jobs", json={"model_id": model_id, "mode": "faithful", "auto_frame": False}
        )
        job_id = created.json()["job_id"]

        # 等终态
        deadline = time.time() + 20
        while time.time() < deadline:
            body = client.get(f"/api/render/jobs/{job_id}").json()
            if body["is_terminal"]:
                break
            time.sleep(0.1)
        assert body["state"] == "success"
        # 成功任务：work/ 已被清理，artifacts 仍在
        job_dir = Path(app.state.jobs.get(job_id).output_dir)
        assert not (job_dir / "work").exists()
        assert (job_dir / "artifacts" / "result.png").is_file()

        # 固定任务
        pinned = client.post(f"/api/render/jobs/{job_id}/pin", params={"pinned": "true"})
        assert pinned.status_code == 200
        assert pinned.json()["pinned"] is True
        assert pinned.json()["retention_state"] == "pinned"

        # 预演清理：固定任务不在可清理集合
        preview = client.post("/api/render/storage/cleanup", params={"dry_run": "true"})
        assert preview.status_code == 200
        assert job_id not in preview.json()["removed_jobs"]


def test_api_storage_write_requires_token(tmp_path: Path) -> None:
    from tests.support import unauthed

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    app = _render_app(tmp_path, model_dir)
    response = unauthed(app).post("/api/render/storage/cleanup")
    assert response.status_code == 401


# -- 修复专项：跨重启清理 / 关闭保留失败日志 / 取消走进程树 --------------


def test_stale_work_scan_finds_dirs_absent_from_memory(tmp_path: Path) -> None:
    """跨重启：内存为空时，仍要从磁盘发现并清理遗留 work/。"""
    out = tmp_path / "out"
    # 磁盘上遗留两个任务目录（内存里完全没有记录）
    for stamp in ("0720-aaaa", "0720-bbbb"):
        d = out / "model-x" / stamp
        (d / "work").mkdir(parents=True)
        (d / "work" / "tmp.exr").write_bytes(b"\0" * 2048)
        (d / "artifacts").mkdir()
        (d / "artifacts" / "final.png").write_bytes(b"png")

    manager = RenderJobManager(out)  # 空管理器（模拟刚重启）
    assert manager.list_jobs() == []
    storage = StorageService(manager, RetentionPolicy(), [str(out)])
    released = storage.cleanup_stale_work()

    assert released >= 4096, "必须从磁盘发现并清理遗留 work"
    for stamp in ("0720-aaaa", "0720-bbbb"):
        assert not (out / "model-x" / stamp / "work").exists()
        assert (out / "model-x" / stamp / "artifacts" / "final.png").is_file()


def test_shutdown_cleanup_keeps_failed_diagnostics(tmp_path: Path) -> None:
    """关闭时执行 cleanup 不得删除失败诊断日志（即使 fresh、未超阈值）。"""
    out = tmp_path / "out"
    job_dir = out / "model-y" / "0720-fail"
    (job_dir / "work").mkdir(parents=True)
    (job_dir / "confirm_server.log").write_text("error tail\n", encoding="utf-8")
    (job_dir / "render.done").write_text("FAILED\n", encoding="utf-8")

    manager = RenderJobManager(out)
    job, _ = manager.create("rj-fail", "model-y", "fpy", "n", {}, str(job_dir))
    manager._force_terminal("rj-fail", "failed")
    manager._by_fingerprint.clear()

    # 默认策略：数量/期限/容量都未超 ⇒ 失败任务不应被整目录删除
    storage = StorageService(
        manager, RetentionPolicy(max_jobs=30, max_age_days=14, max_total_gb=20), [str(out)]
    )
    report = storage.cleanup(dry_run=False)
    assert "rj-fail" not in report.removed_jobs, "未超阈值的失败任务不得被清理"
    assert (job_dir / "confirm_server.log").is_file(), "失败诊断日志必须保留"
    assert (job_dir / "render.done").is_file()


def test_cancel_terminates_process_tree(tmp_path: Path) -> None:
    """取消接口必须通过进程注册表终止整棵进程树（含登记的「真实 Blender」PID）。

    这条用例**刻意不走** ``POST /api/render/jobs``：那个端点建完任务还会
    ``enqueue``，把任务交给常驻 worker 立刻开跑。于是「任务停在可取消状态」与
    「后台正在把状态往前推」变成一场竞态 —— 后台先跑到终态时，``jobs.cancel()``
    会以「当前状态不可取消」返回 400，用例随机变红。用 sleep 或加长等待只是
    把概率调小，并没有消除竞态。

    取消流程本身不关心渲染有没有真的跑起来，因此这里直接建任务、不入队：
    ``RenderJob`` 的初始状态就是 ``queued``，而 ``queued`` 属于
    ``CANCELABLE_STATES``。全程没有后台任务参与，状态完全由本用例决定。
    """
    import subprocess
    import sys

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    app = _render_app(tmp_path, model_dir)
    with authed(app) as client:
        model_id = client.get("/api/render/models").json()["models"][0]["model_id"]

        # 只建任务，不 enqueue：没有后台 runner，状态不会自己往前走。
        # 这里也不走 POST 端点（它顺带会校验 MCP 连接并 enqueue）——
        # 本用例的被测对象是**取消端点**，建任务只是夹具。
        job, created = app.state.pipeline.create_job(
            model_id, {"mode": "faithful", "auto_frame": False}
        )
        assert created is True, "本用例要求新建任务，而不是复用已有活跃任务"
        job_id = job.job_id
        assert job.state == "queued"
        assert render_state.can_cancel(job.state), "用例前提：queued 必须可取消"

        # 造出中间文件，供「取消后清理」断言使用。
        work_dir = Path(job.output_dir) / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "tmp.exr").write_bytes(b"\0" * 4096)
        assert work_dir.is_dir(), "前置条件：work/ 必须真的存在，否则清理断言会空转"

        # 挂一个「假 Blender」子进程，验证取消会终止它。
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            # register() 是同步的：返回即表示已写入注册表，所以下面这条断言就是
            # 「进程已登记」的同步点 —— 不需要 sleep，也不需要等事件。
            app.state.proc_registry.register(
                child.pid, job_id=job_id, exe=sys.executable, kind="blender"
            )
            registered = {rec.pid for rec in app.state.proc_registry.for_job(job_id)}
            assert child.pid in registered, "取消前必须确认子进程已登记在注册表中"

            response = client.post(f"/api/render/jobs/{job_id}/cancel")
            assert response.status_code == 200
            assert response.json()["state"] == "cancelled"

            # ① 任务状态：已取消
            assert app.state.jobs.get(job_id).state == "cancelled"
            # ② 进程树：登记的子进程已退出，且注册表已摘除记录
            assert child.poll() is not None, "取消应终止整棵进程树"
            assert app.state.proc_registry.for_job(job_id) == [], "已退出的进程必须摘除记录"
            # ③ 临时文件：work/ 中间文件被清理
            assert not work_dir.exists(), "取消后应清理 work/ 中间文件"
        finally:
            # 断言中途失败时别把子进程漏在后台。
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


def test_pid_contract_registers_real_blender(tmp_path: Path, monkeypatch) -> None:
    """真实固定脚本契约：入口脚本 stdout 上报的 @@BLENDER_PID@@ 必须被登记。

    走完整适配器路径（真的起子进程 + 真的解析 stdout），不手工调用 _observe_line。
    """
    import asyncio

    from src.server.models_scan import ModelRegistry
    from src.server.render_pipeline_adapter import RenderPipelineService
    from src.server.proc_cleanup import ProcessRegistry, _pid_alive

    # 让替身挂住，使「取消」发生在渲染途中。
    monkeypatch.setenv("TOON_FAKE_HOLD", "30")

    contract = Path(__file__).resolve().parent / "fake_render_pid_contract.py"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")

    registry = ModelRegistry(model_dir, tmp_path / ".reg.json")
    registry.scan()
    manager = RenderJobManager(tmp_path / "out")
    proc_registry = ProcessRegistry()
    service = RenderPipelineService(
        manager,
        registry,
        fixed_render_script=contract,
        model_root=model_dir,
        runtime_dir=tmp_path / "runtime",
        output_root=tmp_path / "out",
        proc_registry=proc_registry,
    )
    model_id = registry.to_public()[0]["model_id"]
    job, _ = service.create_job(model_id, {"mode": "faithful"})

    async def scenario():
        await service.start_worker()
        service.enqueue(job.job_id)
        deadline = time.monotonic() + 15
        bloben = None
        while time.monotonic() < deadline:
            recs = [r for r in proc_registry.for_job(job.job_id) if r.kind == "blender"]
            if recs:
                bloben = recs[0]
                break
            await asyncio.sleep(0.05)
        assert bloben is not None, "入口脚本上报的 Blender PID 必须被登记为 blender 类型"
        assert _pid_alive(bloben.pid), "登记的真实 Blender 进程应在运行"
        # 取消：必须终止整棵树（含登记的 Blender）
        await service.cancel_job(job.job_id)
        await asyncio.sleep(0.3)
        assert not _pid_alive(bloben.pid), "取消应终止登记的真实 Blender 进程"
        await service.stop_worker()

    asyncio.run(scenario())


def test_pid_contract_null_pid_records_warning(tmp_path: Path) -> None:
    """取不到 PID 时（pid=null）必须记录「不保证已终止」告警，不得假装成功。"""
    import asyncio

    from src.server.models_scan import ModelRegistry
    from src.server.render_pipeline_adapter import RenderPipelineService

    contract = Path(__file__).resolve().parent / "fake_render_pid_contract.py"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")
    registry = ModelRegistry(model_dir, tmp_path / ".reg.json")
    registry.scan()
    manager = RenderJobManager(tmp_path / "out")
    service = RenderPipelineService(
        manager,
        registry,
        fixed_render_script=contract,
        model_root=model_dir,
        runtime_dir=tmp_path / "runtime",
        output_root=tmp_path / "out",
    )
    model_id = registry.to_public()[0]["model_id"]
    job, _ = service.create_job(model_id, {"mode": "faithful", "auto_frame": False})

    # 直接以 --fail-pid 调脚本本体，验证契约输出
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, str(contract), "--out", str(tmp_path / "o"), "--fail-pid", "1"],
        capture_output=True,
        text=True,
    )
    assert "@@BLENDER_PID@@{\"pid\": null}" in out.stdout

    # 适配器侧：解析 null 时应写入告警日志
    service._observe_line(job.job_id, '@@BLENDER_PID@@{"pid": null}', job.fingerprint)
    logs = "\n".join(manager.get(job.job_id).logs)
    assert "未能取得其 PID" in logs


def test_job_object_assign_failure_recorded(tmp_path: Path) -> None:
    """assign 失败时必须记录 assigned=False（不得表现为治理成功）。"""
    from src.server.proc_cleanup import ProcessRegistry

    class FakeJobObject:
        available = True

        def assign(self, pid: int) -> bool:
            return False  # 模拟绑定失败（已属其他 Job / 权限不足）

    registry = ProcessRegistry()
    registry.attach_job_object("j1", FakeJobObject())
    rec = registry.register(1234, job_id="j1", exe="blender", kind="blender")
    assert rec.assigned is False, "assign 失败必须记录为 False"


def test_evictions_prioritize_but_respect_thresholds(tmp_path: Path) -> None:
    """取消/失败只提高优先级；未超阈值时不得把它们无条件淘汰。"""
    manager = _manager_with_jobs(
        tmp_path,
        [
            {"job_id": "c1", "state": "cancelled"},
            {"job_id": "f1", "state": "failed"},
            {"job_id": "s1", "state": "success"},
        ],
    )
    # 阈值都未超 ⇒ 什么都别清
    loose = RetentionPolicy(max_jobs=30, max_age_days=14, max_total_gb=20)
    assert choose_evictions(manager.list_jobs(), loose) == []

    # 数量超限 ⇒ 取消/失败优先，但必须受「超数量」这一阈值约束
    tight = RetentionPolicy(max_jobs=1, max_age_days=3650, max_total_gb=1000)
    evict = choose_evictions(manager.list_jobs(), tight)
    assert evict == ["c1", "f1"], f"取消/失败应优先且顺序正确，实际 {evict}"


# -- P1：PID 复用安全（终态清除记录） -------------------------------------


def test_registry_unregister_job_clears_all_pids(tmp_path: Path) -> None:
    """unregister_job 必须清除该任务全部 PID（入口 + Blender + 确认服务）。"""
    from src.server.proc_cleanup import ProcessRegistry

    registry = ProcessRegistry()
    registry.register(1001, job_id="j1", exe="python", kind="entry")
    registry.register(1002, job_id="j1", exe="blender", kind="blender")
    registry.register(1003, job_id="j1", exe="python", kind="confirm")
    registry.register(2001, job_id="j2", exe="python", kind="entry")

    removed = registry.unregister_job("j1")
    assert set(removed) == {1001, 1002, 1003}
    assert registry.for_job("j1") == []
    # 其他任务不受影响
    assert [r.pid for r in registry.for_job("j2")] == [2001]


def test_completed_job_leaves_no_stale_pids(tmp_path: Path) -> None:
    """任务成功跑完后，注册表不得残留该任务的任何 PID（防止重试时误杀）。"""
    from src.server.models_scan import ModelRegistry
    from src.server.render_pipeline_adapter import RenderPipelineService
    from src.server.proc_cleanup import ProcessRegistry

    contract = Path(__file__).resolve().parent / "fake_render_pid_contract.py"
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")
    registry = ModelRegistry(model_dir, tmp_path / ".reg.json")
    registry.scan()
    manager = RenderJobManager(tmp_path / "out")
    proc_registry = ProcessRegistry()
    service = RenderPipelineService(
        manager,
        registry,
        fixed_render_script=contract,
        model_root=model_dir,
        runtime_dir=tmp_path / "runtime",
        output_root=tmp_path / "out",
        proc_registry=proc_registry,
    )
    model_id = registry.to_public()[0]["model_id"]
    job, _ = service.create_job(model_id, {"mode": "faithful"})

    import asyncio

    async def scenario():
        await service.start_worker()
        service.enqueue(job.job_id)
        # 等任务终态
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not manager.get(job.job_id).is_terminal:
            await asyncio.sleep(0.05)
        # 终态在 _finalize 里到达，而 PID 清理由 start() 的 finally 执行 —— 轮询等它收尾。
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and proc_registry.for_job(job.job_id):
            await asyncio.sleep(0.05)
        assert proc_registry.for_job(job.job_id) == [], "完成后不得残留 PID 记录"
        await service.stop_worker()

    asyncio.run(scenario())


def test_cancel_after_stale_pid_reuse_does_not_kill_completed(tmp_path: Path) -> None:
    """模拟旧 PID 被系统复用：已完成任务遗留的记录不得再被取消流程处理。"""
    import subprocess
    import sys

    from src.server.proc_cleanup import ProcessRegistry, terminate_job_processes

    registry = ProcessRegistry()
    # 任务 A 已完成并清空记录
    registry.register(1111, job_id="done-job", exe="blender", kind="blender")
    registry.unregister_job("done-job")

    # 起一个**无关**进程，模拟「系统把 1111 复用给了别的进程」
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        # done-job 已无记录 ⇒ 取消它不得触碰任何进程
        unresolved = terminate_job_processes(registry, "done-job", grace_seconds=0.5)
        assert unresolved == []
        assert bystander.poll() is None, "已完成任务的取消不得误杀无关进程"
    finally:
        bystander.terminate()


def test_cancel_active_pid_still_terminates_and_clears(tmp_path: Path) -> None:
    """取消中的活动 PID 仍能被终止并清除记录。"""
    import subprocess
    import sys

    from src.server.proc_cleanup import ProcessRegistry, terminate_job_processes

    registry = ProcessRegistry()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    registry.register(child.pid, job_id="live-job", exe="blender", kind="blender")

    unresolved = terminate_job_processes(registry, "live-job", grace_seconds=5.0)
    assert unresolved == []
    assert child.poll() is not None, "活动进程必须被终止"
    assert registry.for_job("live-job") == [], "终止后记录必须被清除"


# -- P1：取消顺序（先落态，再终止） ---------------------------------------


def test_cancel_illegal_state_does_not_terminate(tmp_path: Path) -> None:
    """不可取消（rendering）状态下请求取消：不得终止任何进程，任务保持原状态。"""
    import subprocess
    import sys

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    app = _render_app(tmp_path, model_dir)
    # 不启动 worker 执行：让任务停在测试指定的状态，避免与后台执行竞态。
    app.state.pipeline.enqueue = lambda job_id: None  # type: ignore[assignment]
    with authed(app) as client:
        model_id = client.get("/api/render/models").json()["models"][0]["model_id"]
        created = client.post(
            "/api/render/jobs", json={"model_id": model_id, "mode": "faithful", "auto_frame": False}
        )
        job_id = created.json()["job_id"]

        # 把任务推进到 rendering（合法链：analyzing_materials → preparing → rendering）
        app.state.jobs.set_state(job_id, "preflight")
        app.state.jobs.set_state(job_id, "analyzing_materials")
        app.state.jobs.set_state(job_id, "preparing")
        app.state.jobs.set_state(job_id, "rendering")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        app.state.proc_registry.register(child.pid, job_id=job_id, exe=sys.executable, kind="blender")
        try:
            response = client.post(f"/api/render/jobs/{job_id}/cancel")
            # 不可取消 ⇒ 拒绝（绝不能先杀进程再报错）
            assert response.status_code in (400, 409), response.text
            assert app.state.jobs.get(job_id).state == "rendering"
            assert child.poll() is None, "不可取消状态下不得终止进程"
        finally:
            child.terminate()


def test_cancel_legal_state_transitions_first_then_terminates(tmp_path: Path) -> None:
    """合法状态取消：必须**先进入 cancelled**，再终止进程树。"""
    import subprocess
    import sys

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    app = _render_app(tmp_path, model_dir)

    order: list[str] = []
    real_cancel_job = app.state.pipeline.cancel_job

    async def spy_cancel_job(job_id: str):
        # 记录「终止进程树」发生时，任务是否**已经**是 cancelled。
        order.append("terminate:" + app.state.jobs.get(job_id).state)
        return await real_cancel_job(job_id)

    app.state.pipeline.cancel_job = spy_cancel_job
    app.state.pipeline.enqueue = lambda job_id: None  # type: ignore[assignment]

    with authed(app) as client:
        model_id = client.get("/api/render/models").json()["models"][0]["model_id"]
        created = client.post(
            "/api/render/jobs", json={"model_id": model_id, "mode": "faithful", "auto_frame": False}
        )
        job_id = created.json()["job_id"]
        # queued 是可取消状态（已禁用 enqueue，任务保持 queued）
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        app.state.proc_registry.register(child.pid, job_id=job_id, exe=sys.executable, kind="blender")

        response = client.post(f"/api/render/jobs/{job_id}/cancel")
        assert response.status_code == 200
        assert response.json()["state"] == "cancelled"
        # 终止时任务必须**已经**是 cancelled（先落态，后终止）
        assert order == ["terminate:cancelled"], f"顺序错误：{order}"
        assert child.poll() is not None, "合法取消应终止进程树"

    assert app.state.proc_registry.for_job(job_id) == []


def test_cancel_queued_runs_cleanup(tmp_path: Path) -> None:
    """排队任务取消后不会走 _finalize，接口必须主动做任务级清理。"""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    app = _render_app(tmp_path, model_dir)
    app.state.pipeline.enqueue = lambda job_id: None  # type: ignore[assignment]
    with authed(app) as client:
        model_id = client.get("/api/render/models").json()["models"][0]["model_id"]
        created = client.post(
            "/api/render/jobs", json={"model_id": model_id, "mode": "faithful", "auto_frame": False}
        )
        job_id = created.json()["job_id"]
        # 造一个残留 work 目录，模拟排队/进行中残留
        job = app.state.jobs.get(job_id)
        work = Path(job.output_dir) / "work"
        work.mkdir(parents=True, exist_ok=True)
        (work / "tmp.exr").write_bytes(b"\0" * 1024)

        response = client.post(f"/api/render/jobs/{job_id}/cancel")
        assert response.status_code == 200
        assert not work.exists(), "取消后必须清理残留 work 目录"
