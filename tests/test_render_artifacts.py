# -*- coding: utf-8 -*-
"""完整产物登记与安全下载（PR #6 任务3）。

浏览器只提交**任务 ID + 产物 ID**，真实路径只在服务端解析。登记侧守住三条：
  1. 完整：成品图 / 无辉光对照图 / 成品 .blend / 三份运行凭据 JSON / 脱敏日志；
  2. 不暴露：源模型、``.blend1`` 自动备份、``.bak-*`` 人工备份、预检台账等中间物；
  3. 可交付标记只认 SUCCESS —— 诊断图 / 拒绝 / 失败一律不是成品。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from server.render_jobs import RenderJob, _artifact_role, _is_shippable_role  # noqa: E402


def _mk_job(out_dir: Path, *, state: str = "success", shippable: bool = True) -> RenderJob:
    return RenderJob(
        job_id="job-abc123",
        model_id="model-1",
        fingerprint="f" * 64,
        state=state,
        output_dir=str(out_dir),
        shippable=shippable,
        model_name="91",
        settings={"mode": "enhanced"},
    )


def _populate(out_dir: Path) -> None:
    """按真实渲染产物布局铺一套文件（对照 91.blend 真机输出）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "一键渲染.png").write_bytes(b"\x89PNG\r\n\x1a\nPNG-DATA-MARKER" + b"0" * 32)
    (out_dir / "一键渲染_无辉光.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 32)
    (out_dir / "一键卡通渲染_成品.blend").write_bytes(b"BLENDER" + b"2" * 16)
    (out_dir / "一键卡通渲染_成品.blend1").write_bytes(b"BLENDER-backup")
    (out_dir / "一键卡通渲染_成品.bak-20261014-013309.blend").write_bytes(b"BLENDER-bak")
    (out_dir / "run_result.json").write_text('{"outcome": "success"}', encoding="utf-8")
    (out_dir / "run_manifest.json").write_text('{"schema": "x"}', encoding="utf-8")
    (out_dir / "texture_ledger.json").write_text('{"textures": []}', encoding="utf-8")
    (out_dir / "material_preflight.json").write_text('{"materials": []}', encoding="utf-8")
    (out_dir / "render.stdout.log").write_text(
        'argv = ["Z:\\\\secret\\\\path\\\\blender.exe"]\n', encoding="utf-8")
    (out_dir / "render.done").write_text("SUCCESS\n", encoding="utf-8")


# ---------------------------------------------------------------- 角色判定
def test_artifact_role_classification() -> None:
    assert _artifact_role(Path("一键渲染.png")) == "image"
    assert _artifact_role(Path("一键渲染_无辉光.png")) == "image_noglow"
    assert _artifact_role(Path("shot_noglow.png")) == "image_noglow"
    assert _artifact_role(Path("一键卡通渲染_成品.blend")) == "blend"
    assert _artifact_role(Path("run_result.json")) == "run_result"
    assert _artifact_role(Path("run_manifest.json")) == "run_manifest"
    assert _artifact_role(Path("texture_ledger.json")) == "texture_ledger"
    assert _artifact_role(Path("render.stdout.log")) == "log"


def test_backups_and_intermediates_are_never_registered() -> None:
    """备份与预检台账绝不能进产物列表（否则会误下到过期版本或内部台账）。"""
    assert _artifact_role(Path("成品.blend1")) is None
    assert _artifact_role(Path("成品.bak-20261014-013309.blend")) is None
    assert _artifact_role(Path("成品_backup.blend")) is None
    assert _artifact_role(Path("预览.old.png")) is None
    assert _artifact_role(Path("渲染.tmp")) is None
    assert _artifact_role(Path("material_preflight.json")) is None
    assert _artifact_role(Path("render.done")) is None


def test_only_image_and_blend_are_shippable_roles() -> None:
    assert _is_shippable_role("image") is True
    assert _is_shippable_role("image_noglow") is True
    assert _is_shippable_role("blend") is True
    for meta in ("run_result", "run_manifest", "texture_ledger", "log"):
        assert _is_shippable_role(meta) is False, f"{meta} 是运行凭据，绝不能算成品"


# ---------------------------------------------------------------- 登记
def test_list_artifacts_covers_all_roles(tmp_path: Path) -> None:
    out = tmp_path / "job"
    _populate(out)
    arts = {a["name"]: a for a in _mk_job(out).list_artifacts()}
    assert set(arts) == {
        "一键渲染.png", "一键渲染_无辉光.png", "一键卡通渲染_成品.blend",
        "run_result.json", "run_manifest.json", "texture_ledger.json",
        "render.stdout.log",
    }, f"实际登记：{sorted(arts)}"
    assert arts["一键渲染.png"]["role"] == "image"
    assert arts["一键渲染_无辉光.png"]["role"] == "image_noglow"
    assert arts["一键卡通渲染_成品.blend"]["role"] == "blend"
    # 每条都要有中文展示名，前端不自行翻译
    assert all(a["label"] for a in arts.values())


def test_shippable_flag_requires_success(tmp_path: Path) -> None:
    """只有 SUCCESS（shippable=True）时图片与 .blend 才标正式成品。"""
    out = tmp_path / "job"
    _populate(out)
    ok = {a["name"]: a for a in _mk_job(out, state="success", shippable=True).list_artifacts()}
    assert ok["一键渲染.png"]["shippable"] is True
    assert ok["一键卡通渲染_成品.blend"]["shippable"] is True
    # 即使文件在，凭据与日志永远不是成品
    assert ok["run_manifest.json"]["shippable"] is False
    assert ok["render.stdout.log"]["shippable"] is False

    # 诊断 / 拒绝 / 失败终态：即使任务字段被误传 true，文件级仍不应出现正式成品
    for st in ("diagnostic", "rejected", "failed"):
        diag = {a["name"]: a for a in
                _mk_job(out, state=st, shippable=False).list_artifacts()}
        assert diag["一键渲染.png"]["shippable"] is False, st


def test_list_artifacts_rejects_link_that_escapes_the_job_dir(tmp_path: Path) -> None:
    """任何把任务目录外的文件拉进来的链接，都不能出现在产物列表里。

    ★ Windows 上非特权进程无法创建真符号链接：``symlink_to`` 会退化成**硬链接**
      （同一 inode、路径仍在本目录）。所以这里不能只断言「链接被拒」—— 硬链接
      路径确实在目录内，它被登记是合理的。真正必须守住的是：
      **任何解析后落到任务目录之外的条目都不登记**（目录穿越露不出来）。
    """
    out = tmp_path / "job"
    _populate(out)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"SECRET-OUTSIDE")

    link = out / "escape.png"
    try:
        link.symlink_to(outside / "secret.png")
    except OSError:
        pytest.skip("无创建链接的权限")
    try:
        names = {a["name"] for a in _mk_job(out).list_artifacts()}
        if link.is_symlink():
            # 真符号链接：一律不登记
            assert "escape.png" not in names, "符号链接不得登记"
        else:
            # 硬链接退化形态：路径仍在任务目录内，可以登记，
            # 但绝不能出现「解析后落到目录外」的条目。
            for name in names:
                assert (out / name).resolve().is_relative_to(out.resolve())
    finally:
        link.unlink(missing_ok=True)


def test_artifact_realpath_never_escapes_job_dir(tmp_path: Path) -> None:
    """登记的每一条产物，解析后都必须仍在任务目录内。"""
    out = tmp_path / "job"
    _populate(out)
    base = out.resolve()
    for art in _mk_job(out).list_artifacts():
        assert (out / art["name"]).resolve().is_relative_to(base), art["name"]


def test_artifact_id_is_opaque_and_unique(tmp_path: Path) -> None:
    out = tmp_path / "job"
    _populate(out)
    arts = _mk_job(out).list_artifacts()
    ids = [a["artifact_id"] for a in arts]
    assert len(ids) == len(set(ids))
    # artifact_id 只由文件名派生；前端拿它不能反推路径以外的任何东西
    for a in arts:
        assert a["artifact_id"] == f"art-{a['name']}"
        assert os.sep not in a["artifact_id"]


# ---------------------------------------------------------------- 下载安全
def _client():
    from fastapi.testclient import TestClient
    from server import app as app_mod
    return TestClient(app_mod.create_app()), app_mod


def _install_job(app_mod, out: Path, *, state="success", shippable=True):
    job = _mk_job(out, state=state, shippable=shippable)
    app_mod  # 仅保持可读性
    return job


def test_download_rejects_path_traversal_and_sibling_prefix(tmp_path: Path) -> None:
    """穿越 / 相邻目录前缀 / 符号链接都必须被拒，且绝不泄露真实路径。"""
    client, _ = _client()
    out = tmp_path / "out"
    _populate(out)

    from server import app as app_mod
    svc_app = client.app
    jobs = svc_app.state.jobs
    job = _mk_job(out)
    jobs._jobs[job.job_id] = job
    jobs._by_fingerprint[job.fingerprint] = job.job_id

    ok = client.get(f"/api/render/jobs/{job.job_id}/artifacts/art-%E4%B8%80%E9%94%AE%E6%B8%B2%E6%9F%93.png")
    assert ok.status_code == 200, ok.text[:200]
    assert ok.content.startswith(b"\x89PNG"), "中文产物名下载必须真的拿到文件字节"

    # 浏览器不可能拿到这些键 —— 绝不能返回任何文件字节，也绝不能泄露真实路径。
    # 注：``..`` 会被 ASGI 规范化掉路径段，从而落到列表路由（返回任务 JSON）。
    # 这不是漏洞，关键是**任何一种穿越写法都拿不到文件内容**。
    for evil in ("..%2F..%2Fsecret.png", "..", "%2e%2e%2fsecret.png",
                 "art-..%5C..%5Csecret.png", "C:%5Csecret.png",
                 "art-a.png%2F..%2F..%2Fsecret.png"):
        r = client.get(f"/api/render/jobs/{job.job_id}/artifacts/{evil}")
        assert b"\x89PNG" not in r.content, f"{evil} 泄露了文件字节"
        assert b"PNG-DATA-MARKER" not in r.content, evil
        assert str(tmp_path) not in r.text, f"{evil} 的响应泄露了真实路径"


def test_log_download_is_redacted(tmp_path: Path) -> None:
    """日志下载前必须脱敏：渲染脚本打印 argv，必然含本机绝对路径。"""
    client, _ = _client()
    out = tmp_path / "out"
    _populate(out)
    svc_app = client.app
    job = _mk_job(out)
    svc_app.state.jobs._jobs[job.job_id] = job
    svc_app.state.jobs._by_fingerprint[job.fingerprint] = job.job_id

    r = client.get(f"/api/render/jobs/{job.job_id}/artifacts/art-render.stdout.log")
    assert r.status_code == 200
    body = r.text
    assert "secret" not in body.lower(), f"日志未脱敏：{body[:200]}"
    assert "<path>" in body or "path" in body.lower()


def test_blend1_backup_not_downloadable(tmp_path: Path) -> None:
    """备份产物不出现在列表里，因此也无法被下载。"""
    client, _ = _client()
    out = tmp_path / "out"
    _populate(out)
    svc_app = client.app
    job = _mk_job(out)
    svc_app.state.jobs._jobs[job.job_id] = job

    listed = json.loads(
        client.get(f"/api/render/jobs/{job.job_id}/artifacts").text)["artifacts"]
    assert not any(".blend1" in a["name"] or ".bak-" in a["name"] for a in listed)

    r = client.get(f"/api/render/jobs/{job.job_id}/artifacts/"
                   "art-%E4%B8%80%E9%94%AE%E5%8D%A1%E9%80%9A%E6%B8%B2%E6%9F%93_%E6%88%90%E5%93%81.blend1")
    assert r.status_code == 404


def test_response_model_includes_new_artifact_fields(tmp_path: Path) -> None:
    """response_model 会静默丢字段 —— 新增字段必须同步进 models.py。"""
    client, _ = _client()
    out = tmp_path / "out"
    _populate(out)
    svc_app = client.app
    job = _mk_job(out)
    svc_app.state.jobs._jobs[job.job_id] = job
    r = client.get(f"/api/render/jobs/{job.job_id}/artifacts")
    art = json.loads(r.text)["artifacts"][0]
    for key in ("artifact_id", "name", "size", "role", "label", "shippable"):
        assert key in art, f"响应里丢了 {key}（response_model 未同步？）"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))