# -*- coding: utf-8 -*-
"""
生成「确认清单」+「自动执行清单」（v3.1）
=====================================================================================
把 语义层 + 结构证据 + 确定性分级 合成两份产出：

  1. reports/confirmation/<指纹>_confirmation.json
     —— 需要用户确认的材质（SUGGESTED / UNRESOLVED）
  2. reports/auto_executed.md / .json
     —— **会被自动执行的材质全清单**（MODEL_MAP + SEMANTIC_CONFIRMED），
        按模型分组并标注命中依据，供人工抽查后再冻结规则

分级口径见 decision.py：
  MODEL_MAP / SEMANTIC_CONFIRMED  → 不询问，直接执行（前者的来源是用户确认，后者是明确语义规则）
  SUGGESTED / UNRESOLVED          → 必须确认

用法：
  python _tools/build_confirmation.py
  python _tools/build_confirmation.py --only-requiring-user
"""
import argparse
import glob
import hashlib
import io
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
V31 = os.path.dirname(HERE)
sys.path.insert(0, V31)

import pmx_features as PF                       # noqa: E402
import structure_classifier as SC               # noqa: E402
import decision as DEC                          # noqa: E402
from material_classifier import MaterialClassifier, load_model_map, default_maps_dir  # noqa: E402

ROOT = os.environ.get("TOON_MODEL_ROOT", "")
RUNTIME_DIR = os.environ.get("TOON_RUNTIME_DIR", os.path.join(V31, "runtime"))
OUT = os.path.join(RUNTIME_DIR, "confirmation")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def build_one(pmx_path, clf, maps_dir, reverse_tested=()):
    fp = sha256_of(pmx_path)
    model = PF.load_pmx(pmx_path)
    if not model.get("ok"):
        return None
    feats = PF.compute_features(model)
    name_of = {m["index"]: m["name"] for m in model["materials"]}
    mat_of = {m["index"]: m for m in model["materials"]}

    model_map, _ = load_model_map(fingerprint=fp, maps_dir=maps_dir)

    # 语义层（冷启动视角：不带映射）
    sem = {}
    for i, nm in name_of.items():
        ctx = {"surface_count": mat_of[i]["surface_count"],
               "alpha": mat_of[i]["diffuse"][3],
               "diffuse_rgb": mat_of[i]["diffuse"][:3],
               "has_texture": bool(mat_of[i]["texture"])}
        sem[i] = clf.classify(nm, model_map=None, ctx=ctx)

    name_feats = {name_of[i]: feats.get(i) for i in name_of}
    name_class = {name_of[i]: (sem[i].klass, sem[i].confidence)
                  for i in sem
                  if sem[i].klass != "unresolved" and not sem[i].needs_review
                  and sem[i].confidence >= 0.85}
    peer_idx = SC.build_peer_index(name_feats, name_class)

    items = []
    for i, nm in name_of.items():
        f = feats.get(i) or {}
        ev = SC.evidence_of(f, mat_of[i], {})
        peers = [x for x in peer_idx.get(f.get("texture_name"), []) if x[0] != nm]
        sc = SC.classify_by_structure(ev, peers)
        cands = [(k, "；".join(rs)) for k, _conf, rs in sc["candidates"]]

        mm_hit = bool(model_map and nm in model_map)
        mm_val = (model_map or {}).get(nm, {}).get("group") if mm_hit else None

        d = DEC.decide(nm, ev, semantic=sem[i], model_map_hit=mm_hit,
                       model_map_value=mm_val, candidates=cands, notes=sc["notes"],
                       reverse_tested_rules=reverse_tested)

        items.append({
            "material": nm,
            "index": i,
            "stage": d["stage"],
            "proposed_class": d["proposed_class"],
            "structure_rules": d["structure_rules"],
            "reasons": d["reasons"],
            "candidates": [{"class": c, "why": w} for c, w in d["candidates"]],
            "requires_user": bool(d["requires_user"]),
            "auto_source": d["auto_source"],
            "semantic": {"class": sem[i].klass, "matched_by": sem[i].matched_by,
                         "rule": sem[i].rule},
            "evidence": ev,
            "previews": {
                "mask_only": "previews/%s__%d__mask.png" % (model["file"], i),
                "overview_highlighted": "previews/%s__%d__overview.png" % (model["file"], i),
                "proposed_group": "previews/%s__%d__proposed.png" % (model["file"], i),
            },
            "choices": [{"class": c, "label": lb} for c, lb in DEC.CHOICES],
        })

    requiring = [x for x in items if x["requires_user"]]
    auto = [x for x in items if not x["requires_user"]]
    return {
        "schema": "toon-confirmation/1",
        "model": {
            "file": model["file"], "path": pmx_path,
            "display_name": model.get("model_name"),
            "fingerprint": "sha256:" + fp,
            "morph_available": model.get("morph_available"),
        },
        "summary_all": DEC.summarize([x["stage"] for x in items]),
        "summary_requiring_user": DEC.summarize([x["stage"] for x in requiring]),
        "items": items,
        "_auto": auto,
        "_requiring": requiring,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-requiring-user", action="store_true")
    ap.add_argument("--pmx", default=None)
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--out", default=OUT)
    # ★ F6：映射目录必须可由外部贯穿；默认=用户数据目录（不是仓库内的 model_material_maps/）
    ap.add_argument("--maps-dir", default=None)
    a = ap.parse_args()
    if not a.pmx and not a.root:
        ap.error("批量模式需要 --root 或环境变量 TOON_MODEL_ROOT")

    clf = MaterialClassifier()
    maps_dir = os.path.abspath(a.maps_dir) if a.maps_dir else (
        os.environ.get("TOON_MAPS_DIR") or default_maps_dir())
    out_dir = os.path.abspath(a.out)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "previews"), exist_ok=True)

    targets = [a.pmx] if a.pmx else sorted(
        glob.glob(os.path.join(a.root, "**", "*.pmx"), recursive=True))

    index, auto_all = [], []
    for p in targets:
        payload = build_one(p, clf, maps_dir)
        if not payload:
            continue
        fp = payload["model"]["fingerprint"].split(":", 1)[1]
        jp = os.path.join(out_dir, "%s_confirmation.json" % fp[:16])
        with io.open(jp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)

        s = payload["summary_all"]
        index.append({"model": payload["model"]["file"], "file": os.path.basename(jp),
                      "fingerprint": payload["model"]["fingerprint"],
                      "summary": s})
        if payload["_requiring"]:
            sr = payload["summary_requiring_user"]
            print("%-22s 待确认 %2d（SUGGESTED %d / UNRESOLVED %d）｜自动 %d（语义 %d + 映射 %d）"
                  % (payload["model"]["file"][:22], sr["requires_user"],
                     sr["structure_suggested"], sr["unresolved"],
                     s["auto_executable"], s["semantic_auto_confirmed"], s["model_map_confirmed"]))
        for x in payload["_auto"]:
            auto_all.append({"model": payload["model"]["file"],
                             "fingerprint": payload["model"]["fingerprint"],
                             "material": x["material"], "index": x["index"],
                             "class": x["proposed_class"], "stage": x["stage"],
                             "source": x["auto_source"],
                             "matched_by": x["semantic"]["matched_by"],
                             "rule": x["semantic"]["rule"]})

    # ---- 全量统计
    tot = {"model_map_confirmed": 0, "semantic_auto_confirmed": 0,
           "structure_suggested": 0, "unresolved": 0, "total": 0}
    for x in index:
        for k in tot:
            tot[k] += x["summary"].get(k, 0)

    with io.open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": "toon-confirmation-index/1", "metrics": tot,
                   "models": index}, f, ensure_ascii=False, indent=1)

    # ---- 自动执行清单（供人工抽查）
    with io.open(os.path.join(out_dir, "auto_executed.json"), "w",
                 encoding="utf-8") as f:
        json.dump({"schema": "toon-auto-executed/1", "count": len(auto_all),
                   "entries": auto_all}, f, ensure_ascii=False, indent=1)
    L = ["# 会被自动执行的材质清单（供抽查）", "",
         "> 这些材质**不会**弹确认页，会直接进入渲染。规则冻结前请抽查本清单。", "",
         "自动执行只有两个来源：",
         "1. `semantic_rule` —— 明确语义规则命中（v3 的别名/语义层）",
         "2. `model_map` —— 用户以前确认过的侧车映射", "",
         "**结构规则 R1/R2 不在其中** —— 它们已经降级为 `SUGGESTED`（必须确认）。", "",
         "| 模型 | 材质 | 类别 | 来源 | 命中方式 | 依据 |", "|---|---|---|---|---|---|"]
    for e in auto_all:
        L.append("| %s | %s | %s | %s | %s | `%s` |"
                 % (e["model"], e["material"], e["class"] or "—", e["source"],
                    e["matched_by"], e["rule"] or "—"))
    src_cnt = Counter(e["source"] for e in auto_all)
    L += ["", "## 按来源统计", "", "| 来源 | 数量 |", "|---|---|"]
    for k, v in src_cnt.most_common():
        L.append("| %s | %d |" % (k, v))
    with io.open(os.path.join(out_dir, "auto_executed.md"), "w",
                 encoding="utf-8") as f:
        f.write("\n".join(L))

    print("-" * 78)
    print("全量统计（口径已按新分级）：")
    print("  semantic_auto_confirmed : %d" % tot["semantic_auto_confirmed"])
    print("  model_map_confirmed     : %d" % tot["model_map_confirmed"])
    print("  structure_suggested     : %d   <- 含 R1/R2 命中" % tot["structure_suggested"])
    print("  unresolved              : %d" % tot["unresolved"])
    print("  ── 自动执行合计 %d / 材质总数 %d；需确认 %d"
          % (tot["semantic_auto_confirmed"] + tot["model_map_confirmed"],
             tot["total"], tot["structure_suggested"] + tot["unresolved"]))
    print()
    print("自动执行清单 → %s" % os.path.join(out_dir, "auto_executed.md"))
    print("确认清单目录 → %s" % out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
