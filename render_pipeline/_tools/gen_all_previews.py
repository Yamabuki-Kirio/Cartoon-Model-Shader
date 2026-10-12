# -*- coding: utf-8 -*-
"""
批量生成预览图（v3.1）
=====================================================================================
遍历 reports/confirmation/*.json，对每个"需要用户确认"的模型：
  1. 用【文件指纹】反查 PMX 真实路径（绝不手写路径）
  2. 通过常驻 Blender 的 9876 通道派发 preview_gen.py
  3. 轮询 --done 标记（不用固定 sleep）

用法：
  python _tools/gen_all_previews.py            # 全部
  python _tools/gen_all_previews.py --force    # 重生成已有的
"""
import argparse
import glob
import hashlib
import io
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
V31 = os.path.dirname(HERE)
ROOT = os.environ.get("TOON_MODEL_ROOT", "")
BLENDER = os.environ.get("TOON_BLENDER", "")
SOURCE_BLEND = os.environ.get("TOON_SRC_BLEND", "")
RUNTIME_DIR = os.environ.get("TOON_RUNTIME_DIR", os.path.join(V31, "runtime"))
CONF_DIR = os.path.join(RUNTIME_DIR, "confirmation")
PREVIEW_DIR = os.path.join(CONF_DIR, "previews")
TIMEOUT = 600


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def locate_by_fingerprint(fp, root=ROOT):
    """按指纹前缀反查 PMX 路径 —— 唯一允许的定位方式。"""
    for r, _ds, fs in os.walk(root):
        for f in sorted(fs):
            if not f.lower().endswith(".pmx"):
                continue
            p = os.path.join(r, f)
            try:
                if sha256_of(p).startswith(fp):
                    return p
            except Exception:
                continue
    return None


def send(cmd, timeout=60):
    s = socket.create_connection(("127.0.0.1", 9876), timeout=10)
    s.settimeout(timeout)
    s.sendall((json.dumps(cmd) + "\n").encode("utf-8"))
    buf = b""
    while True:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            s.close()
            return {"__error__": "timeout"}
        if not chunk:
            break
        buf += chunk
        try:
            return json.loads(buf.decode("utf-8"))
        except json.JSONDecodeError:
            continue
    try:
        return json.loads(buf.decode("utf-8"))
    except Exception:
        return {"__raw__": buf.decode("utf-8", "replace")}


def launch(pmx, conf, done, logp):
    argv = [BLENDER, "--background", "--factory-startup", "--python",
            os.path.join(HERE, "preview_gen.py"), "--",
            "--pmx", pmx, "--confirmation", conf, "--out", PREVIEW_DIR,
            "--resolve", SOURCE_BLEND,
            "--log", logp, "--done", done]
    code = ("import subprocess, json\n"
            "p = subprocess.Popen(%r, stdout=open(%r,'w',encoding='utf-8',errors='replace'),"
            " stderr=subprocess.STDOUT)\n"
            "print('@@LAUNCH@@' + json.dumps({'pid': p.pid}))\n" % (argv, logp))
    return send({"type": "execute_code", "params": {"code": code}}, timeout=60)


def main():
    global ROOT, BLENDER, SOURCE_BLEND
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--blender", default=BLENDER)
    ap.add_argument("--source-blend", default=SOURCE_BLEND)
    a = ap.parse_args()
    ROOT, BLENDER, SOURCE_BLEND = a.root, a.blender, a.source_blend
    if not ROOT or not BLENDER or not SOURCE_BLEND:
        ap.error("需要 --root、--blender、--source-blend（或对应 TOON_* 环境变量）")

    os.makedirs(PREVIEW_DIR, exist_ok=True)
    jobs = []
    for jp in sorted(glob.glob(os.path.join(CONF_DIR, "*_confirmation.json"))):
        with io.open(jp, encoding="utf-8") as f:
            conf = json.load(f)
        req = [x for x in conf["items"] if x.get("requires_user")]
        if not req:
            continue
        fp = conf["model"]["fingerprint"].split(":", 1)[1]
        expect = []
        for x in req:
            for k in ("mask_only", "overview_highlighted", "proposed_group"):
                expect.append(os.path.join(PREVIEW_DIR, os.path.basename(x["previews"][k])))
        missing = [p for p in expect if not os.path.isfile(p)]
        if not missing and not a.force:
            print("跳过 %-22s（%d 张预览图已存在）" % (conf["model"]["file"], len(expect)))
            continue
        jobs.append({"conf_path": jp, "conf": conf, "fp": fp, "missing": len(missing)})

    if not jobs:
        print("没有需要生成预览图的模型")
        return 0

    print("待生成 %d 个模型" % len(jobs))
    for j in jobs:
        label = j["conf"]["model"]["file"]
        pmx = locate_by_fingerprint(j["fp"])
        if not pmx:
            print("  !! %-22s 按指纹 %s 找不到 PMX" % (label, j["fp"][:16]))
            continue
        done = os.path.join(HERE, "_preview_run.done")
        logp = os.path.join(PREVIEW_DIR, "_%s.log" % j["fp"][:8])
        for p in (done, logp):
            try:
                os.remove(p)
            except Exception:
                pass
        r = launch(pmx, j["conf_path"], done, logp)
        if "__error__" in r or "@@LAUNCH@@" not in json.dumps(r):
            print("  !! %-22s 派发失败：%s" % (label, str(r)[:120]))
            continue
        print("  → %-22s 已派发（%s）" % (label, os.path.basename(pmx)))
        t0 = time.time()
        while time.time() - t0 < TIMEOUT:
            if os.path.isfile(done):
                break
            time.sleep(1.5)
        st = open(done, encoding="utf-8").read().strip() if os.path.isfile(done) else "(超时)"
        print("     %s  用时 %.0fs" % (st, time.time() - t0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
