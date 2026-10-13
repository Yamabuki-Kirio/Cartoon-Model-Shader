"""测试替身：退出码 0，但**不产出任何产物**。

用来守「绝不允许仅凭退出码 0 就报成功」这条契约（规格 §11）。
"""

from __future__ import annotations

import argparse


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fingerprint")
    ap.add_argument("--pmx", default=None)
    ap.add_argument("--blend", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="faithful")
    ap.add_argument("--auto-frame", type=int, default=0)
    ap.add_argument("--lighting", default="volume_fog")
    ap.add_argument("--fog-density", type=float, default=0.018)
    ap.add_argument("--glow-threshold", type=float, default=None)
    ap.add_argument("--glow-strength", type=float, default=None)
    ap.add_argument("--glow-size", type=float, default=None)
    ap.add_argument("--glow-smoothness", type=float, default=None)
    ap.add_argument("--glow-quality", default=None)
    ap.add_argument("--runtime-dir", default="")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--model-root", default=None)
    ap.add_argument("--source-blend", default=None)
    ap.add_argument("--blender", default=None)
    ap.add_argument("--maps-dir", default=None)
    args = ap.parse_args()

    print("[1/3] 预检与自动分类…", flush=True)
    print("[2/3] 无需人工确认 —— 直接渲染", flush=True)
    print("  渲染中…", flush=True)
    # 退出码 0，但目录里什么都不写。
    print("[3/3] 完成（输出：%s）" % args.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
