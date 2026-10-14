"""测试替身：真实模拟入口脚本的 **PID 上报契约**。

不调用 `_observe_line` 做手工模拟，而是真的：
1. 起一个长驻子进程（模拟「MCP 派发的真实 Blender」）；
2. 打印 ``@@BLENDER_PID@@{"pid": N}`` 到自己的 stdout（与固定脚本一致）；
3. 有 ``--fail-pid`` 时打印 ``@@BLENDER_PID@@{"pid": null}``（模拟取不到 PID）。

行为由环境变量控制：
* ``TOON_FAKE_HOLD``（秒，默认 0）：写出 ``render.done=SUCCESS`` 后是否继续挂住。
  0 表示立刻正常结束（用于验证「完成后不残留 PID」）。
  >0 表示写完后继续 sleep（用于验证「取消能终止整棵树」）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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
    ap.add_argument("--fail-pid", type=int, default=0)
    ap.add_argument("--child-seconds", type=float, default=0.0)
    args = ap.parse_args()

    hold = float(os.environ.get("TOON_FAKE_HOLD", "0") or "0")

    print("[1/3] 预检与自动分类…", flush=True)
    print("[2/3] 无需人工确认 —— 直接渲染", flush=True)
    print("  渲染中…", flush=True)

    # 模拟「常驻 Blender 经 MCP 派发」出来的真实 Blender：一个子进程。
    # 默认存活时间取 child-seconds / hold 的较大者，保证取消测试期间它仍在跑。
    child_life = max(args.child_seconds, hold, 1.0)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(%f)" % child_life]
    )
    if args.fail_pid:
        print("@@BLENDER_PID@@" + json.dumps({"pid": None}), flush=True)
        child.terminate()
        return 1

    print("@@BLENDER_PID@@" + json.dumps({"pid": child.pid}), flush=True)

    if hold > 0:
        # 挂住（模拟渲染进行中），直到被杀。
        try:
            time.sleep(hold)
        finally:
            try:
                child.terminate()
            except Exception:
                pass
        return 0

    # 正常结束：写产物 + 终态标记（子进程在 finally 里收掉）。
    try:
        os.makedirs(os.path.join(args.out, "artifacts"), exist_ok=True)
        with open(os.path.join(args.out, "artifacts", "result.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n fake")
        with open(os.path.join(args.out, "render.done"), "w", encoding="utf-8") as fh:
            fh.write("SUCCESS\n")
        print("[3/3] 完成（输出：%s）" % args.out, flush=True)
    finally:
        try:
            child.terminate()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
