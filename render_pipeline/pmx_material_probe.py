# -*- coding: utf-8 -*-
# =====================================================================================
#  【v3 遗留工具 · 保留说明】
#  v3.1 的预检与确认清单由 _tools/build_confirmation.py 产出（它接入了结构特征层
#  与确定性分级）。本文件不再承担 v3.1 的预检职责，保留原因有两个：
#    1. v3 契约测试（tests/test_classifier.py）依赖它渲染 markdown；
#    2. 它是不依赖 Blender 的 PMX 二进制复算入口，审计方可独立复跑。
#  规则仍只从 material_rules.json 读 —— 不存在第二份规则副本。
# =====================================================================================
"""
PMX 材质预检器（v3）
=====================================================================================
在启动 Blender、【导入之前】就把模型的材质表读出来并完成分类，回答三个问题：

  1. 这个模型能不能过严格准入？（不需要开 Blender 等 5 分钟才知道）
  2. 不能过的话，卡在哪些材质上？该归到哪个类？
  3. 用户确认一次之后，下次能不能自动复用？

产出：
  material_preflight.json   机器可读（供驱动/CI 消费）
  material_preflight.md     人可读（供人工确认）

规则来源：material_rules.json（唯一规则源，与主脚本同源，不复制）。

用法：
  python pmx_material_probe.py --pmx <模型.pmx> [--out-dir <目录>]
  python pmx_material_probe.py --batch <根目录> --out-dir <目录>
  python pmx_material_probe.py --pmx <模型.pmx> --write-map-template
"""
import argparse
import glob
import hashlib
import json
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from material_classifier import (MaterialClassifier, map_path_for, load_model_map,
                                 write_map_template, strip_blender_ext)  # noqa: E402

SIG = b"PMX "


# ------------------------------------------------------------------ PMX 解析
class PmxReader(object):
    def __init__(self, path):
        self.path = path
        self.buf = open(path, "rb").read()
        self.p = 0

    def need(self, n):
        if self.p + n > len(self.buf):
            raise EOFError("越界读取 @%d (+%d)" % (self.p, n))

    def u8(self):
        self.need(1)
        v = self.buf[self.p]
        self.p += 1
        return v

    def i32(self):
        self.need(4)
        v = struct.unpack_from("<i", self.buf, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        self.need(4)
        v = struct.unpack_from("<f", self.buf, self.p)[0]
        self.p += 4
        return v

    def skip(self, n):
        self.need(n)
        self.p += n

    def text(self, enc):
        n = self.i32()
        if n <= 0:
            return ""
        self.need(n)
        raw = self.buf[self.p:self.p + n]
        self.p += n
        return raw.decode("utf-16-le" if enc == 0 else "utf-8", "replace")

    def index(self, size, signed=True):
        if size == 1:
            v = self.u8()
            return v - 256 if (signed and v >= 128) else v
        if size == 2:
            self.need(2)
            v = struct.unpack_from("<H", self.buf, self.p)[0]
            self.p += 2
            return v - 65536 if (signed and v >= 32768) else v
        return self.i32()


def parse_pmx_materials(path):
    """解析 PMX，返回 header + 材质列表（含双面标志与贴图槽）。"""
    r = PmxReader(path)
    out = {"file": os.path.basename(path), "path": path, "ok": False,
           "error": None, "materials": [], "textures": []}
    try:
        if r.buf[:4] != SIG:
            out["error"] = "签名非 'PMX '：%r" % r.buf[:4]
            return out
        r.p = 4
        version = r.f32()
        gcount = r.u8()
        g = [r.u8() for _ in range(gcount)]
        if gcount < 8:
            out["error"] = "globals 数量异常：%d" % gcount
            return out
        enc, add_uv, vidx, tidx, midx, bidx = g[0], g[1], g[2], g[3], g[4], g[5]
        out["encoding"] = "UTF-16LE" if enc == 0 else "UTF-8"
        out["version"] = round(version, 2)
        out["model_name"] = r.text(enc)
        out["model_name_en"] = r.text(enc)
        r.text(enc)
        r.text(enc)

        vcount = r.i32()
        for _ in range(vcount):
            r.skip(12 + 12 + 8 + add_uv * 16)
            wt = r.u8()
            if wt == 0:
                r.index(bidx)
            elif wt == 1:
                r.index(bidx); r.index(bidx); r.skip(4)
            elif wt == 2:
                for _i in range(4):
                    r.index(bidx)
                r.skip(16)
            elif wt == 3:
                r.index(bidx); r.index(bidx); r.skip(4 + 12 + 12 + 12)
            elif wt == 4:
                for _i in range(4):
                    r.index(bidx)
                r.skip(16)
            else:
                raise ValueError("未知 weight type %d" % wt)
            r.skip(4)
        out["vertex_count"] = vcount

        fcount = r.i32()
        r.skip(fcount * vidx)
        out["face_index_count"] = fcount

        tcount = r.i32()
        texs = [r.text(enc) for _ in range(tcount)]
        out["textures"] = texs
        out["texture_count"] = tcount

        mcount = r.i32()
        for i in range(mcount):
            name = r.text(enc)
            name_en = r.text(enc)
            diffuse = [round(r.f32(), 4) for _ in range(4)]
            r.skip(12)
            r.skip(4)
            r.skip(12)
            draw_flag = r.u8()
            r.skip(16)
            r.skip(4)
            ti = r.index(tidx)
            ei = r.index(tidx)
            env_mode = r.u8()
            toon_ref = r.u8()
            if toon_ref == 0:
                r.index(tidx)
            else:
                r.u8()
            memo = r.text(enc)
            surf = r.i32()
            out["materials"].append({
                "index": i,
                "name": name,
                "name_en": name_en,
                "alpha": diffuse[3],
                "diffuse_rgb": diffuse[:3],
                "texture_index": ti,
                "texture": texs[ti] if 0 <= ti < len(texs) else None,
                "env_index": ei,
                "env_texture": texs[ei] if 0 <= ei < len(texs) else None,
                "env_blend_mode": env_mode,
                "draw_flag": draw_flag,
                "double_sided": bool(draw_flag & 0x01),
                "has_edge": bool(draw_flag & 0x10),
                "memo": memo,
                "surface_count": surf,
            })
        out["material_count"] = mcount
        out["ok"] = True
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


# ------------------------------------------------------------------ 指纹
def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def structure_fingerprint(parsed):
    """结构指纹：材质名 + 面数 + 贴图引用（不含路径）。用于跨目录移动后仍能认回同一模型。"""
    items = []
    for m in parsed.get("materials", []):
        items.append("%s|%d|%s" % (m["name"], m.get("surface_count", 0), m.get("texture") or ""))
    blob = "\n".join(items)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 预检
def preflight(pmx_path, classifier, maps_dir=None, explicit_map=None, low_conf_threshold=None,
              skip_model_map=False):
    parsed = parse_pmx_materials(pmx_path)
    rec = {
        "pmx": os.path.basename(pmx_path),
        "pmx_path": pmx_path,
        "parse_ok": parsed["ok"],
        "parse_error": parsed["error"],
        "model_name": parsed.get("model_name"),
        "version": parsed.get("version"),
        "encoding": parsed.get("encoding"),
        "vertex_count": parsed.get("vertex_count"),
        "material_count": parsed.get("material_count"),
        "texture_count": parsed.get("texture_count"),
        "fingerprint_file": None,
        "fingerprint_structure": None,
        "map_used": None,
        "materials": [],
        "resource_issues": [],
    }
    if not parsed["ok"]:
        rec["would_pass_strict"] = False
        rec["outcome"] = "failed"
        return rec

    try:
        rec["fingerprint_file"] = "sha256:" + file_sha256(pmx_path)
    except Exception as e:
        rec["resource_issues"].append("无法计算文件指纹：%s" % e)
    rec["fingerprint_structure"] = "sha256:" + structure_fingerprint(parsed)

    model_map, map_meta = load_model_map(
        fingerprint=rec["fingerprint_file"].split(":", 1)[1],
        maps_dir=maps_dir, explicit_path=explicit_map)
    if skip_model_map:
        model_map, map_meta = None, None
        rec["map_used"] = "（冷启动：显式忽略所有侧车映射）"
    if model_map:
        rec["map_used"] = os.path.basename(explicit_map) if explicit_map else "model_material_maps/*"
        # 侧车映射引用不存在的材质 → 资源问题
        known = set()
        for m in parsed["materials"]:
            known.add(m["name"])
            known.add(strip_blender_ext(m["name"]))
        for k in model_map:
            if k not in known:
                rec["resource_issues"].append("侧车映射引用了不存在的材质：%s" % k)
        _valid = set(classifier.rules.class_to_group.keys()) \
            | set(classifier.rules.class_to_group.values()) \
            | {"ignore", "IGNORE", "unresolved", "UNRESOLVED"}
        for k, v in model_map.items():
            val = v.get("group") if isinstance(v, dict) else v
            if val not in _valid:
                rec["resource_issues"].append(
                    "侧车映射里「%s」的分类值非法：%r（可选：%s）"
                    % (k, val, "、".join(sorted(_valid))))

    cls = []
    for m in parsed["materials"]:
        ctx = {"surface_count": m.get("surface_count"),
               "alpha": m.get("alpha"),
               "diffuse_rgb": m.get("diffuse_rgb"),
               "has_texture": bool(m.get("texture")),
               "double_sided": m.get("double_sided")}
        c = classifier.classify(m["name"], model_map=model_map, ctx=ctx)
        d = c.as_dict()
        d.update({
            "index": m["index"],
            "surface_count": m.get("surface_count"),
            "texture": m.get("texture"),
            "diffuse_rgb": m.get("diffuse_rgb"),
            "alpha": m.get("alpha"),
            "double_sided": m.get("double_sided"),
            "has_edge": m.get("has_edge"),
            "memo": m.get("memo"),
        })
        cls.append(d)
        # 必需贴图缺失：材质引用了贴图槽但文件不在
        if m.get("texture"):
            base = os.path.dirname(pmx_path)
            cand = os.path.join(base, m["texture"].replace("\\", os.sep))
            if not os.path.isfile(cand):
                rec["resource_issues"].append("贴图缺失：%s（材质 %s）" % (m["texture"], m["name"]))
    rec["materials"] = cls
    rec["class_options"] = sorted(set(list(classifier.rules.class_to_group.keys())
                                      + ["ignore", "unresolved"]))
    rec["ignore_patterns"] = list(classifier.rules.ignore_patterns)

    # 侧车映射后重新统计（以"用户确认过的结果"为准）
    cls_with_map = []
    for m in parsed["materials"]:
        ctx = {"surface_count": m.get("surface_count"), "alpha": m.get("alpha"),
               "diffuse_rgb": m.get("diffuse_rgb"), "has_texture": bool(m.get("texture"))}
        cls_with_map.append(classifier.classify(m["name"], model_map=model_map, ctx=ctx))
    summ = MaterialClassifier.summarize(cls_with_map)
    rec["summary"] = summ
    rec["unresolved_names"] = summ["unresolved_names"]
    rec["needs_review_names"] = summ["low_confidence_names"]
    rec["would_pass_strict"] = (summ["unresolved"] == 0 and summ["low_confidence"] == 0
                                and not rec["resource_issues"])
    rec["outcome"] = "success" if rec["would_pass_strict"] else "rejected"

    # ---- 冷启动基线（不加载任何侧车映射）--------------------------------------
    #  把"自动分类的真实能力"与"靠人工确认换来的通过"分开报，避免后者虚高覆盖率。
    cls_cold = []
    for m in parsed["materials"]:
        ctx = {"surface_count": m.get("surface_count"), "alpha": m.get("alpha"),
               "diffuse_rgb": m.get("diffuse_rgb"), "has_texture": bool(m.get("texture"))}
        cls_cold.append(classifier.classify(m["name"], model_map=None, ctx=ctx))
    summ_cold = MaterialClassifier.summarize(cls_cold)
    rec["summary_cold"] = summ_cold
    rec["would_pass_strict_cold"] = (summ_cold["unresolved"] == 0
                                     and summ_cold["low_confidence"] == 0
                                     and not rec["resource_issues"])
    # 覆盖率归类（四类互斥且完备）
    if not rec["parse_ok"] or rec["resource_issues"]:
        cc = "unresolved"
    elif rec["would_pass_strict_cold"]:
        cc = "cold_start_auto_resolved"
    elif rec["would_pass_strict"]:
        cc = "resolved_with_model_map"
    else:
        cc = "requires_confirmation"
    rec["coverage_class"] = cc
    return rec


def render_md(rec):
    L = []
    L.append("# 材质预检 · %s" % rec["pmx"])
    L.append("")
    L.append("- 模型名：`%s`" % rec.get("model_name"))
    L.append("- PMX 版本：%s ／ 编码 %s" % (rec.get("version"), rec.get("encoding")))
    L.append("- 材质数：%s（贴图 %s 张，顶点 %s）"
             % (rec.get("material_count"), rec.get("texture_count"), rec.get("vertex_count")))
    L.append("- 文件指纹：`%s`" % rec.get("fingerprint_file"))
    L.append("- 结构指纹：`%s`" % rec.get("fingerprint_structure"))
    L.append("- 侧车映射：%s" % (rec.get("map_used") or "（无）"))
    L.append("- **严格模式结论：%s**" % ("可以通过" if rec["would_pass_strict"] else "会被拒绝"))
    L.append("")
    s = rec.get("summary") or {}
    if s:
        L.append("## 分类汇总")
        L.append("")
        L.append("| 项 | 值 |")
        L.append("|---|---|")
        L.append("| 材质总数 | %s |" % s.get("total"))
        L.append("| 已解析 | %s |" % s.get("resolved"))
        L.append("| 未解决 | %s |" % s.get("unresolved"))
        L.append("| 低置信度 | %s |" % s.get("low_confidence"))
        L.append("| 按设计忽略 | %s |" % s.get("ignored"))
        L.append("| 命中来源 | %s |" % json.dumps(s.get("mapping_source_counts", {}), ensure_ascii=False))
        L.append("")
    if rec["resource_issues"]:
        L.append("## 资源问题")
        L.append("")
        for x in rec["resource_issues"]:
            L.append("- %s" % x)
        L.append("")
    L.append("## 逐材质")
    L.append("")
    L.append("| # | 材质名 | 面数 | 透明度 | 双面 | 贴图 | 分类 | 命中层 | 依据 | 置信度 | 需确认 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for d in rec["materials"]:
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            d["index"], d["original_name"], d["surface_count"], d["alpha"],
            "是" if d["double_sided"] else "否",
            (d["texture"] or "—")[:28],
            d["class"], d["layer"], d["rule"] or "—",
            d["confidence"], "是" if d["needs_review"] else ""))
    L.append("")

    # ---- 待人工确认清单（附判据，便于一次填完侧车映射）
    todo = [d for d in rec["materials"] if d["needs_review"] or d["class"] == "unresolved"]
    if todo:
        L.append("## 待人工确认（%d 项）" % len(todo))
        L.append("")
        L.append("填法：把下面每项的分类写进侧车映射文件的 `assignments`，再带 `--material-map` 重跑。")
        L.append("")
        for d in todo:
            L.append("- **%s**（序号 %s）" % (d["original_name"], d["index"]))
            L.append("  - 面数 `%s`／透明度 `%s`／双面 `%s`／贴图 `%s`"
                     % (d["surface_count"], d["alpha"],
                        "是" if d["double_sided"] else "否", d["texture"] or "—"))
            L.append("  - 贴图色彩倾向：RGB `%s`" % (d["diffuse_rgb"],))
            L.append("  - 自动判断：`%s`（%s，置信度 %s）" % (d["class"], d["matched_by"], d["confidence"]))
            if d.get("note"):
                L.append("  - 判据/提示：%s" % d["note"])
            L.append("  - 可选分类：%s" % "、".join(sorted(rec.get("class_options") or [])))
        L.append("")
    return "\n".join(L)


def classify_reason(rec):
    """把拒绝原因归类成方案要求的五档。"""
    if not rec.get("parse_ok"):
        return "解析失败"
    if any("贴图缺失" in x for x in rec.get("resource_issues", [])):
        return "资源缺失"
    if any("侧车映射" in x for x in rec.get("resource_issues", [])):
        return "侧车映射问题"
    names = rec.get("unresolved_names") or []
    if names and all(re.search(r"^material\d*$", n, re.IGNORECASE) for n in names):
        return "无语义名称"
    if names:
        return "需要人工映射"
    if (rec.get("summary") or {}).get("low_confidence"):
        return "需要人工映射"
    if rec.get("resource_issues"):
        return "资源缺失"
    return "自动通过"


def render_batch_md(recs, coverage=None):
    from collections import Counter
    L = ["# 材质预检 · 批量汇总", "",
         "共 %d 个 PMX。判定口径：严格模式（`--material-policy strict`）下能否直接跑。" % len(recs),
         ""]
    if coverage:
        L += ["## 覆盖率（冷/热启动分开报）", "",
              "运行模式：**%s**" % ("冷启动（忽略所有侧车映射）" if coverage.get("mode") == "cold"
                                else "热启动（允许加载已确认侧车映射）"), "",
              "| 指标 | 模型数 | 含义 |", "|---|---|---|",
              "| `cold_start_auto_resolved` | %d | 不加载任何侧车映射即可通过 —— **这才是自动分类的真实能力** |"
              % coverage.get("cold_start_auto_resolved", 0),
              "| `resolved_with_model_map` | %d | 冷启动不通过，靠**人工确认过的**侧车映射才通过 |"
              % coverage.get("resolved_with_model_map", 0),
              "| `requires_confirmation` | %d | 需要人工确认（表情切换件 / 无语义命名 / 低置信度） |"
              % coverage.get("requires_confirmation", 0),
              "| `unresolved` | %d | 资源缺失或解析失败，确认也解决不了 |"
              % coverage.get("unresolved", 0),
              "| 合计 | %d | |" % coverage.get("total", len(recs)), ""]
    L += ["## 分档统计", "", "| 档位 | 数量 |", "|---|---|"]
    buckets = [(classify_reason(r), r) for r in recs]
    cnt = Counter(b for b, _ in buckets)
    for k in ("自动通过", "需要人工映射", "无语义名称", "资源缺失", "侧车映射问题", "解析失败"):
        if cnt.get(k):
            L.append("| %s | %d |" % (k, cnt[k]))
    L += ["", "## 逐模型", "",
          "| 判定 | 覆盖率归类 | 档位 | PMX | 材质 | 冷启动解析 | 热启动解析 | 未解决 | 低置信 | 待确认材质 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for b, r in buckets:
        s = r.get("summary") or {}
        sc = r.get("summary_cold") or {}
        todo = r.get("unresolved_names") or []
        todo += [n for n in (r.get("needs_review_names") or []) if n not in todo]
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            "可通过" if r.get("would_pass_strict") else "**会拒绝**",
            r.get("coverage_class"), b, r.get("pmx"), r.get("material_count"),
            sc.get("resolved"), s.get("resolved"),
            s.get("unresolved"), s.get("low_confidence"),
            "、".join(todo[:6]) + ("…" if len(todo) > 6 else "") or "—"))
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="PMX 材质预检器 v3")
    ap.add_argument("--pmx")
    ap.add_argument("--batch", help="根目录，递归找 *.pmx")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--rules", default=None)
    ap.add_argument("--maps-dir", default=None)
    ap.add_argument("--material-map", default=None)
    ap.add_argument("--write-map-template", action="store_true")
    ap.add_argument("--no-model-map", action="store_true",
                    help="冷启动：显式忽略所有侧车映射，用于测量自动分类的真实能力")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    clf = MaterialClassifier(a.rules)
    out_dir = a.out_dir or os.path.dirname(os.path.abspath(__file__))

    targets = []
    if a.batch:
        targets = sorted(glob.glob(os.path.join(a.batch, "**", "*.pmx"), recursive=True))
        targets = [t for t in targets if "_审计_通用性" not in t]
    elif a.pmx:
        targets = [a.pmx]
    else:
        ap.error("需要 --pmx 或 --batch")

    os.makedirs(out_dir, exist_ok=True)
    all_recs = []
    for t in targets:
        rec = preflight(t, clf, maps_dir=a.maps_dir, explicit_map=a.material_map,
                        skip_model_map=a.no_model_map)
        all_recs.append(rec)
        if not a.quiet:
            flag = "可通过" if rec["would_pass_strict"] else "会拒绝"
            print("[%s] %-34s 材质%3s 未解决%3d 低置信%2d %s"
                  % (flag, rec["pmx"][:34], rec.get("material_count"),
                     (rec.get("summary") or {}).get("unresolved", 0),
                     (rec.get("summary") or {}).get("low_confidence", 0),
                     (" | " + "; ".join(rec["unresolved_names"][:8])) if rec.get("unresolved_names") else ""))
        if len(targets) == 1:
            jp = os.path.join(out_dir, "material_preflight.json")
            mp = os.path.join(out_dir, "material_preflight.md")
            with open(jp, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
            with open(mp, "w", encoding="utf-8") as f:
                f.write(render_md(rec))
            if not a.quiet:
                print("  → %s" % jp)
                print("  → %s" % mp)
            if a.write_map_template:
                fp = (rec.get("fingerprint_file") or "").split(":", 1)[-1] or "unknown"
                mp_path = map_path_for(None, fp, a.maps_dir)
                entries = [(d["original_name"], "建议 class=%s（%s）" % (d["class"], d["matched_by"]))
                           for d in rec["materials"] if d["needs_review"] or d["class"] == "unresolved"]
                write_map_template(mp_path, entries, {
                    "display_name": rec.get("model_name") or rec["pmx"],
                    "fingerprint": rec.get("fingerprint_file"),
                    "fingerprint_structure": rec.get("fingerprint_structure"),
                    "pmx": rec["pmx"],
                })
                if not a.quiet:
                    print("  → 映射模板 %s（%d 项待确认）" % (mp_path, len(entries)))

    if len(targets) > 1:
        from collections import Counter as _C
        cc = _C(r.get("coverage_class") for r in all_recs)
        coverage = {
            "mode": "cold" if a.no_model_map else "with_map",
            "cold_start_auto_resolved": cc.get("cold_start_auto_resolved", 0),
            "resolved_with_model_map": cc.get("resolved_with_model_map", 0),
            "requires_confirmation": cc.get("requires_confirmation", 0),
            "unresolved": cc.get("unresolved", 0),
            "total": len(all_recs),
        }
        jp = os.path.join(out_dir, "material_preflight_batch.json")
        with open(jp, "w", encoding="utf-8") as f:
            json.dump({"schema": "toon-preflight-batch/1", "count": len(all_recs),
                       "coverage": coverage, "models": all_recs},
                      f, ensure_ascii=False, indent=1)
        mp = os.path.join(out_dir, "material_preflight_batch.md")
        with open(mp, "w", encoding="utf-8") as f:
            f.write(render_batch_md(all_recs, coverage))
        ok = sum(1 for r in all_recs if r["would_pass_strict"])
        print("-" * 78)
        print("批量预检：共 %d 个 PMX（模式：%s）"
              % (len(all_recs), "冷启动·忽略侧车映射" if a.no_model_map else "热启动·允许侧车映射"))
        print("  严格模式可通过        : %d" % ok)
        print("  cold_start_auto_resolved: %d   <- 自动分类的真实能力"
              % coverage["cold_start_auto_resolved"])
        print("  resolved_with_model_map : %d   <- 靠人工确认换来的"
              % coverage["resolved_with_model_map"])
        print("  requires_confirmation   : %d" % coverage["requires_confirmation"])
        print("  unresolved              : %d" % coverage["unresolved"])
        print("→ %s" % jp)
        print("→ %s" % mp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
