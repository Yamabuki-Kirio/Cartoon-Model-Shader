"""测试替身：固定脚本终态 = REJECTED（渲染前被材质准入拦下）。

规格 §3.2 第二行：``REJECTED`` 必须映射为 Web 状态 ``rejected``、
``shippable=false``，页面显示「需要材质确认」而不是「渲染失败」。

这里刻意**退出码 0** 且**留一个零散文件**：旧实现会因为它「没报错」就归为失败，
但更糟的是无法与真正的 FAILED 区分；新实现必须给出 rejected。
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
    # 准入阶段写下的确认清单残留（不是成品）
    with open(os.path.join(args.out, "confirmation.json"), "w", encoding="utf-8") as fh:
        fh.write('{"items": []}')
    with open(os.path.join(args.out, "render.done"), "w", encoding="utf-8") as fh:
        fh.write("REJECTED\n")
    print("  材质需要确认：有 2 个材质无法自动判定", flush=True)
    print("[3/3] 已拒绝（渲染前拦下，未出图）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
