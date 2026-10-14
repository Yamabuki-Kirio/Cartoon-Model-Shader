# -*- coding: utf-8 -*-
""".blend 材质确认闭环（PR #6 任务2）。

``.blend`` 的材质清单只能在 Blender 内读，Python 侧预检（PMX 二进制解析器）用不上。
主脚本在 ``strict`` 拒绝时把逐材质台账写进输出目录 ``material_preflight.json``；
入口脚本据此反推出**既有确认页契约**的清单（字段搬运，不新建确认系统），
再走原路径：确认页 → 侧车映射落盘 → 确认服务自动续跑。

本组测试守住三件事：
  1. 反推的清单与 ``build_confirmation.build_one`` 的结构一致（否则确认页读不了）；
  2. ``render()`` 不再把 REJECTED/DIAGNOSTIC 压成 FAILED（否则待确认被误报成渲染失败）；
  3. confirm_server 从确认清单派生 job 时按 model_kind 分流 pmx/blend。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "render_pipeline" / "开始渲染.py"
CONFIRM_SERVER = REPO / "render_pipeline" / "_tools" / "confirm_server.py"
BUILD_CONF = REPO / "render_pipeline" / "_tools" / "build_confirmation.py"


def _load_entry():
    spec = importlib.util.spec_from_file_location("entry_blend_confirm", ENTRY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "render_pipeline"))
    sys.path.insert(0, str(REPO / "render_pipeline" / "_tools"))
    spec.loader.exec_module(mod)
    return mod


def _write_preflight(out_dir: Path, *, fingerprint: str) -> dict[str, Any]:
    """模拟主脚本 strict 拒绝时留下的逐材质分类台账。"""
    rec = {
        "schema": "toon-preflight-run/1",
        "policy": "strict",
        "outcome": "rejected",
        "model_fingerprint": "sha256:" + fingerprint,
        "targets": ["scene.blend"],
        "summary": {"resolved": 1, "unresolved": 2, "low_confidence": 1, "ignored": 0},
        "materials": [
            {  # 已解决 → 不进确认清单
                "original_name": "Body_材質1", "class": "resolved", "group": "Cel_Skin",
                "confidence": 0.92, "needs_review": False, "note": None,
            },
            {  # 未解决 → 必须要求用户确认
                "original_name": "Headphones_1_材質1.001", "class": "unresolved",
                "group": None, "confidence": 0.0, "needs_review": True,
                "note": "无语义依据", "matched_by": None,
            },
            {  # 有建议但需复核 → 也要确认，但 stage 是 SUGGESTED
                "original_name": "Coat_材質3", "class": "resolved", "group": "Cel_Cloth",
                "confidence": 0.55, "needs_review": True, "note": "低置信度",
                "matched_by": "rule",
            },
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "material_preflight.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rec


# ---------------------------------------------------------------- 反推清单
def test_blend_confirmation_matches_build_one_contract(tmp_path: Path) -> None:
    """反推清单必须与 build_one 的契约同构 —— 否则既有确认页读不出来。"""
    mod = _load_entry()
    out_dir = tmp_path / "out"
    _write_preflight(out_dir, fingerprint="a" * 64)
    model = tmp_path / "scene.blend"
    model.write_bytes(b"BLENDER-scene")
    mod.CONF_DIR = str(tmp_path / "conf")

    conf_path, payload = mod.blend_confirmation_from_run(str(out_dir), str(model))
    assert conf_path is not None and payload is not None

    # ① 与 build_one 一样的顶层键
    ref = ast.parse(BUILD_CONF.read_text(encoding="utf-8"))
    build_ret_keys = None
    for node in ast.walk(ref):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = [k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if "model" in keys and "items" in keys and "summary_all" in keys:
                build_ret_keys = set(keys)
                break
    assert build_ret_keys, "无法从 build_confirmation 里定位返回结构（契约可能变了）"
    assert build_ret_keys <= set(payload), "反推清单缺少 build_one 的必需键"

    # ② 指纹口径：整文件 sha256，带 sha256: 前缀（确认服务据此定位侧车映射）
    assert payload["model"]["fingerprint"] == "sha256:" + "a" * 64
    assert payload["model"]["path"] == str(model)
    assert payload["model"]["file"] == "scene.blend"

    # ③ 只有未解决/待复核的材质进清单，且都要 requires_user
    names = [x["material"] for x in payload["items"]]
    assert names == ["Headphones_1_材質1.001", "Coat_材質3"]
    assert all(x["requires_user"] for x in payload["items"])
    assert payload["items"][0]["stage"] == "UNRESOLVED"
    assert payload["items"][1]["stage"] == "SUGGESTED"

    # ④ 已解决材质绝不能混进确认清单（否则用户被迫重复确认已定项）
    assert "Body_材質1" not in names

    # ⑤ 确认页会读 previews 三键；.blend 没有预览图，必须给空串而不是缺键
    for item in payload["items"]:
        assert set(item["previews"]) == {
            "mask_only", "overview_highlighted", "proposed_group"}

    # ⑥ 清单落盘到 CONF_DIR，文件名按整文件指纹前 16 位（适配层同口径查找）
    assert Path(conf_path).name == ("a" * 16) + "_confirmation.json"
    on_disk = json.loads(Path(conf_path).read_text(encoding="utf-8"))
    assert on_disk["model"]["fingerprint"] == payload["model"]["fingerprint"]


def test_blend_confirmation_no_pending_returns_none(tmp_path: Path) -> None:
    """全部材质已解决 → 没有待确认项，返回 (None, None)，调用方照实报终态。"""
    mod = _load_entry()
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)
    rec = {
        "model_fingerprint": "sha256:" + "b" * 64,
        "targets": ["scene.blend"],
        "materials": [{"original_name": "Skin", "class": "resolved",
                       "group": "Cel_Skin", "needs_review": False,
                       "confidence": 0.9, "note": None}],
    }
    (out_dir / "material_preflight.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    model = tmp_path / "scene.blend"
    model.write_bytes(b"BLENDER-scene")

    assert mod.blend_confirmation_from_run(str(out_dir), str(model)) == (None, None)


def test_blend_confirmation_missing_or_broken_file(tmp_path: Path) -> None:
    """台账缺失或损坏必须安全降级，绝不抛异常打断任务。"""
    mod = _load_entry()
    model = tmp_path / "scene.blend"
    model.write_bytes(b"BLENDER-scene")
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)

    assert mod.blend_confirmation_from_run(str(out_dir), str(model)) == (None, None)

    (out_dir / "material_preflight.json").write_text("{ broken", encoding="utf-8")
    assert mod.blend_confirmation_from_run(str(out_dir), str(model)) == (None, None)


# ---------------------------------------------------------------- 终态透出
def test_render_passes_rejected_and_diagnostic_through(tmp_path: Path) -> None:
    """REJECTED / DIAGNOSTIC 是正式终态，必须原样透出而不是压成 FAILED。

    压成 FAILED 会导致「待人工确认」被误报成「渲染失败」，也无法接确认闭环。
    """
    src = ENTRY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    render_fn = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "render")
    body = ast.get_source_segment(src, render_fn) or ""
    assert 'token.lower() in ("rejected", "diagnostic")' in body, \
        "render() 必须把 rejected/diagnostic 作为正式终态透出"
    # 且失败分支的错误码里不能把这两个终态当成 code
    tail = body[body.index("token.lower()"):]
    assert '"RENDER_TIMEOUT"' in tail


def test_render_does_not_start_second_blender_on_rejected(tmp_path: Path) -> None:
    """同一任务只能有一个 Blender 进程树：确认路径由确认服务续跑，render() 不重入。"""
    src = ENTRY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    body = ast.get_source_segment(src, main_fn) or ""
    blend_branch = body[body.index('if kind == "blend"'):body.index('out("[1/3] 预检与自动分类…")')]
    # blend 分支最多一次 render() 调用；确认续跑走 run_confirm_and_render
    assert blend_branch.count("= render(") == 1
    assert blend_branch.count("run_confirm_and_render(") == 1


def test_blend_branch_routes_rejected_into_confirmation() -> None:
    """.blend 被拒后必须接既有确认闭环，且 diagnostic 口径不做确认。"""
    src = ENTRY.read_text(encoding="utf-8")
    body = src[src.index('if kind == "blend"'):src.index('out("[1/3] 预检与自动分类…")')]
    assert "blend_confirmation_from_run(" in body
    assert 'outcome.get("token") != "REJECTED"' in body
    assert 'a.material_policy != "strict"' in body
    # 关键词必须命中适配层的 waiting_confirmation 匹配
    assert "材质需要确认" in body


# ---------------------------------------------------------------- 确认服务端
def test_derive_job_splits_blend_targets() -> None:
    """确认服务派生 job 时按 model_kind 分流：.blend 不能塞进 pmx。

    塞错会让驱动走 PMX 导入分支（读不了二进制直接报错）。
    """
    src = CONFIRM_SERVER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_derive_job_from_conf")
    body = ast.get_source_segment(src, fn) or ""
    assert 'model_kind") == "blend"' in body or 'model_kind")=="blend"' in body
    assert '"blend": p if is_blend else None' in body.replace("\n", " ").replace("  ", " ") \
        or '"blend": p if is_blend else None' in body


def test_confirm_page_tolerates_missing_previews() -> None:
    """确认页遇到空预览 src 必须跳过，不能 <img src=""> 把页面地址当图片再请求。"""
    html = (REPO / "render_pipeline" / "_tools" / "confirm_page.html").read_text(
        encoding="utf-8")
    assert "filter(([k])=>pv[k])" in html.replace(" ", "")
    assert "nopv" in html


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))