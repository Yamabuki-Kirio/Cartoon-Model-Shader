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
from tests.support import authed

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


def test_api_storage_and_pin_and_cleanup(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    app = create_app(_config(tmp_path, model_dir))
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
    app = create_app(_config(tmp_path, model_dir))
    response = unauthed(app).post("/api/render/storage/cleanup")
    assert response.status_code == 401
