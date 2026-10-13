"""测试替身：固定脚本写出**四个正式终态之外**的 token。

规格 §3.3：「未知终态 token 一律失败」。这里写 ``WEIRD_OUTCOME`` 且退出码 0、
目录里还有一张成品图 —— 用来守「契约不认识的话必须被单独标成
``RENDER_UNKNOWN_TERMINAL``，而不是被静默归成普通 FAILED」。
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
        fh.write(b"\x89PNG\r\n\x1a\n stray-but-real-image")
    with open(os.path.join(args.out, "render.done"), "w", encoding="utf-8") as fh:
        fh.write("WEIRD_OUTCOME\n")
    print("[3/3] 奇怪的终态标记", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
