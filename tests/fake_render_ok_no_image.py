"""测试替身：**不写 render.done**、退出码 0，但**没有任何成品图**。

规格 §3.2 第六行：缺失且无合法产物 ⇒ ``failed`` + ``RENDER_OUTPUT_MISSING``。
这里只留一个非图片文件，确保「有文件」不等于「有产物」。
"""

from __future__ import annotations

import argparse
import os


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fingerprint")
    ap.add_argument("--pmx", default=None)
    ap.add_argument("--blend", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="faithful")
    ap.add_argument("--auto-frame", type=int, default=0)
    ap.add_argument("--lighting", default="volume_fog")
    ap.add_argument("--scheme", default="volume_fog")
    ap.add_argument("--scheme-param", action="append", default=None)
    ap.add_argument("--scheme-params", default=None)
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
    with open(os.path.join(args.out, "empty.log"), "w", encoding="utf-8") as fh:
        fh.write("什么也没产出")
    print("[3/3] 结束（无产物）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
