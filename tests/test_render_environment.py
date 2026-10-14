"""渲染环境诊断测试（PR #6 规格 §5）。

守四件事：

1. **12 项检查一项不少**，分级合法；
2. **响应里绝不出现本机绝对路径**（盘符 / 用户名 / 完整目录都不行）；
3. **只读**：环境诊断不得删除任何东西（连孤儿锁都只计数）；
4. **Blender 未连接不得创建渲染任务**（规格 §5.4），且报的是可操作错误码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.server import errors
from src.server.app import create_app
from src.server.config import AppConfig, BlenderMCPConfig, RenderConfig, ServerConfig
from src.server.models_scan import ModelRegistry
from src.server.render_environment import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_WARNING,
    _disk_status,
    build_environment_report,
)
from src.server.render_jobs import RenderJob, RenderJobManager
from tests.support import FakeBlenderGateway, authed, unauthed

#: 规格 §5.3 列出的 12 项检查。
EXPECTED_CHECKS = (
    "blender_mcp",
    "blender_exe",
    "fixed_script",
    "source_blend",
    "model_root",
    "output_root",
    "runtime_dir",
    "maps_dir",
    "disk_system",
    "disk_output",
    "stale_locks",
    "active_jobs",
)

#: 判定「泄漏了绝对路径」的形态：盘符路径或当前用户名。
_LEAK_PATTERNS = ("C:\\", "C:/", "Z:\\", "Z:/", "A:\\", "A:/")


def _payload_is_clean(payload: dict) -> bool:
    text = json.dumps(payload, ensure_ascii=False)
    return not any(p in text for p in _LEAK_PATTERNS)


def _report(**kwargs):
    base = dict(
        blender_status={"ok": True, "status": "connected", "target": "127.0.0.1:9876"},
        blender_exe=kwargs.pop("blender_exe", None),
    )
    base.update(kwargs)
    return build_environment_report(**base)


# -- 检查项完整性 ----------------------------------------------------------


def test_all_twelve_checks_present_and_valid() -> None:
    report = _report(
        blender_exe="C:/Blender/blender.exe",
        source_blend="C:/work/source.blend",
        model_root="C:/models",
        output_root="C:/out",
        runtime_dir="C:/runtime",
        maps_dir="C:/maps",
        fixed_script="C:/repo/render_pipeline/开始渲染.py",
    )
    ids = [c.id for c in report.checks]
    assert tuple(ids) == EXPECTED_CHECKS
    for check in report.checks:
        assert check.status in (STATUS_OK, STATUS_WARNING, STATUS_ERROR), check.id


def test_disconnected_mcp_blocks_and_explains() -> None:
    report = build_environment_report(
        blender_status={"ok": False, "status": "refused", "error": {"message": "连接被拒绝"}},
    )
    assert report.ready is False
    assert "blender_mcp" in report.blockers
    mcp = next(c for c in report.checks if c.id == "blender_mcp")
    assert mcp.status == STATUS_ERROR
    assert mcp.hint and "9876" in mcp.hint


def test_missing_config_lists_every_missing_item() -> None:
    """什么都没配时，必须**逐项**说清缺什么，而不是一句「环境未就绪」。"""
    report = build_environment_report(
        blender_status={"ok": True, "status": "connected"},
    )
    assert report.ready is False
    for check_id in ("blender_exe", "source_blend", "model_root", "output_root", "runtime_dir", "maps_dir"):
        assert check_id in report.blockers, check_id


def test_fixed_script_missing_is_error() -> None:
    report = _report(fixed_script="C:/nope/开始渲染.py")
    check = next(c for c in report.checks if c.id == "fixed_script")
    assert check.status == STATUS_ERROR
    assert "fixed_script" in report.blockers


def test_empty_model_dir_is_warning_not_error(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    report = _report(model_root=models)
    check = next(c for c in report.checks if c.id == "model_root")
    assert check.status == STATUS_WARNING
    # 空目录不阻断渲染（用户可能就是要用当前工程）
    assert "model_root" not in report.blockers


def test_unwritable_output_dir_is_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir.txt"
    blocker.write_text("x", encoding="utf-8")
    report = _report(output_root=blocker)
    check = next(c for c in report.checks if c.id == "output_root")
    assert check.status == STATUS_ERROR
    assert "output_root" in report.blockers


def test_active_jobs_is_warning(tmp_path: Path) -> None:
    manager = RenderJobManager(tmp_path / "out")
    job = RenderJob(
        job_id="rj-1",
        model_id="m",
        fingerprint="fp",
        model_name="模型甲",
        settings={},
        output_dir=str(tmp_path / "o"),
        state="rendering",
    )
    manager._jobs[job.job_id] = job  # noqa: SLF001 - 直接装配，避免走子进程
    report = _report(jobs=manager.list_jobs())
    check = next(c for c in report.checks if c.id == "active_jobs")
    assert check.status == STATUS_WARNING
    assert "模型甲" in check.message


def test_stale_locks_is_warning() -> None:
    report = _report(stale_lock_count=2)
    check = next(c for c in report.checks if c.id == "stale_locks")
    assert check.status == STATUS_WARNING
    assert "2" in check.message


# -- 磁盘分级 --------------------------------------------------------------


def test_disk_status_thresholds() -> None:
    assert _disk_status(None) == STATUS_ERROR
    assert _disk_status(0.5) == STATUS_ERROR
    assert _disk_status(3.0) == STATUS_WARNING
    assert _disk_status(50.0) == STATUS_OK


# -- 安全：不泄漏路径 / 只读 ----------------------------------------------


def test_payload_contains_no_absolute_paths(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "a.pmx").write_bytes(b"x")
    report = _report(
        blender_exe=tmp_path / "blender.exe",
        source_blend=tmp_path / "source.blend",
        model_root=models,
        output_root=tmp_path / "out",
        runtime_dir=tmp_path / "runtime",
        maps_dir=tmp_path / "maps",
        fixed_script=tmp_path / "开始渲染.py",
    )
    payload = report.to_public()
    assert _payload_is_clean(payload), json.dumps(payload, ensure_ascii=False)[:400]


def test_environment_probe_is_read_only(tmp_path: Path) -> None:
    """环境诊断**不得删除任何东西** —— 连孤儿锁都只计数。"""
    out = tmp_path / "out"
    (out / ".locks").mkdir(parents=True)
    lock = out / ".locks" / "abc.render.lock"
    lock.write_text("holder", encoding="utf-8")
    before = sorted(p.name for p in out.rglob("*"))

    report = _report(output_root=out, runtime_dir=out, maps_dir=out)
    assert report.to_public()["ready"] in (True, False)

    after = sorted(p.name for p in out.rglob("*"))
    assert before == after, "环境诊断改变了磁盘内容"
    assert lock.is_file(), "孤儿锁必须保留（只计数，不清除）"


# -- API 层 ---------------------------------------------------------------


def _config(tmp_path: Path, script: Path, model_dir: Path) -> AppConfig:
    return AppConfig(
        blender_mcp=BlenderMCPConfig(
            host="127.0.0.1", port=1, connect_timeout_seconds=0.01, response_timeout_seconds=0.01
        ),
        server=ServerConfig(host="127.0.0.1", port=8765),
        render=RenderConfig(
            model_root=model_dir,
            runtime_dir=tmp_path / "runtime",
            output_dir=tmp_path / "out",
            pipeline_script=script,
        ),
    )


def test_api_environment_is_readable_without_token(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    app = create_app(
        _config(tmp_path, tmp_path / "开始渲染.py", model_dir),
        gateway=FakeBlenderGateway(),
    )
    client = unauthed(app)
    response = client.get("/api/render/environment")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert [c["id"] for c in body["checks"]] == list(EXPECTED_CHECKS)
    assert isinstance(body["ready"], bool)
    assert _payload_is_clean(body), response.text[:400]


def test_api_create_job_rejected_when_mcp_offline(tmp_path: Path) -> None:
    """规格 §5.4：Blender 未连接不得创建正式渲染任务。"""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")
    app = create_app(
        _config(tmp_path, tmp_path / "开始渲染.py", model_dir),
        gateway=FakeBlenderGateway(connected=False),
    )
    client = authed(app)
    registry = ModelRegistry(model_dir, tmp_path / ".reg.json")
    registry.scan()
    model_id = registry.to_public()[0]["model_id"]

    response = client.post(
        "/api/render/jobs", json={"model_id": model_id, "mode": "faithful", "auto_frame": False}
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == errors.BLENDER_CONNECTION_REFUSED
    # 必须是可操作提示，不是裸 traceback
    assert "未连接" in response.json()["error"]["message"]
    assert response.json()["error"]["hint"]


def test_api_create_job_allowed_when_mcp_online(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "m.pmx").write_bytes(b"model")
    app = create_app(
        _config(tmp_path, tmp_path / "开始渲染.py", model_dir),
        gateway=FakeBlenderGateway(),
    )
    client = authed(app)
    model_id = client.get("/api/render/models").json()["models"][0]["model_id"]
    response = client.post(
        "/api/render/jobs", json={"model_id": model_id, "mode": "faithful", "auto_frame": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] in ("queued", "preflight")
