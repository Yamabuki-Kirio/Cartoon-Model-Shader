"""运行终态契约测试（PR #6 规格 §3 / P0）。

守的是**四条互相咬合的硬约束**：

1. 四种正式终态必须分别映射为 ``success`` / ``rejected`` / ``diagnostic`` /
   ``failed``，不能再被压成同一个 ``failed``；
2. 只有 ``success`` 可 ``shippable=true``，且调用方无法用参数把它抬真；
3. 未知终态 token 一律失败，并单独标 ``RENDER_UNKNOWN_TERMINAL``；
4. **不得以文件存在或退出码 0 单独判断成功** —— token 与事实冲突时同样判失败。

每个 case 都用一个专门的 fake 固定脚本（``tests/fake_render_*.py``）驱动，
走的是真实子进程路径，不是直接调 ``_finalize``：要连退出码一起验。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.server import errors, render_contract, render_state
from src.server.models_scan import ModelRegistry
from src.server.render_jobs import RenderJobManager
from src.server.render_pipeline_adapter import RenderPipelineService

HERE = Path(__file__).resolve().parent


def _fake(name: str) -> Path:
    return HERE / name


def _make_service(tmp_path: Path, script: Path):
    model_dir = tmp_path / "models"
    model_dir.mkdir(exist_ok=True)
    (model_dir / "m.pmx").write_bytes(b"model")
    registry = ModelRegistry(model_dir, tmp_path / ".reg.json")
    registry.scan()
    manager = RenderJobManager(tmp_path / "out")
    service = RenderPipelineService(
        manager,
        registry,
        fixed_render_script=script,
        model_root=model_dir,
        runtime_dir=tmp_path / "runtime",
        output_root=tmp_path / "out",
    )
    return manager, service, registry


def _run(tmp_path: Path, script_name: str):
    """跑一个 fake 固定脚本，返回终态任务。"""
    manager, service, registry = _make_service(tmp_path, _fake(script_name))
    model_id = registry.to_public()[0]["model_id"]
    job, created = service.create_job(model_id, {"mode": "faithful", "auto_frame": False})
    assert created is True
    asyncio.run(service.start(job.job_id))
    final = manager.get(job.job_id)
    assert final is not None
    return final


# -- 契约桥接：词表必须来自 run_contract，不在本地复制 ----------------------


def test_contract_bridge_loads_real_run_contract() -> None:
    assert render_contract.contract_path().is_file()
    assert set(render_contract.outcome_set()) == {"success", "rejected", "diagnostic", "failed"}
    assert render_contract.shippable_outcomes() == ("success",)
    assert render_contract.valid_tokens() == frozenset(
        {"SUCCESS", "REJECTED", "DIAGNOSTIC", "FAILED"}
    )


def test_contract_normalize_unknown_is_failed() -> None:
    assert render_contract.normalize("SUCCESS") == "success"
    assert render_contract.normalize("REJECTED") == "rejected"
    assert render_contract.normalize("DIAGNOSTIC") == "diagnostic"
    assert render_contract.normalize("FAILED") == "failed"
    # 未知 / 空 / None 一律 failed，绝不返回 None 或原值
    assert render_contract.normalize("WEIRD") == "failed"
    assert render_contract.normalize("") == "failed"
    assert render_contract.normalize(None) == "failed"
    assert render_contract.normalize("cancelled") == "failed"


def test_contract_only_success_is_shippable() -> None:
    assert render_contract.is_shippable("success") is True
    for other in ("rejected", "diagnostic", "failed", "cancelled", "WEIRD"):
        assert render_contract.is_shippable(other) is False, other


def test_contract_is_known_token_distinguishes_unknown() -> None:
    assert render_contract.is_known_token("success") is True
    assert render_contract.is_known_token("REJECTED") is True
    assert render_contract.is_known_token("DIAGNOSTIC") is True
    assert render_contract.is_known_token("FAILED") is True
    assert render_contract.is_known_token("OK") is False
    assert render_contract.is_known_token("SUCCESS_WITH_WARNINGS") is False


# -- render_state：可交付白名单与不可取消阶段 ------------------------------


def test_state_shippable_only_success() -> None:
    assert render_state.is_shippable("success")
    for other in ("failed", "rejected", "diagnostic", "cancelled", "rendering"):
        assert not render_state.is_shippable(other), other


def test_formal_render_phase_is_not_cancelable() -> None:
    """规格 §7.3：preparing / rendering / verifying 一律不可取消。"""
    for state in ("preparing", "rendering", "verifying"):
        assert not render_state.can_cancel(state), state
        # 迁移表上也不留 cancelled：绕过 can_cancel 也改不进去。
        assert not render_state.can_transition(state, "cancelled"), state
    for state in ("queued", "analyzing_materials", "waiting_confirmation"):
        assert render_state.can_cancel(state), state


# -- 四种正式终态映射 ------------------------------------------------------


def test_success_maps_to_success_and_shippable(tmp_path: Path) -> None:
    final = _run(tmp_path, "fake_render_ok.py")
    assert final.state == "success"
    assert final.outcome == "success"
    assert final.shippable is True
    assert final.error_code is None
    assert final.degraded is False


def test_rejected_maps_to_rejected_not_failed(tmp_path: Path) -> None:
    final = _run(tmp_path, "fake_render_rejected.py")
    assert final.state == "rejected"
    assert final.outcome == "rejected"
    assert final.shippable is False
    assert final.error_code == errors.RENDER_REJECTED


def test_diagnostic_maps_to_diagnostic_and_not_shippable(tmp_path: Path) -> None:
    final = _run(tmp_path, "fake_render_diagnostic.py")
    assert final.state == "diagnostic"
    assert final.outcome == "diagnostic"
    assert final.shippable is False
    assert final.error_code == errors.RENDER_DIAGNOSTIC
    # 有图也绝不交付：诊断图不是成品
    assert final.list_artifacts(), "诊断图应被列出来供查看"
    assert final.shippable is False


def test_failed_token_maps_to_failed(tmp_path: Path) -> None:
    final = _run(tmp_path, "fake_render_failed_token.py")
    assert final.state == "failed"
    assert final.outcome == "failed"
    assert final.shippable is False
    assert final.error_code == errors.RENDER_SUBPROCESS_FAILED


# -- token 与事实冲突 ------------------------------------------------------


def test_success_token_with_nonzero_exit_is_failed(tmp_path: Path) -> None:
    final = _run(tmp_path, "fake_render_success_nonzero.py")
    assert final.state == "failed"
    assert final.shippable is False
    assert final.error_code == errors.RENDER_SUBPROCESS_FAILED


def test_success_token_without_image_is_failed(tmp_path: Path) -> None:
    final = _run(tmp_path, "fake_render_success_no_image.py")
    assert final.state == "failed"
    assert final.shippable is False
    assert final.error_code == errors.RENDER_OUTPUT_MISSING


# -- 未知 token -----------------------------------------------------------


def test_unknown_token_is_failed_with_own_code(tmp_path: Path) -> None:
    final = _run(tmp_path, "fake_render_unknown_token.py")
    assert final.state == "failed"
    assert final.outcome == "failed"
    assert final.shippable is False
    # 必须单独标注：契约升级时要被发现，不能混进普通失败
    assert final.error_code == errors.RENDER_UNKNOWN_TERMINAL
    assert any("无法识别" in ln for ln in final.logs)


# -- 兼容降级 -------------------------------------------------------------


def test_missing_token_with_exit_zero_and_image_is_degraded_success(tmp_path: Path) -> None:
    final = _run(tmp_path, "fake_render_ok_no_token.py")
    assert final.state == "success"
    assert final.shippable is True
    # 必须如实披露这是降级判定
    assert final.degraded is True


def test_missing_token_without_image_is_output_missing(tmp_path: Path) -> None:
    final = _run(tmp_path, "fake_render_ok_no_image.py")
    assert final.state == "failed"
    assert final.shippable is False
    assert final.error_code == errors.RENDER_OUTPUT_MISSING


# -- manager.settle：无法把可交付性抬真 ------------------------------------


def test_settle_cannot_force_shippable(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    manager.create("j1", "m", "fp", "m", {}, str(tmp_path / "o"))
    manager.set_state("j1", "preflight")

    settled = manager.settle("j1", "success")
    assert settled.shippable is True

    manager.create("j2", "m", "fp2", "m", {}, str(tmp_path / "o2"))
    manager.set_state("j2", "preflight")
    # 就算调用方「想」让它可交付，契约也只认 success
    settled2 = manager.settle("j2", "REJECTED")
    assert settled2.state == "rejected"
    assert settled2.shippable is False
    assert settled2.outcome == "rejected"


def test_settle_unknown_token_falls_back_to_failed(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    manager.create("j1", "m", "fp", "m", {}, str(tmp_path / "o"))
    settled = manager.settle("j1", "TOTALLY_NEW_TOKEN")
    assert settled.state == "failed"
    assert settled.outcome == "failed"
    assert settled.shippable is False


def test_retry_clears_terminal_fields(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    manager.create("j1", "m", "fp", "m", {}, str(tmp_path / "o"))
    manager.set_state("j1", "preflight")
    manager.settle("j1", "diagnostic", error_code=errors.RENDER_DIAGNOSTIC)
    assert manager.get("j1").shippable is False

    retried = manager.retry("j1")
    assert retried.state == "queued"
    # 终态字段必须清空：否则重跑未完成时就已被标成可交付
    assert retried.outcome is None
    assert retried.shippable is False
    assert retried.error_code is None
    assert retried.degraded is False


def test_cancel_is_not_a_formal_outcome(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    manager.create("j1", "m", "fp", "m", {}, str(tmp_path / "o"))
    # 新建任务处于 queued，属于可取消窗口
    cancelled = manager.cancel("j1", reason="用户取消")
    assert cancelled.state == "cancelled"
    # cancelled 不是 run_contract 终态：outcome 必须保持 None，
    # 否则前端会显示四种正式终态之一的文案。
    assert cancelled.outcome is None
    assert cancelled.shippable is False


def test_cancel_rejected_during_formal_render(tmp_path: Path) -> None:
    """规格 §7.3：正式渲染阶段取消必须被拒绝，且不得留下「已取消」假象。"""
    manager = RenderJobManager(tmp_path / "out")
    manager.create("j1", "m", "fp", "m", {}, str(tmp_path / "o"))
    manager.set_state("j1", "preflight")
    manager.set_state("j1", "analyzing_materials")
    manager.set_state("j1", "preparing")
    manager.set_state("j1", "rendering")
    with pytest.raises(ValueError, match="不可取消"):
        manager.cancel("j1")
    job = manager.get("j1")
    assert job is not None
    assert job.state == "rendering", "取消被拒绝后状态必须保持原样"
    assert job.outcome is None


def test_settle_is_idempotent(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    manager.create("j1", "m", "fp", "m", {}, str(tmp_path / "o"))
    manager.settle("j1", "success")
    first = manager.get("j1")
    again = manager.settle("j1", "failed")
    assert again.state == "success", "已落定的终态不得被二次改写"
    assert again.shippable is True
    assert first.updated_at == again.updated_at


# -- 公开序列化：不泄漏、且四个字段都在 ------------------------------------


@pytest.mark.parametrize(
    "script_name,expected_state",
    [
        ("fake_render_ok.py", "success"),
        ("fake_render_rejected.py", "rejected"),
        ("fake_render_diagnostic.py", "diagnostic"),
        ("fake_render_failed_token.py", "failed"),
        ("fake_render_unknown_token.py", "failed"),
    ],
)
def test_public_payload_exposes_terminal_contract(
    tmp_path: Path, script_name: str, expected_state: str
) -> None:
    final = _run(tmp_path, script_name)
    public = final.to_public(include_logs=True, include_artifacts=True)
    assert public["state"] == expected_state
    assert public["outcome"] in {"success", "rejected", "diagnostic", "failed"}
    assert public["shippable"] is (expected_state == "success")
    assert "output_dir" not in public, "公开序列化绝不泄漏输出目录"
    assert "path" not in public
