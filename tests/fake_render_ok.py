"""测试用的「固定渲染脚本」替身：模拟开始渲染.py 的对外行为。

它不是真的渲染，只按 main 的契约产出：
* 打印预检 / 直接渲染 / 完成 等阶段性 stdout（供适配器推进状态机）；
* 在 --out 目录里写出 ``render.done``（首行 SUCCESS）与一张产物图片。

适配器只通过 ``--fingerprint/--out/--mode/--auto-frame/--runtime-dir`` 等白名单参数调用它，
因此这里的 argparse 只认这些参数（多一个都不认，用来反向守住白名单）。
"""

from __future__ import annotations

import argparse
import os
import sys


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

    print("[1/3] 预检与自动分类…", flush=True)
    print("  材质 3：语义自动 3 / 映射命中 0 / 需确认 0 / 无依据 0", flush=True)
    print("[2/3] 无需人工确认 —— 直接渲染", flush=True)
    print("  渲染中…", flush=True)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "result.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n fake")
    with open(os.path.join(args.out, "render.done"), "w", encoding="utf-8") as fh:
        fh.write("SUCCESS\n")
    print("[3/3] 完成（输出：%s）" % args.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
