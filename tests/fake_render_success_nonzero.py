"""测试替身：token 说 SUCCESS、成品图也有，但**退出码非 0**。

规格 §3.4 要守的「token 与退出码冲突」：进程明明失败了却留下成功标记与图。
必须以 ``RENDER_SUBPROCESS_FAILED`` 收场，绝不交付。
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
    with open(os.path.join(args.out, "result.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n looks-fine")
    with open(os.path.join(args.out, "render.done"), "w", encoding="utf-8") as fh:
        fh.write("SUCCESS\n")
    print("[3/3] 声称成功，但随后崩溃", flush=True)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
