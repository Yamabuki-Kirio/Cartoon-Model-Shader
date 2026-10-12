# -*- coding: utf-8 -*-
"""
扫描：对 1009 下全部 PMX 提取结构特征，重点输出 v3 冷启动未通过的那些材质。
用于判断"26/29 是否可达"以及建立标注集的输入。

用法：python _tools/scan_targets.py [--out reports/features.json]
"""
import argparse
import glob
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pmx_features as PF  # noqa: E402

ROOT = os.environ.get("TOON_MODEL_ROOT", "")
RUNTIME_DIR = os.environ.get(
    "TOON_RUNTIME_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"),
)

# v3 冷启动判定为「需要人工确认 / 无法判断」的材质（来自 reports/cold_start）
TARGETS = {
    "娜娜莉1.0.pmx": ["Emotion1", "Emotion2"],
    "千夏皮肤.pmx": ["新規"],
    "哈尼娅1.1.pmx": ["Emotion1", "Emotion2"],
    "银狼.pmx": ["表情"],
    "小贞.pmx": ["表情"],
    "灵可.pmx": ["表情"],
    "阿芙2.0.pmx": ["口腔", "表情"],
    "汗.pmx": ["汗", "汗2"],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--out", default=os.path.join(
        RUNTIME_DIR, "evidence", "features.json"))
    a = ap.parse_args()
    if not a.root:
        ap.error("需要 --root 或环境变量 TOON_MODEL_ROOT")

    pmxs = sorted(glob.glob(os.path.join(a.root, "**", "*.pmx"), recursive=True))
    all_res = {}
    mat_names = {}          # basename -> {index: name}
    stat = {"total": 0, "ok": 0, "morph_ok": 0, "bones_ok": 0, "failed": []}

    for p in pmxs:
        stat["total"] += 1
        res, model = PF.analyse(p)
        all_res[os.path.basename(p)] = res
        mat_names[os.path.basename(p)] = {m["index"]: m["name"] for m in model.get("materials", [])}
        if res.get("ok"):
            stat["ok"] += 1
            stat["morph_ok"] += 1 if res.get("morph_available") else 0
            stat["bones_ok"] += 1 if res.get("bones_available") else 0
        else:
            stat["failed"].append((os.path.basename(p), res.get("error")))

    out = {"schema": "toon-structural-features/1",
           "root": "<MODEL_ROOT>", "stat": stat, "models": all_res,
           "material_names": mat_names}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("=" * 100)
    print("解析统计：共 %d 个 PMX，ok=%d，morph 可用=%d，骨骼可用=%d"
          % (stat["total"], stat["ok"], stat["morph_ok"], stat["bones_ok"]))
    for n, e in stat["failed"]:
        print("  失败：%s -> %s" % (n, e))

    print()
    print("目标材质的结构特征（v3 冷启动未通过的那些）")
    print("-" * 100)
    print("%-20s %-9s %6s %9s %7s %7s %7s %8s  %s" % (
        "模型", "材质", "tri", "贴图", "高度下", "高度上", "morph率", "最大位移", "被material-morph引用"))
    for fn, names in TARGETS.items():
        res = all_res.get(fn)
        if not res or not res.get("ok"):
            print("%-20s (解析失败)" % fn)
            continue
        feats = res.get("features", {})
        nmap = mat_names.get(fn, {})
        for nm in names:
            idx = [i for i, n in nmap.items() if n == nm]
            if not idx:
                print("%-20s %-9s (材质不存在)" % (fn[:20], nm))
                continue
            hit = feats.get(str(idx[0])) or feats.get(idx[0])
            if not hit:
                continue
            print("%-20s %-9s %6s %9s %7.2f %7.2f %7.3f %8.5f  %s" % (
                fn[:20], nm, hit.get("triangles"),
                (hit.get("texture_name") or "-")[:9],
                hit.get("bbox_rel_bottom", 0), hit.get("bbox_rel_top", 0),
                hit.get("morph_vertex_hit_ratio", 0), hit.get("morph_max_displacement_rel", 0),
                [r["morph"] for r in hit.get("material_morph_refs", [])][:3]))
    print("-" * 100)
    print("→ %s" % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
