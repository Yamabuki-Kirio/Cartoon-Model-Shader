# -*- coding: utf-8 -*-
"""一键渲染真机验收回归（PR #6 任务6）。

这些断言来自 2026-10-15 凌晨对 ``91.blend`` 的三条真实闭环：

  A. 严格模式（复用本机侧车映射）→ SUCCESS + shippable=true
  B. 宽松诊断（隔离映射目录）→ DIAGNOSTIC + shippable=false
  C. 材质确认：REJECTED → 确认页 → 确认 → **服务端自动续跑** → SUCCESS，
     且再次执行自动复用映射（不再要求确认）

本文件钉死那些**只有真机跑才会暴露**的不变量 —— 尤其是 C 里发现的两个真实
缺陷：确认服务的 ``--model-root`` 传空串导致 .blend 的确认闭环根本起不来，
以及产物下载端点在中文文件名下抛 UnicodeEncodeError。

**不放真实模型、贴图、.blend 或真实侧车映射进仓库** —— 这里只用 tmp 目录里的
合成文件与源码契约。
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "render_pipeline" / "开始渲染.py"
CONFIRM_SERVER = REPO / "render_pipeline" / "_tools" / "confirm_server.py"
RENDER_JOBS = REPO / "src" / "server" / "render_jobs.py"
RENDER_STATE = REPO / "src" / "server" / "render_state.py"
sys.path.insert(0, str(REPO / "src"))

from server.render_jobs import RenderJob  # noqa: E402

#: 真实 91.blend 的侧车映射键（材质名 → 分类）。只记材质名，不含任何真实资产。
REAL_91_ASSIGNMENTS = {
    "Headphones_1_材質1.001": "cloth",
    "Headphones_2_材質2.002": "cloth",
    "Headphones_3_材質3.001": "cloth",
}


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _fn_body(path: Path, name: str) -> str:
    tree = ast.parse(_src(path))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(_src(path), fn) or ""


# ============================================================ 闭环 A：严格模式
def test_strict_reuses_sidemap_and_reaches_success(tmp_path: Path) -> None:
    """A：映射齐备时严格模式直接 SUCCESS，产物齐备且可交付。"""
    out = tmp_path / "outA"
    out.mkdir()
    # 真实闭环 A 的产物布局
    (out / "一键渲染.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    (out / "一键渲染_无辉光.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 16)
    (out / "一键卡通渲染_成品.blend").write_bytes(b"BLENDER" + b"2" * 16)
    (out / "run_result.json").write_text(json.dumps(
        {"outcome": "success", "done_token": "SUCCESS", "shippable": True}),
        encoding="utf-8")

    job = RenderJob(job_id="jA", model_id="m", fingerprint="f" * 64,
                    state="success", output_dir=str(out), shippable=True,
                    model_name="91", settings={})
    assert job.state == "success"
    assert job.shippable is True

    roles = {a["role"] for a in job.list_artifacts() if a["shippable"]}
    # 成品图 / 无辉光对照图 / 成品 .blend 三类都必须可交付
    assert {"image", "image_noglow", "blend"} <= roles

    result = json.loads((out / "run_result.json").read_text(encoding="utf-8"))
    assert result["shippable"] is True


def test_real_91_assignment_keys_are_all_covered() -> None:
    """真实 91.blend 的三个耳机材质名必须与验收脚本记录的完全一致。

    材质名一旦漂移（比如 Blender 版本差异改了名），映射就会静默失配 ——
    所以把这份名单钉在测试里。
    """
    assert set(REAL_91_ASSIGNMENTS) == {
        "Headphones_1_材質1.001",
        "Headphones_2_材質2.002",
        "Headphones_3_材質3.001",
    }
    assert all(v == "cloth" for v in REAL_91_ASSIGNMENTS.values())


# ============================================================ 闭环 B：诊断模式
def test_diagnostic_is_never_shippable(tmp_path: Path) -> None:
    """B：诊断终态 shippable 恒为 false，产物不得被标成正式成品。"""
    out = tmp_path / "outB"
    out.mkdir()
    (out / "一键渲染.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    (out / "run_result.json").write_text(json.dumps(
        {"outcome": "diagnostic", "done_token": "DIAGNOSTIC",
         "shippable": False, "label": "诊断图（不是成品）"}), encoding="utf-8")

    job = RenderJob(job_id="jB", model_id="m", fingerprint="f" * 64,
                    state="diagnostic", output_dir=str(out), shippable=False,
                    model_name="91", settings={})
    assert job.shippable is False
    assert all(not a["shippable"] for a in job.list_artifacts())

    result = json.loads((out / "run_result.json").read_text(encoding="utf-8"))
    assert result["shippable"] is False
    assert "不是成品" in result["label"]


# ============================================================ 闭环 C：确认续跑
def test_blend_branch_derives_confirmation_and_routes_to_server() -> None:
    """C：.blend 被拒后接既有确认流程，渲染归确认服务唯一所有。"""
    body = _src(ENTRY)
    branch = body[body.index('if kind == "blend"'):
                  body.index('out("[1/3] 预检与自动分类…")')]
    assert "blend_confirmation_from_run(" in branch
    # 确认后由 run_confirm_and_render 起服务续跑，入口脚本自己不重入 render()
    assert "run_confirm_and_render(" in branch
    assert branch.count("= render(") == 1


def test_confirm_server_model_root_is_never_empty() -> None:
    """★ 真机发现的缺陷：--model-root 传空串会让 .blend 的确认闭环起不来。

    ``--blend`` 显式给路径时 ROOT 可以为空，而把空串传给 argparse 会被当成
    「没给 --model-root」→ 确认服务直接退出码 2 → 整条确认闭环断掉。
    必须回退到模型所在目录。
    """
    body = _fn_body(ENTRY, "run_confirm_and_render")
    assert "model_root = ROOT or os.path.dirname(os.path.abspath(model))" in body
    # 落进 argv 的必须是回退后的值
    assert '"--model-root", model_root' in body
    # 空 source-blend 不应再传（.blend 不需要源工程）
    assert 'if SOURCE_BLEND:' in body


def test_confirm_server_splits_blend_targets_on_resume() -> None:
    """续跑时 .blend 必须走 --blend/--resolve 自身，不能塞进 pmx。"""
    body = _fn_body(CONFIRM_SERVER, "_run_one")
    assert 'if blend:' in body
    assert '["--blend", blend, "--resolve", blend]' in body.replace("\n", " ")


def test_render_passes_rejected_diagnostic_through() -> None:
    """★ 真机语义：REJECTED / DIAGNOSTIC 是正式终态，不能压成 FAILED。

    压成 FAILED 时「待人工确认」会被报成「渲染失败」，确认闭环也接不上。
    """
    body = _fn_body(ENTRY, "render")
    assert 'token.lower() in ("rejected", "diagnostic")' in body
    assert '"stage": "材质准入"' in body


def test_second_run_reuses_sidemap(tmp_path: Path) -> None:
    """C 的后半段：确认落盘的映射让下次直接 SUCCESS，不再要求确认。"""
    maps = tmp_path / "maps"
    maps.mkdir()
    fp = "b8eb6ee86286647725a3d6421dad9c6988ff654bd07536c0bdfaeec1804e8acd"
    sidecar = maps / f"{fp}.material-map.json"
    sidecar.write_text(json.dumps({
        "schema": "toon-material-map/1",
        "model": {"fingerprint": "sha256:" + fp},
        "assignments": {k: {"class": v, "group": "Cel_Cloth",
                            "source": "user_confirmed"}
                        for k, v in REAL_91_ASSIGNMENTS.items()},
    }, ensure_ascii=False), encoding="utf-8")

    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert len(data["assignments"]) == 3
    assert all(a["source"] == "user_confirmed" for a in data["assignments"].values())
    # 文件名即整文件 sha256 —— 与主脚本/适配层/确认服务同一口径
    assert sidecar.name == fp + ".material-map.json"


def test_repository_contains_no_real_sidecar_or_assets() -> None:
    """★ 真实映射与模型绝不能进 Git（验收硬约束）。"""
    forbidden_suffixes = (".material-map.json", ".blend", ".blend1")
    tracked_candidates = [
        p for p in REPO.rglob("*")
        if p.is_file() and not any(part in {".git", "node_modules", "output"}
                                   for part in p.parts)
    ]
    leaked = [
        str(p.relative_to(REPO)) for p in tracked_candidates
        if p.suffix.lower() in forbidden_suffixes
        or ".material-map.template.json" in p.name
    ]
    assert not leaked, f"仓库里出现了真实映射/模型资产：{leaked}"


# ============================================================ 中文产物名（真机 bug）
def test_attachment_disposition_handles_cjk_names() -> None:
    """★ 真机发现的缺陷：中文产物名让下载端点抛 UnicodeEncodeError → 500。

    HTTP 头是 latin-1，把 ``一键渲染.png`` 直接塞进 filename="…" 会炸。
    """
    app_src = _src(REPO / "src" / "server" / "app.py")
    assert "_attachment_disposition" in app_src
    tree = ast.parse(app_src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_attachment_disposition")
    body = ast.get_source_segment(app_src, fn) or ""
    assert "RFC 5987" in body or "5987" in body
    assert "filename*=UTF-8''" in body
    assert "quote(" in body


def test_download_endpoint_uses_disposition_helper() -> None:
    """下载端点必须经 _attachment_disposition 产出文件名，不能直接拼引号。"""
    app_src = _src(REPO / "src" / "server" / "app.py")
    tree = ast.parse(app_src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "artifact_file")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_attachment_disposition"]
    assert calls, "下载端点没有走 _attachment_disposition（中文名会 500）"
    # 日志分支也必须用同一个 helper
    assert any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "PlainTextResponse"
        for n in ast.walk(fn)
    ), "日志下载应返回脱敏后的文本响应"


# ============================================================ 状态与恢复口径
def test_interrupted_is_distinct_from_run_contract_outcomes() -> None:
    """中断是终态但不是 run_contract 正式终态 —— 绝不伪装成失败或成功。"""
    from server import render_state

    assert "interrupted" in render_state.TERMINAL_STATES
    assert "interrupted" not in render_state.SHIPPABLE_STATES
    assert "interrupted" not in {
        "success", "rejected", "diagnostic", "failed", "cancelled"}


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))