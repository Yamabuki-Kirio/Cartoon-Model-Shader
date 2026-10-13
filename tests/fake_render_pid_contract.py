"""测试替身：真实模拟入口脚本的 **PID 上报契约**。

不调用 `_observe_line` 做手工模拟，而是真的：
1. 起一个长驻子进程（模拟「MCP 派发的真实 Blender」）；
2. 打印 ``@@BLENDER_PID@@{"pid": N}`` 到自己的 stdout（与固定脚本一致）；
3. 有 ``--fail-pid`` 时打印 ``@@BLENDER_PID@@{"pid": null}``（模拟取不到 PID）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time


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
    ap.add_argument("--fail-pid", type=int, default=0)
    ap.add_argument("--child-seconds", type=float, default=30.0)
    args = ap.parse_args()

    print("[1/3] 预检与自动分类…", flush=True)
    print("[2/3] 无需人工确认 —— 直接渲染", flush=True)
    print("  渲染中…", flush=True)

    # 模拟「常驻 Blender 经 MCP 派发」出来的真实 Blender：一个长驻子进程。
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(%f)" % args.child_seconds]
    )
    if args.fail_pid:
        # 契约：取不到 PID 时必须显式上报 null，供调用方告警。
        print("@@BLENDER_PID@@" + json.dumps({"pid": None}), flush=True)
        child.terminate()
        return 1

    print("@@BLENDER_PID@@" + json.dumps({"pid": child.pid}), flush=True)

    # 一直等（模拟渲染进行中），直到被杀。
    try:
        time.sleep(args.child_seconds)
    finally:
        try:
            child.terminate()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
