"""测试替身：token 说 SUCCESS，但**没有产出任何成品图**。

规格 §3.3：「不得以文件存在或退出码 0 单独判断成功」的反面 ——
反过来也一样：token 说成功但事实不足，同样不能交付。必须判
``RENDER_OUTPUT_MISSING``。
"""

from __future__ import annotations

import argparse
import os


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fingerprint")
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

    os.makedirs(args.out, exist_ok=True)
    # 只有终态标记和一个非图片文件，没有成品图
    with open(os.path.join(args.out, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("声称成功但没有图")
    with open(os.path.join(args.out, "render.done"), "w", encoding="utf-8") as fh:
        fh.write("SUCCESS\n")
    print("[3/3] 完成（但没写图）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
