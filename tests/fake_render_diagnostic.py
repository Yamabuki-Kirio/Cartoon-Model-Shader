"""测试替身：固定脚本终态 = DIAGNOSTIC（只出诊断图，不是成品）。

规格 §3.2 第三行：``DIAGNOSTIC`` 必须映射为 Web 状态 ``diagnostic``、
``shippable=false``，页面明确「诊断图，不可交付」。

这里刻意**退产出带水印的诊断 PNG**：文件确实存在、也确实是一张合法图片，
因此「有图即成功」的旧逻辑会把它当成品交付 —— 正是本次要堵住的洞。
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
    ap.add_argument("--runtime-dir", default="")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--model-root", default=None)
    ap.add_argument("--source-blend", default=None)
    ap.add_argument("--blender", default=None)
    ap.add_argument("--maps-dir", default=None)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    # 洋红棋盘诊断图：带「诊断预览」标识，**不是成品**
    with open(os.path.join(args.out, "diagnostic.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n DIAGNOSTIC-PREVIEW magenta-checkerboard")
    with open(os.path.join(args.out, "render.done"), "w", encoding="utf-8") as fh:
        fh.write("DIAGNOSTIC\n")
    print("[3/3] 诊断图已产出（带诊断预览标识）—— 不是成品", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
