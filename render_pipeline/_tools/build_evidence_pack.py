# -*- coding: utf-8 -*-
"""
生成「待确认材质证据包」（v3.1）
=====================================================================================
对冷启动未通过的模型，把每个未解决材质的：
  * v3 语义层判定
  * 结构证据（morph 驱动率 / 位移 / 高度 / 骨骼 / 贴图同伴）
  * 候选类别 + 置信度 + 逐条判据
整理成一份可读清单，供**一次性人工确认**（也是标定集的来源）。

产出：
  reports/evidence_pack.json   机器可读
  reports/证据包.md             人可读（确认界面雏形）

用法：
  python _tools/build_evidence_pack.py
"""
import glob
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
V31 = os.path.dirname(HERE)
sys.path.insert(0, V31)

import pmx_features as PF                       # noqa: E402
import structure_classifier as SC               # noqa: E402
from material_classifier import MaterialClassifier  # noqa: E402

ROOT = os.environ.get("TOON_MODEL_ROOT", "")
RUNTIME_DIR = os.environ.get("TOON_RUNTIME_DIR", os.path.join(V31, "runtime"))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--out-dir", default=os.path.join(RUNTIME_DIR, "evidence"))
    a = ap.parse_args()
    if not a.root:
        ap.error("需要 --root 或环境变量 TOON_MODEL_ROOT")
    clf = MaterialClassifier()
    pmxs = sorted(glob.glob(os.path.join(a.root, "**", "*.pmx"), recursive=True))

    models = {}
    for p in pmxs:
        model = PF.load_pmx(p)
        if not model.get("ok"):
            continue
        feats = PF.compute_features(model)
        name_of = {m["index"]: m["name"] for m in model["materials"]}
        mat_of = {m["index"]: m for m in model["materials"]}

        # 1) v3 语义层先分类（冷启动，不加载侧车映射）
        sem = {}
        for i, nm in name_of.items():
            ctx = {"surface_count": mat_of[i]["surface_count"],
                   "alpha": mat_of[i]["diffuse"][3],
                   "diffuse_rgb": mat_of[i]["diffuse"][:3],
                   "has_texture": bool(mat_of[i]["texture"])}
            sem[i] = clf.classify(nm, model_map=None, ctx=ctx)

        # 2) 用「已可信分类」的材质建贴图同伴索引
        name_feats = {name_of[i]: feats.get(i) for i in name_of}
        name_class = {}
        for i, c in sem.items():
            if c.klass not in ("unresolved",) and not c.needs_review \
                    and c.confidence >= 0.85:
                name_class[name_of[i]] = (c.klass, c.confidence)
        peer_idx = SC.build_peer_index(name_feats, name_class)

        # 3) 对未解决的材质出结构候选
        entries = []
        for i, nm in name_of.items():
            c = sem[i]
            if c.klass != "unresolved" and not c.needs_review:
                continue
            f = feats.get(i) or {}
            ev = SC.evidence_of(f, mat_of[i], {})
            peers = [x for x in peer_idx.get(f.get("texture_name"), []) if x[0] != nm]
            res = SC.classify_by_structure(ev, peers)
            entries.append({
                "material": nm,
                "index": i,
                "v3_class": c.klass,
                "v3_matched_by": c.matched_by,
                "v3_rule": c.rule,
                "v3_note": c.note,
                "evidence": ev,
                "candidates": [{"class": k, "confidence": cf, "reasons": rs}
                               for k, cf, rs in res["candidates"]],
                "abstain": res["abstain"],
                "notes": res["notes"],
                "peers": [{"material": a, "class": b, "confidence": c3} for a, b, c3 in peers][:6],
            })
        if entries:
            models[model["file"]] = {
                "file": model["file"], "path": p,
                "model_name": model.get("model_name"),
                "morph_available": model.get("morph_available"),
                "entries": entries,
            }

    payload = {"schema": "toon-evidence-pack/1", "root": "<MODEL_ROOT>",
               "model_count": len(models), "models": models}
    os.makedirs(a.out_dir, exist_ok=True)
    jp = os.path.join(a.out_dir, "evidence_pack.json")
    with io.open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    # ---- 人可读
    L = ["# 待确认材质 · 结构证据包", "",
         "对冷启动未通过的材质，逐条给出**结构证据**与**候选类别**。",
         "每条候选都附带依据；给不出候选的一律标 `abstain`（不硬猜）。", ""]
    tot = solved = abst = 0
    for fn, m in models.items():
        L += ["## %s（%s）" % (fn, m.get("model_name")), ""]
        for e in m["entries"]:
            tot += 1
            ev = e["evidence"]
            L.append("### %s" % e["material"])
            L.append("")
            L.append("| 项 | 值 |")
            L.append("|---|---|")
            L.append("| v3 语义层 | `%s`（%s）%s |"
                     % (e["v3_class"], e["v3_matched_by"],
                        (" 提示：" + (e["v3_note"] or "")) if e.get("v3_note") else ""))
            L.append("| 三角数 / alpha | %s / %s |" % (ev["triangles"], ev["alpha"]))
            L.append("| 贴图 | `%s` |" % ev["texture"])
            L.append("| 高度中心（0=脚 1=头顶） | %s |" % ev["height_center"])
            L.append("| 主要骨骼 | %s |" % "、".join(ev["top_bones"]))
            L.append("| morph 顶点驱动率 | %s |" % ev["morph_vertex_ratio"])
            L.append("| morph 最大位移（×对角线） | %s |" % ev["morph_max_displacement"])
            L.append("| 被 material morph 引用 | %s |" % (ev["material_morph_refs"] or "—"))
            if e["peers"]:
                L.append("| 贴图同伴 | %s |"
                         % "、".join("%s(%s %.2f)" % (p["material"], p["class"], p["confidence"])
                                     for p in e["peers"]))
            L.append("")
            if e["candidates"]:
                solved += 1
                L.append("**候选**：")
                for cnd in e["candidates"]:
                    L.append("- `%s`　置信度 **%.2f**" % (cnd["class"], cnd["confidence"]))
                    for r in cnd["reasons"]:
                        L.append("  - %s" % r)
            else:
                abst += 1
                L.append("**给不出候选（abstain）** —— 结构证据不足以定类。")
            for n in e["notes"]:
                L.append("- 旁证：%s" % n)
            L.append("")
    L += ["---", "", "## 小计", "",
          "| 项 | 数量 |", "|---|---|",
          "| 待确认材质总数 | %d |" % tot,
          "| 结构层给出候选 | %d |" % solved,
          "| 结构层仍无法判断 | %d |" % abst, ""]
    mp = os.path.join(a.out_dir, "证据包.md")
    with io.open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    print("待确认材质 %d 个：结构层给出候选 %d，仍无法判断 %d" % (tot, solved, abst))
    print("→ %s" % jp)
    print("→ %s" % mp)
    for fn, m in models.items():
        for e in m["entries"]:
            best = e["candidates"][0] if e["candidates"] else None
            print("  %-16s %-10s -> %s" % (
                fn[:16], e["material"],
                ("%s (%.2f)" % (best["class"], best["confidence"])) if best else "abstain"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
