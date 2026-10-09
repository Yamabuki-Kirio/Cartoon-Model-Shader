# -*- coding: utf-8 -*-
"""
按指纹定位 PMX（v3.1）
=====================================================================================
**唯一目的：让任何人都不用再手写模型路径。**

踩过三次同一个坑了：
  阿芙2.0 / 银狼 / 哈尼娅1.1 的 PMX 都放在**与包同名的子目录**里
  （…_by_神帝宇_fe97…/白银之城—阿芙2.0/阿芙2.0.pmx），
  手写路径时少写一层，就会被误判成"路径含特殊字符"或"文件不存在"。

所以：凡是要用模型路径的地方，一律用本模块按【文件指纹】反查，禁止手写。

用法：
  python _tools/_locate.py --fingerprint 0dda8805            # 前缀匹配
  python _tools/_locate.py --name 哈尼娅1.1.pmx
  python _tools/_locate.py --list                            # 列出全部（基名 + 指纹 + 真实路径）
"""
import argparse
import hashlib
import io
import os
import sys

ROOT = os.environ.get("TOON_MODEL_ROOT", "")


def sha256_of(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def iter_pmx(root=ROOT):
    if not root:
        return
    for r, _ds, fs in os.walk(root):
        for f in sorted(fs):
            if f.lower().endswith(".pmx"):
                yield os.path.join(r, f)


def list_all(root=ROOT):
    out = []
    for p in iter_pmx(root):
        try:
            fp = sha256_of(p)
        except Exception:
            fp = ""
        out.append({"name": os.path.basename(p), "path": p, "fingerprint": fp})
    return out


def locate(fingerprint=None, name=None, root=ROOT):
    """按指纹前缀（推荐）或文件名定位，返回 (path, fingerprint) 或 (None, None)。"""
    for rec in list_all(root):
        if fingerprint and rec["fingerprint"].startswith(fingerprint):
            return rec["path"], rec["fingerprint"]
        if name and rec["name"] == name:
            return rec["path"], rec["fingerprint"]
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fingerprint")
    ap.add_argument("--name")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--root", default=ROOT)
    a = ap.parse_args()
    if not a.root:
        ap.error("需要 --root 或环境变量 TOON_MODEL_ROOT")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    if a.list:
        recs = list_all(a.root)
        print("共 %d 个 PMX（基名 / 指纹前16 / 真实相对路径）" % len(recs))
        for r in recs:
            rel = os.path.relpath(r["path"], a.root)
            print("  %-22s %-16s %s" % (r["name"], r["fingerprint"][:16], rel))
        return 0

    p, fp = locate(a.fingerprint, a.name, a.root)
    if not p:
        print("未找到（fingerprint=%s name=%s）" % (a.fingerprint, a.name))
        return 1
    print(p)
    print("fingerprint=%s" % fp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
