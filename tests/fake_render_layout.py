"""测试替身：按分类布局产出 artifacts/ logs/ work/，用于验证资源治理。

* 成功：artifacts/ 放最终图，logs/ 放日志，work/ 放中间大文件（应被清理）。
* 失败（--fail）：work/ 放假大文件，artifacts/ 不产出，logs/ 留摘要。
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
    ap.add_argument("--fail", type=int, default=0)
    args = ap.parse_args()

    out = args.out
    for sub in ("artifacts", "logs", "work"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)

    print("[1/3] 预检与自动分类…", flush=True)
    print("[2/3] 无需人工确认 —— 直接渲染", flush=True)
    print("  渲染中…", flush=True)

    # 中间大文件（无论成败都会写，用于验证清理）
    with open(os.path.join(out, "work", "intermediate.exr"), "wb") as fh:
        fh.write(b"\0" * (256 * 1024))

    with open(os.path.join(out, "logs", "render.log"), "w", encoding="utf-8") as fh:
        fh.write("render log line\n")

    if args.fail:
        with open(os.path.join(out, "render.done"), "w", encoding="utf-8") as fh:
            fh.write("FAILED\n")
        print("[3/3] 失败（阶段：渲染；错误码 SURFACE_APPLY_FAILED） —— 未产出成品", flush=True)
        return 0

    with open(os.path.join(out, "artifacts", "result.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n final")
    with open(os.path.join(out, "render.done"), "w", encoding="utf-8") as fh:
        fh.write("SUCCESS\n")
    print("[3/3] 完成（输出：%s）" % out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
