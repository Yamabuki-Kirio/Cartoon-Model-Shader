"""测试替身：**不写 render.done**，退出码 0 且有成品图。

规格 §3.2 第五行：兼容旧入口 —— 判 success，但必须**记录降级判定**
（``degraded=true``），让用户知道这不是完整终态契约给出的结论。
"""

from __future__ import annotations

import argparse
import os


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fingerprint")
    ap.add_argument("--pmx", default=None)
    ap.add_argument("--blend", default=None)
    ap.add_argument("--material-policy", default="strict")
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
    with open(os.path.join(args.out, "result.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n legacy-entry")
    print("[3/3] 完成（旧版入口，未写 render.done）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
