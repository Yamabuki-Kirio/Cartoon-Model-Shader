"""测试替身：退出码 0、目录里有零散产物，但 render.done 明确写 FAILED。

用来守「固定脚本的 render.done 是权威终态：它说 FAILED 就绝不能因为退出码 0
或目录里存在图片而判成功」这条契约。
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
    # 有产物，但终态是失败
    with open(os.path.join(args.out, "leftover.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n stray")
    with open(os.path.join(args.out, "render.done"), "w", encoding="utf-8") as fh:
        fh.write("FAILED\n")
    print("[3/3] 失败（阶段：渲染；错误码 SURFACE_APPLY_FAILED） —— 未产出成品", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
