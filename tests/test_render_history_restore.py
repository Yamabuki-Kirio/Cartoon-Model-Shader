# -*- coding: utf-8 -*-
"""任务历史跨重启恢复（PR #6 任务5）。

服务重启后从摘要索引重建历史任务。四条硬约束：
  1. 已完成任务继续展示、**可继续下载产物**；
  2. 上次卡在非终态的任务标记为 ``interrupted``（绝不留在「渲染中」假装活着，
     也绝不写成成功）；
  3. 不恢复旧线程或旧子进程，且不因 PID 相同去杀任何进程；
  4. 历史数量受 ``max_memory_jobs`` 限制。

索引是磁盘上的明文文件，因此它**绝不写绝对路径**（只存相对 output_root 的
相对路径），恢复时还要复核拼回的路径仍在输出根目录内。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from server import render_state  # noqa: E402
from server.render_jobs import RenderJobManager  # noqa: E402


def _mk_manager(root: Path, *, max_jobs: int = 200) -> RenderJobManager:
    return RenderJobManager(root, max_memory_jobs=max_jobs,
                            history_index_path=root / ".jobs_index.json")


def _make_job(mgr: RenderJobManager, *, state: str, out_rel: str = "job-1",
              shippable: bool = False, outcome: str | None = None,
              pinned: bool = False) -> str:
    out = mgr.output_root / out_rel
    out.mkdir(parents=True, exist_ok=True)
    job, _created = mgr.create(
        "job-" + out_rel.replace("/", "-"), "model-1", "f" * 64,
        model_name="91", settings={"mode": "enhanced", "material_policy": "strict"},
        output_dir=str(out),
    )
    job.state = state
    job.outcome = outcome
    job.shippable = shippable
    job.pinned = pinned
    mgr._append_history_index(job)
    return job.job_id


# ---------------------------------------------------------------- 状态词表
def test_interrupted_is_terminal_but_not_shippable() -> None:
    assert "interrupted" in render_state.ALL_STATES
    assert render_state.is_terminal("interrupted") is True
    # 绝不进可交付白名单
    assert "interrupted" not in render_state.SHIPPABLE_STATES
    # 中断可以重试（产物目录可能只写了一半）
    assert "interrupted" in render_state.RETRYABLE_STATES
    assert render_state.stage_info("interrupted")[1] == "已中断"


# ---------------------------------------------------------------- 恢复
def test_completed_job_is_restored_and_still_downloadable(tmp_path: Path) -> None:
    """已完成任务恢复后仍能列举并下载产物。"""
    mgr = _mk_manager(tmp_path)
    job_id = _make_job(mgr, state="success", outcome="success", shippable=True)
    out = mgr.output_root / "job-1"
    (out / "一键渲染.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    (out / "run_manifest.json").write_text("{}", encoding="utf-8")

    fresh = _mk_manager(tmp_path)          # 模拟重启：新 manager，同一索引文件
    assert fresh.restore_history() == 1
    job = fresh.get(job_id)
    assert job is not None
    assert job.state == "success"
    assert job.outcome == "success"
    assert job.shippable is True
    assert job.restored_from_history is True
    # 关键：产物仍可列举（可继续下载）
    names = {a["name"] for a in job.list_artifacts()}
    assert "一键渲染.png" in names
    assert "run_manifest.json" in names
    img = next(a for a in job.list_artifacts() if a["name"] == "一键渲染.png")
    assert img["shippable"] is True


def test_non_terminal_job_becomes_interrupted(tmp_path: Path) -> None:
    """上次卡在渲染中的任务必须落成 interrupted，绝不留在「渲染中」假装活着。"""
    mgr = _mk_manager(tmp_path)
    job_id = _make_job(mgr, state="rendering", out_rel="job-render")
    fresh = _mk_manager(tmp_path)
    assert fresh.restore_history() == 1
    job = fresh.get(job_id)
    assert job is not None
    assert job.state == "interrupted"
    assert job.is_terminal is True
    # 中断绝不可交付，且不是 run_contract 正式终态
    assert job.shippable is False
    assert job.outcome is None


def test_no_thread_or_process_is_resurrected(tmp_path: Path) -> None:
    """恢复只重建内存记录：不 attach 任何进程句柄，也不重连轮询。"""
    mgr = _mk_manager(tmp_path)
    _make_job(mgr, state="rendering", out_rel="job-render")
    fresh = _mk_manager(tmp_path)
    fresh.restore_history()
    job = next(iter(fresh.list_jobs()))
    assert job._proc is None, "恢复的任务不得持有进程句柄"
    # 活动任务表为空：没有「正在跑」的任务
    assert fresh._active == {}


def test_history_is_capped_by_max_memory_jobs(tmp_path: Path) -> None:
    """历史数量受 max_memory_jobs 限制。

    写入时索引本身就截尾（``_append_history_index`` 按 ``max_memory_jobs`` 保留最近
    若干条），恢复时再截一次 —— 两道闸都在，所以索引里最多也就这么多条。
    """
    root = tmp_path
    mgr = _mk_manager(root, max_jobs=3)
    for i in range(6):
        _make_job(mgr, state="success", outcome="success", out_rel=f"job-{i}")

    idx = json.loads((root / ".jobs_index.json").read_text(encoding="utf-8"))
    assert len(idx) == 3, "索引写入时就必须截尾，不能无界增长"

    fresh = _mk_manager(root, max_jobs=3)
    assert fresh.restore_history() == 3
    # 保留的是最新的三条
    assert {j.job_id for j in fresh.list_jobs()} == {
        r["job_id"] for r in idx}


def test_oversized_index_is_truncated_on_restore(tmp_path: Path) -> None:
    """索引文件本身超长时（大 max_jobs 写入、小 max_jobs 恢复），恢复仍要截尾。"""
    root = tmp_path
    big = _mk_manager(root, max_jobs=50)
    ids = [_make_job(big, state="success", outcome="success",
                     out_rel=f"job-{i}") for i in range(8)]
    # 索引里有 8 条，但本次会话只允许装 3 条
    fresh = _mk_manager(root, max_jobs=3)
    assert fresh.restore_history() == 3
    assert {j.job_id for j in fresh.list_jobs()} == set(ids[-3:])


def test_index_never_contains_absolute_paths(tmp_path: Path) -> None:
    """索引是明文文件，绝不能夹带本机绝对路径。"""
    mgr = _mk_manager(tmp_path)
    _make_job(mgr, state="success", outcome="success")
    raw = (tmp_path / ".jobs_index.json").read_text(encoding="utf-8")
    assert "output_rel" in raw
    assert str(tmp_path) not in raw, raw[:300]
    # 也不能含 Windows 盘符形态的绝对路径
    assert ":\\" not in raw and ":/" not in raw.replace("https://", "")


def test_traversal_in_index_is_rejected(tmp_path: Path) -> None:
    """索引被篡改成 ../ 或绝对路径时必须拒绝，不得越界到任意目录。"""
    mgr = _mk_manager(tmp_path)
    _make_job(mgr, state="success", outcome="success")
    idx = tmp_path / ".jobs_index.json"
    data = json.loads(idx.read_text(encoding="utf-8"))
    data[0]["output_rel"] = "../../../Windows/System32"
    idx.write_text(json.dumps(data), encoding="utf-8")

    secret = tmp_path.parent / "secret_out"
    secret.mkdir(exist_ok=True)
    (secret / "x.png").write_bytes(b"PNG")

    fresh = _mk_manager(tmp_path)
    fresh.restore_history()
    job = next(iter(fresh.list_jobs()))
    # 记录仍在（用户能看到它曾存在），但目录不可用 → 没有产物、不可交付
    assert job.output_dir == ""
    assert job.list_artifacts() == []
    assert job.shippable is False


def test_absolute_path_in_index_is_rejected(tmp_path: Path) -> None:
    mgr = _mk_manager(tmp_path)
    _make_job(mgr, state="success", outcome="success")
    idx = tmp_path / ".jobs_index.json"
    data = json.loads(idx.read_text(encoding="utf-8"))
    data[0]["output_rel"] = "C:/Windows/System32"
    idx.write_text(json.dumps(data), encoding="utf-8")

    fresh = _mk_manager(tmp_path)
    fresh.restore_history()
    job = next(iter(fresh.list_jobs()))
    assert job.output_dir == ""
    assert job.shippable is False


def test_shippable_flag_is_revalidated_against_state(tmp_path: Path) -> None:
    """索引里写着 shippable 但状态不是 success 时，恢复必须否掉它。"""
    mgr = _mk_manager(tmp_path)
    _make_job(mgr, state="diagnostic", outcome="diagnostic", out_rel="job-diag")
    idx = tmp_path / ".jobs_index.json"
    data = json.loads(idx.read_text(encoding="utf-8"))
    data[0]["shippable"] = True          # 模拟索引被改坏 / 旧版本写入
    idx.write_text(json.dumps(data), encoding="utf-8")

    fresh = _mk_manager(tmp_path)
    fresh.restore_history()
    job = next(iter(fresh.list_jobs()))
    assert job.state == "diagnostic"
    assert job.shippable is False, "诊断态绝不能因为索引写了 true 就变成可交付"
    assert all(not a["shippable"] for a in job.list_artifacts())


def test_pinned_survives_restart(tmp_path: Path) -> None:
    """固定状态必须跨重启保留（否则自动清理会删掉用户明确要留的任务）。"""
    mgr = _mk_manager(tmp_path)
    _make_job(mgr, state="success", outcome="success", out_rel="job-pin", pinned=True)
    fresh = _mk_manager(tmp_path)
    fresh.restore_history()
    job = next(iter(fresh.list_jobs()))
    assert job.pinned is True
    assert job.retention_state == "pinned"


def test_unknown_state_entry_is_skipped(tmp_path: Path) -> None:
    """索引里的未知状态不得进入内存（否则前端会拿到不认识的 state）。"""
    mgr = _mk_manager(tmp_path)
    _make_job(mgr, state="success", outcome="success")
    idx = tmp_path / ".jobs_index.json"
    data = json.loads(idx.read_text(encoding="utf-8"))
    data.append({"job_id": "job-bogus", "state": "not_a_state",
                 "output_rel": "job-1", "model_id": "m"})
    idx.write_text(json.dumps(data), encoding="utf-8")

    fresh = _mk_manager(tmp_path)
    fresh.restore_history()
    assert fresh.get("job-bogus") is None


def test_corrupt_index_does_not_block_startup(tmp_path: Path) -> None:
    mgr = _mk_manager(tmp_path)
    _make_job(mgr, state="success", outcome="success")
    (tmp_path / ".jobs_index.json").write_text("{ broken", encoding="utf-8")
    fresh = _mk_manager(tmp_path)
    assert fresh.restore_history() == 0


def test_restore_is_idempotent(tmp_path: Path) -> None:
    """重复调用不产生重复记录。"""
    mgr = _mk_manager(tmp_path)
    _make_job(mgr, state="success", outcome="success")
    fresh = _mk_manager(tmp_path)
    assert fresh.restore_history() == 1
    assert fresh.restore_history() == 0
    assert len(fresh.list_jobs()) == 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))