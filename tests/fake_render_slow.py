"""测试替身：长时间运行、直到被杀掉才结束的假渲染。

用来验证服务关闭时会**终止**仍在渲染的子进程（避免孤儿进程 + 锁被误释放）。
"""

from __future__ import annotations

import argparse
import time


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
    ap.parse_args()

    print("[1/3] 预检与自动分类…", flush=True)
    print("[2/3] 无需人工确认 —— 直接渲染", flush=True)
    print("  渲染中…", flush=True)
    # 一直睡到被杀。
    for _ in range(600):
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
