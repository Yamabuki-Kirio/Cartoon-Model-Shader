# -*- coding: utf-8 -*-
"""
本地材质确认服务 + 渲染会话（v3.1）
=====================================================================================
零依赖本地 HTTP 服务。完成"最少人工确认"，并**由服务端唯一持有渲染任务**。

为什么要由服务端持有渲染
-------------------------------------------------------------------------------------
以前是「页面负责确认」「开始渲染.py 负责渲染」两个触发点。加上页面按钮后就有两个
触发点 → 可能起两个 Blender。现在改成：

    * 渲染任务的状态机只存在于 **服务端这一个进程**里；
    * 「最后一项确认后自动续跑」和「用户点按钮」都只是**调用同一个受锁保护的函数**，
      谁先到谁赢，另一个拿到 `already=true`；
    * 再加一层文件系统锁（render_lock.py），跨进程也不会起两个 Blender。

HTTP 接口（刻意很小）
-------------------------------------------------------------------------------------
    GET  /                             确认页
    GET  /api/session                  全部待确认项 + 统计 + 会话 id + 渲染状态
    GET  /api/session/<sid>/status     只返回渲染状态（轮询用，很轻）
    POST /api/session/<sid>/render     开始/重试渲染（body 只允许 {"retry": true}）
    POST /api/session/<sid>/open-output  打开输出目录（仅 SUCCESS 后可用）
    POST /api/answer                   写一条确认 → 立即落盘到该模型的侧车映射
    GET  /previews/<file>              预览图

安全边界
-------------------------------------------------------------------------------------
- 只监听 127.0.0.1。
- `sid` 是本进程启动时随机生成的会话 id，只有本页知道；不匹配一律 404。
- **浏览器永远不能传路径**：`/render` 只认 `retry` 布尔量，模型路径与输出目录
  全部来自服务端登记的 job（`--job` 文件，或从确认清单派生）。
- job 里的每个目标都要通过三重校验才会被执行：
  ① 指纹必须已在本次会话里登记；② 文件内容 sha256 必须等于该指纹；
  ③ 路径必须位于素材根目录之下。

状态
-------------------------------------------------------------------------------------
    waiting    等待确认（还有 N 项没确认）
    ready      可渲染
    rendering  渲染中
    success / rejected / diagnostic / failed   —— 终态（只有 success 是成品）

用法
-------------------------------------------------------------------------------------
    python confirm_server.py --all [--port 8770] [--open]
    python confirm_server.py --confirmation <conf.json> [--port 8770]
    python confirm_server.py --all --check                 # 只打印还缺什么
    python confirm_server.py --all --auto-render --job <job.json>   # 一键入口用
    python confirm_server.py --all --self-test             # 不开 Blender，只验状态机
"""
import argparse
import glob
import hashlib
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
V31 = os.path.dirname(HERE)
sys.path.insert(0, V31)
sys.path.insert(0, HERE)

import run_contract as RC                       # noqa: E402
from material_classifier import map_path_for    # noqa: E402
from render_lock import RenderLock, LockBusy    # noqa: E402

ROOT = os.environ.get("TOON_MODEL_ROOT", "")
BLENDER = os.environ.get("TOON_BLENDER", "")
SOURCE_BLEND = os.environ.get("TOON_SRC_BLEND", "")
DRIVER = os.path.join(V31, "一键渲染_通用驱动.py")
SCRIPT = os.path.join(V31, "一键卡通渲染.py")
RUNTIME_DIR = os.environ.get("TOON_RUNTIME_DIR", os.path.join(V31, "runtime"))
CONF_DIR = os.path.join(RUNTIME_DIR, "confirmation")
JOB_DIR = os.path.join(CONF_DIR, "jobs")
LOCK_DIR = os.path.join(CONF_DIR, ".locks")

STATE = {"models": {}, "order": [], "maps_dir": None, "lock": threading.RLock()}

#: 终态严重度 —— 会话聚合时取"最坏"的那个（全 SUCCESS 才算成品）
SEV = {"SUCCESS": 0, "DIAGNOSTIC": 1, "REJECTED": 2, "FAILED": 3}

STATE_LABEL = {
    "waiting": "等待确认",
    "ready": "可渲染",
    "rendering": "渲染中",
    "success": "成品完成",
    "rejected": "已拒绝（渲染前拦下，未出图）",
    "diagnostic": "诊断图（不是成品）",
    "failed": "失败",
    "idle": "空闲",
}

RENDER = {
    "session_id": secrets.token_hex(8),
    "auto": False,          # 自动续跑模式（由 开始渲染.py 启动时为 True）
    "job": None,            # 服务端登记的 job（含 targets），浏览器拿不到也改不了
    "job_path": None,
    "state": "idle",        # idle（从未跑过） | running | done
    "attempt": 0,
    "outcome": None,        # 大写 token：SUCCESS/REJECTED/DIAGNOSTIC/FAILED
    "error": None,
    "results": [],
    "history": [],
    "started_at": None,
    "ended_at": None,
    "self_test": False,
    "lock": threading.Lock(),
    "thread": None,
    "auto_event": threading.Event(),
}


# =====================================================================================
#  基础
# =====================================================================================
def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _under(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) \
            == os.path.abspath(root)
    except ValueError:
        return False


# =====================================================================================
#  确认部分（原有逻辑）
# =====================================================================================
def load_model(conf_path, maps_dir):
    with io.open(conf_path, encoding="utf-8") as f:
        conf = json.load(f)
    fp = conf["model"]["fingerprint"].split(":", 1)[1]
    mp = map_path_for(None, fp, maps_dir)
    assignments = {}
    if os.path.isfile(mp):
        try:
            with io.open(mp, encoding="utf-8") as f:
                old = json.load(f)
            for k, v in (old.get("assignments") or {}).items():
                if isinstance(v, dict):
                    assignments[k] = v
        except Exception:
            pass
    return {
        "fingerprint": fp,
        "conf_path": conf_path,
        "map_path": mp,
        "conf": conf,
        "assignments": assignments,
        "file": conf["model"]["file"],
        "display_name": conf["model"].get("display_name"),
    }


def save_model(m):
    data = {
        "schema": "toon-material-map/1",
        "model": {
            "display_name": m["display_name"],
            "fingerprint": "sha256:" + m["fingerprint"],
            "pmx": m["file"],
            "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed_by": "local_confirm_page",
        },
        "assignments": m["assignments"],
    }
    tmp = m["map_path"] + ".tmp"
    os.makedirs(os.path.dirname(m["map_path"]), exist_ok=True)
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, m["map_path"])
    return m["map_path"]


def session_payload():
    models = []
    tot = {"structure_suggested": 0, "unresolved": 0, "done": 0, "pending": 0}
    for fp in STATE["order"]:
        m = STATE["models"][fp]
        items = []
        for x in m["conf"]["items"]:
            if not x.get("requires_user"):
                continue
            done = x["material"] in m["assignments"]
            items.append(dict(x, model=m["file"], model_fingerprint=fp,
                              confirmed=done,
                              confirmed_group=(m["assignments"].get(x["material"]) or {}).get("group")
                              if done else None))
            st = x.get("stage")
            if st == "SUGGESTED":
                tot["structure_suggested"] += 1
            elif st == "UNRESOLVED":
                tot["unresolved"] += 1
            tot["done" if done else "pending"] += 1
        if items:
            models.append({"file": m["file"], "display_name": m["display_name"],
                           "fingerprint": fp, "items": items})
    return {"schema": "toon-confirm-session/2", "models": models, "metrics": tot,
            "session_id": RENDER["session_id"], "render": render_status()}


# =====================================================================================
#  渲染会话
# =====================================================================================
def pending_items():
    """[(模型名, 材质名)] —— 还没确认的项。"""
    out = []
    for fp in STATE["order"]:
        m = STATE["models"][fp]
        for x in m["conf"]["items"]:
            if x.get("requires_user") and x["material"] not in m["assignments"]:
                out.append({"model": m["file"], "material": x["material"],
                            "fingerprint": fp})
    return out


def session_state():
    """waiting / ready / rendering / 终态（小写）。"""
    if RENDER["state"] == "running":
        return "rendering"
    if RENDER["outcome"]:
        return RC.normalize(RENDER["outcome"])
    return "waiting" if pending_items() else "ready"


def render_status():
    st = session_state()
    shippable = bool(RENDER["outcome"]) and RC.is_shippable(RENDER["outcome"])
    return {
        "schema": "toon-render-status/1",
        "session_id": RENDER["session_id"],
        "state": st,
        "state_label": STATE_LABEL.get(st, st),
        "auto_render": bool(RENDER["auto"]),
        "self_test": bool(RENDER["self_test"]),
        "pending": len(pending_items()),
        "pending_items": ["%s / %s" % (p["model"], p["material"]) for p in pending_items()],
        "attempt": RENDER["attempt"],
        "outcome": RENDER["outcome"],
        "shippable": shippable,
        "results": RENDER["results"],
        "history": RENDER["history"],
        "outputs": sorted({os.path.dirname(r["out_dir"]) if os.path.isfile(r["out_dir"]) else r["out_dir"]
                           for r in RENDER["results"] if r.get("out_dir")}),
        "out_root": (RENDER["job"] or {}).get("out_root"),
        "error": RENDER["error"],
        "blender_channel": blender_channel_ready(),
        "started_at": RENDER["started_at"],
        "ended_at": RENDER["ended_at"],
        "targets": [{"label": t.get("label"), "out": t.get("out")}
                    for t in ((RENDER["job"] or {}).get("targets") or [])],
    }


def _send(cmd, timeout=60):
    """通过常驻 Blender MCP 进程派发。"""
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


def _dispatch(argv, log_path):
    """
    起一个后台 Blender。返回 (ok, info)。
    优先走常驻 Blender MCP 进程；不可用时回退为本机直接启动。
    两条通道都失败时给出可操作诊断，而不是抛裸异常。
    """
    resident_err = None
    try:
        code = ("import subprocess, json\n"
                "p = subprocess.Popen(%r, stdout=open(%r,'w',encoding='utf-8',errors='replace'),"
                " stderr=subprocess.STDOUT)\n"
                "print('@@LAUNCH@@' + json.dumps({'pid': p.pid}))\n" % (argv, log_path))
        r = _send({"type": "execute_code", "params": {"code": code}}, timeout=60)
        if "@@LAUNCH@@" in json.dumps(r):
            return True, "resident"
        resident_err = str(r)[:200]
    except Exception as e:
        resident_err = "%r" % (e,)
    # 回退：直接启动本机 Blender。
    try:
        logf = open(log_path, "w", encoding="utf-8", errors="replace")
        p = subprocess.Popen(argv, stdout=logf, stderr=subprocess.STDOUT)
        return True, "direct:pid=%d" % p.pid
    except Exception as e:
        return False, ("无法启动 Blender。"
                       "常驻通道 127.0.0.1:9876 不可用（%s）；"
                       "直接启动亦被拒（%s）。\n"
                       "      处理：先在 Blender 里启用 MCP 监听（默认端口 9876），再重试渲染。"
                       % (resident_err, e))


_CHAN = {"ok": None, "ts": 0.0}


def blender_channel_ready(timeout=2.0, cache_sec=3.0):
    """常驻 Blender 通道是否在线（渲染的唯一可用通路）。"""
    now = time.time()
    if _CHAN["ok"] is not None and now - _CHAN["ts"] < cache_sec:
        return _CHAN["ok"]
    ok = False
    try:
        s = socket.create_connection(("127.0.0.1", 9876), timeout=timeout)
        s.close()
        ok = True
    except Exception:
        ok = False
    _CHAN["ok"], _CHAN["ts"] = ok, now
    return ok


def _run_one(tgt, out_dir, job, timeout=1800):
    """跑一个模型，返回 result dict。"""
    os.makedirs(out_dir, exist_ok=True)
    logp = os.path.join(out_dir, "render.stdout.log")
    done = os.path.join(out_dir, "render.done")
    for p in (logp, done):
        try:
            os.remove(p)
        except Exception:
            pass

    if RENDER["self_test"]:
        # 自检模式：**绝不启动 Blender，也绝不可能产出 SUCCESS**
        return {"label": tgt.get("label"), "fingerprint": tgt.get("fingerprint"),
                "out_dir": out_dir, "outcome": "FAILED", "token": "FAILED",
                "error": "自检模式（--self-test）：未启动 Blender，仅验证流程状态机",
                "pid_source": None, "log": None}

    argv = [job.get("blender") or BLENDER, "--background", "--factory-startup",
            "--python", job.get("driver") or DRIVER, "--",
            "--pmx", tgt["pmx"], "--out", out_dir,
            "--mode", job.get("mode", "faithful"),
            "--material-policy", "strict",
            "--resolve", job.get("source_blend") or SOURCE_BLEND,
            "--script", job.get("script") or SCRIPT,
            "--auto-frame", str(int(job.get("auto_frame") or 0)),
            "--log", logp, "--done", done]
    mf = tgt.get("map_file")
    if mf and os.path.isfile(mf):
        argv += ["--material-map", mf]

    ok, info = _dispatch(argv, logp)
    if not ok:
        return {"label": tgt.get("label"), "fingerprint": tgt.get("fingerprint"),
                "out_dir": out_dir, "outcome": "FAILED", "token": "FAILED",
                "error": "派发失败：%s" % info, "pid_source": None, "log": logp}

    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.isfile(done):
            break
        time.sleep(1.0)
    if not os.path.isfile(done):
        return {"label": tgt.get("label"), "fingerprint": tgt.get("fingerprint"),
                "out_dir": out_dir, "outcome": "FAILED", "token": "FAILED",
                "error": "超时（%ds）未见完成标记" % timeout,
                "pid_source": info, "log": logp}

    tok = ""
    try:
        tok = io.open(done, encoding="utf-8").read().strip().splitlines()[0].strip()
    except Exception:
        pass
    outcome = RC.normalize(tok).upper()
    extra = {}
    rp = os.path.join(out_dir, "run_result.json")
    if os.path.isfile(rp):
        try:
            with io.open(rp, encoding="utf-8") as f:
                extra = json.load(f)
        except Exception:
            pass
    return {"label": tgt.get("label"), "fingerprint": tgt.get("fingerprint"),
            "out_dir": out_dir, "outcome": outcome, "token": tok,
            "shippable": RC.is_shippable(outcome),
            "error": None if outcome == "SUCCESS" else (extra.get("label") or "见日志"),
            "pid_source": info, "log": logp,
            "material_classification": (extra.get("material_classification") or {})}


def _render_worker(attempt, targets, job):
    results = []
    out_root = job.get("out_root") or os.path.join(V31, "output")
    ts = time.strftime("%m%d_%H%M%S")
    try:
        for tgt in targets:
            label = tgt.get("label") or os.path.basename(tgt["pmx"])
            out_dir = tgt.get("out") or os.path.join(
                out_root, "%s_%s_a%d" % (os.path.splitext(label)[0], ts, attempt))
            lockp = os.path.join(LOCK_DIR, "%s.render.lock" % tgt["fingerprint"][:16])
            try:
                with RenderLock(lockp, tag="server:a%d:%s" % (attempt, RENDER["session_id"])):
                    res = _run_one(tgt, out_dir, job)
            except LockBusy as e:
                res = {"label": label, "fingerprint": tgt["fingerprint"],
                       "out_dir": out_dir, "outcome": "FAILED", "token": "FAILED",
                       "error": "已有渲染在跑（跨进程锁）：%s" % e.holder,
                       "pid_source": None, "log": None}
            res["attempt"] = attempt
            results.append(res)
            RENDER["results"] = list(results)      # 边跑边可见
    except Exception as e:
        results.append({"label": "(worker)", "outcome": "FAILED", "token": "FAILED",
                        "error": "渲染线程异常：%r" % e, "attempt": attempt,
                        "out_dir": None, "fingerprint": None})
    finally:
        worst = max(results, key=lambda r: SEV.get(r["outcome"], 3)) if results else None
        with RENDER["lock"]:
            RENDER["results"] = results
            RENDER["outcome"] = worst["outcome"] if worst else "FAILED"
            RENDER["error"] = None if (worst and worst["outcome"] == "SUCCESS") \
                else (worst or {}).get("error")
            RENDER["state"] = "done"
            RENDER["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            RENDER["history"].append({"attempt": attempt, "outcome": RENDER["outcome"],
                                      "ended_at": RENDER["ended_at"]})
        _write_result_file()
        RENDER["auto_event"].set()


def _write_result_file():
    if not RENDER["job_path"]:
        return
    p = os.path.splitext(RENDER["job_path"])[0].replace(".job", "") + ".render-result.json"
    payload = {"schema": "toon-render-session-result/1",
               "session_id": RENDER["session_id"],
               "outcome": RENDER["outcome"],
               "shippable": RC.is_shippable(RENDER["outcome"] or ""),
               "attempts": RENDER["history"],
               "results": RENDER["results"],
               "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def try_start_render(retry=False, actor="page"):
    """
    唯一入口：自动续跑与页面按钮都走这里。
    整个"检查 + 开始"在一把锁里完成 → 两个并发请求只有一个能真正起渲染。
    返回 (ok, http_code, payload)
    """
    with RENDER["lock"]:
        if RENDER["state"] == "running":
            return True, 200, dict(render_status(), started=False, already=True,
                                   reason="rendering_in_progress", actor=actor)
        pend = pending_items()
        if pend:
            return False, 409, dict(render_status(), started=False, already=False,
                                    error="还有 %d 项未确认" % len(pend), actor=actor)
        if not RENDER["job"] or not RENDER["job"].get("targets"):
            return False, 409, dict(render_status(), started=False, already=False,
                                    error="服务端没有登记可执行的渲染目标", actor=actor)
        if RENDER["state"] == "done" and RC.is_shippable(RENDER["outcome"] or ""):
            return True, 200, dict(render_status(), started=False, already=True,
                                   reason="already_succeeded", actor=actor)
        if RENDER["state"] == "done" and not retry:
            return True, 200, dict(render_status(), started=False, already=True,
                                   reason="terminal_result_exists", actor=actor)

        RENDER["attempt"] += 1
        attempt = RENDER["attempt"]
        RENDER["state"] = "running"
        RENDER["outcome"] = None
        RENDER["error"] = None
        RENDER["results"] = []
        RENDER["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        RENDER["ended_at"] = None
        RENDER["auto_event"].clear()
        targets = [dict(t) for t in RENDER["job"]["targets"]]
        job = dict(RENDER["job"])
        th = threading.Thread(target=_render_worker, args=(attempt, targets, job),
                              daemon=True, name="render-a%d" % attempt)
        RENDER["thread"] = th
        th.start()
        return True, 200, dict(render_status(), started=True, already=False,
                               reason="started", actor=actor)


def _auto_watcher():
    """自动续跑：只要全部确认且从未开始过，就起渲染（绝不会自动重试失败）。"""
    while True:
        time.sleep(1.0)
        if not RENDER["auto"]:
            continue
        if RENDER["state"] != "idle":
            continue
        if pending_items():
            continue
        try_start_render(retry=False, actor="auto")


def _launch_auto_watcher():
    if RENDER["auto"]:
        threading.Thread(target=_auto_watcher, daemon=True, name="auto-watch").start()


# =====================================================================================
#  job 登记与校验
# =====================================================================================
def _validate_job(job):
    """
    job 只可能来自本地进程（--job 文件）或由本服务从确认清单派生，绝不来自浏览器。
    即便如此仍然逐项复核，确保不会被塞进任意路径。
    """
    tgs = []
    for t in (job.get("targets") or []):
        fp = (t.get("fingerprint") or "").strip()
        pmx = t.get("pmx")
        if fp not in STATE["models"]:
            continue                                   # ① 必须是本次登记过的会话
        if not pmx or not os.path.isfile(pmx):
            continue
        try:
            if sha256_of(pmx) != fp:
                continue                               # ② 内容指纹必须对上
        except Exception:
            continue
        if not ROOT or not _under(pmx, ROOT):
            continue                                   # ③ 必须位于素材根目录下
        out = t.get("out")
        allowed_out_root = job.get("out_root") or os.path.join(RUNTIME_DIR, "output")
        if out and not _under(out, allowed_out_root):
            out = None                                 # 输出目录也只允许落在本工程内
        mf = None
        maps_dir = job.get("maps_dir")
        if maps_dir:
            cand = map_path_for(None, fp, maps_dir)
            if os.path.isfile(cand) and _under(cand, maps_dir):
                mf = cand
        tgs.append({"fingerprint": fp, "pmx": pmx,
                    "label": t.get("label") or os.path.basename(pmx),
                    "out": out, "map_file": mf})
    job["targets"] = tgs
    if not job.get("out_root"):
        job["out_root"] = os.path.join(RUNTIME_DIR, "output")
    return job


def _derive_job_from_conf():
    """独立打开确认服务时的默认 job：就渲染本次登记的这些模型。"""
    tgs = []
    for fp in STATE["order"]:
        m = STATE["models"][fp]
        p = (m["conf"].get("model") or {}).get("path")
        if p:
            tgs.append({"fingerprint": fp, "pmx": p, "label": m["file"]})
    return {"schema": "toon-render-job/1", "auto_render": False,
            "mode": "faithful", "auto_frame": 0,
            "source_blend": SOURCE_BLEND, "blender": BLENDER,
            "driver": DRIVER, "script": SCRIPT,
            "out_root": os.path.join(RUNTIME_DIR, "output"),
            "maps_dir": STATE["maps_dir"],
            "targets": tgs}


# =====================================================================================
#  HTTP
# =====================================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "toon-confirm/3.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code, obj):
        return self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def _sid_ok(self, sid):
        return bool(sid) and sid == RENDER["session_id"]

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])
        if path in ("/", "/index.html"):
            with io.open(os.path.join(HERE, "confirm_page.html"), encoding="utf-8") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/session":
            return self._json(200, session_payload())
        parts = [p for p in path.split("/") if p]
        # /api/session/<sid>/status
        if len(parts) == 4 and parts[:2] == ["api", "session"] and parts[3] == "status":
            if not self._sid_ok(parts[2]):
                return self._json(404, {"ok": False, "error": "未知会话 id"})
            return self._json(200, render_status())
        if path.startswith("/previews/"):
            name = os.path.basename(path)          # 中文文件名必须 URL 解码，否则 404
            fp = os.path.join(CONF_DIR, "previews", name)
            if not os.path.isfile(fp):
                return self._send(404, "not found: %s" % name, "text/plain; charset=utf-8")
            with open(fp, "rb") as f:
                return self._send(200, f.read(), "image/png")
        return self._send(404, "not found", "text/plain; charset=utf-8")

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        path = unquote(self.path.split("?", 1)[0])
        payload = self._read_body()
        if payload is None:
            return self._json(400, {"ok": False, "error": "bad json"})

        if path == "/api/answer":
            return self._api_answer(payload)

        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[:2] == ["api", "session"]:
            sid, action = parts[2], parts[3]
            if not self._sid_ok(sid):
                return self._json(404, {"ok": False, "error": "未知会话 id"})
            if action == "render":
                # ★ 只认 retry 这一个布尔量。路径/命令一律不接受。
                retry = bool(payload.get("retry")) if isinstance(payload, dict) else False
                ok, code, res = try_start_render(retry=retry, actor="page")
                if not ok:
                    return self._json(code, res)
                return self._json(code, res)
            if action == "open-output":
                return self._api_open_output()
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def _api_answer(self, payload):
        fp = (payload.get("model_fingerprint") or "").strip()
        mat = payload.get("material")
        cls = payload.get("class")
        if not fp or not mat or not cls:
            return self._json(400, {"ok": False,
                                    "error": "缺少 model_fingerprint/material/class"})
        with STATE["lock"]:
            m = STATE["models"].get(fp)
            if not m:
                return self._json(404, {"ok": False, "error": "未知模型指纹 %s" % fp})
            try:
                from material_classifier import MaterialClassifier
                c2g = MaterialClassifier().rules.class_to_group
            except Exception:
                c2g = {}
            group = c2g.get(cls, cls)
            m["assignments"][mat] = {
                "group": group, "class": cls, "source": "user_confirmed",
                "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            saved = save_model(m)
        # 全部确认完 → 自动续跑（与页面按钮互斥，只有一个能真正开始）
        auto = None
        if RENDER["auto"] and not pending_items():
            auto = try_start_render(retry=False, actor="auto-after-confirm")[2]
        body = {"ok": True, "group": group, "saved_to": os.path.basename(saved),
                "render": render_status()}
        if auto is not None:
            body["auto_start"] = {"started": auto.get("started"),
                                  "already": auto.get("already"),
                                  "reason": auto.get("reason")}
        return self._json(200, body)

    def _api_open_output(self):
        if not (RENDER["outcome"] and RC.is_shippable(RENDER["outcome"])):
            return self._json(409, {"ok": False, "error": "只有 SUCCESS 才有输出目录"})
        outs = [r["out_dir"] for r in RENDER["results"] if r.get("out_dir")]
        target = (RENDER["job"] or {}).get("out_root")
        if outs:
            target = os.path.dirname(outs[0]) if os.path.isfile(outs[0]) else outs[0]
        if not target or not _under(target, V31):
            return self._json(409, {"ok": False, "error": "输出目录不在本工程内"})
        err = None
        try:
            if os.name == "nt":
                os.startfile(target)                     # noqa: S606
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as e:
            err = str(e)
        return self._json(200, {"ok": True, "path": target, "opened": err is None,
                                "error": err})


# =====================================================================================
#  入口
# =====================================================================================
def main():
    global ROOT, BLENDER, SOURCE_BLEND, RUNTIME_DIR, CONF_DIR, JOB_DIR, LOCK_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirmation")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--maps-dir", default=os.path.join(V31, "model_material_maps"))
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--auto-render", action="store_true",
                    help="自动续跑：全部确认后由服务端自动开始渲染（一键入口用）")
    ap.add_argument("--job", help="渲染任务文件（只由本地进程写入；浏览器无法提供）")
    ap.add_argument("--mode", default="faithful", choices=("faithful", "enhanced"))
    ap.add_argument("--auto-frame", type=int, default=0)
    ap.add_argument("--out-root")
    ap.add_argument("--model-root", default=ROOT)
    ap.add_argument("--source-blend", default=SOURCE_BLEND)
    ap.add_argument("--blender", default=BLENDER)
    ap.add_argument("--runtime-dir", default=RUNTIME_DIR)
    ap.add_argument("--self-test", action="store_true",
                    help="不开 Blender，只验证流程状态机（结果恒为 FAILED，绝不可能是成品）")
    a = ap.parse_args()

    ROOT = os.path.abspath(a.model_root) if a.model_root else ""
    SOURCE_BLEND = os.path.abspath(a.source_blend) if a.source_blend else ""
    BLENDER = os.path.abspath(a.blender) if a.blender else ""
    RUNTIME_DIR = os.path.abspath(a.runtime_dir)
    CONF_DIR = os.path.join(RUNTIME_DIR, "confirmation")
    JOB_DIR = os.path.join(CONF_DIR, "jobs")
    LOCK_DIR = os.path.join(CONF_DIR, ".locks")
    if not ROOT:
        ap.error("需要 --model-root 或环境变量 TOON_MODEL_ROOT")

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    STATE["maps_dir"] = a.maps_dir
    RENDER["auto"] = bool(a.auto_render)
    RENDER["self_test"] = bool(a.self_test)

    paths = []
    if a.all:
        paths = sorted(glob.glob(os.path.join(CONF_DIR, "*_confirmation.json")))
    elif a.confirmation:
        paths = [a.confirmation]
    else:
        ap.error("需要 --all 或 --confirmation")

    for p in paths:
        m = load_model(p, a.maps_dir)
        if not any(x.get("requires_user") for x in m["conf"]["items"]):
            continue
        STATE["models"][m["fingerprint"]] = m
        STATE["order"].append(m["fingerprint"])

    if not STATE["models"]:
        print("没有待确认的模型")
        return 0

    # ---- 登记 job（先登记后校验；浏览器全程无法参与）
    if a.job:
        try:
            with io.open(a.job, encoding="utf-8") as f:
                job = json.load(f)
            RENDER["job_path"] = a.job
        except Exception as e:
            print("!! 读取 job 失败：%s" % e)
            return 2
    else:
        job = _derive_job_from_conf()
    job.setdefault("mode", a.mode)
    job.setdefault("auto_frame", a.auto_frame)
    if a.out_root:
        job["out_root"] = a.out_root
    job.setdefault("maps_dir", a.maps_dir)
    RENDER["job"] = _validate_job(job)

    payload = session_payload()
    tot = payload["metrics"]
    if a.check:
        print("待确认 %d 项，已完成 %d 项" % (tot["pending"] + tot["done"], tot["done"]))
        for mo in payload["models"]:
            miss = [i["material"] for i in mo["items"] if not i["confirmed"]]
            print("  %-22s 缺 %s" % (mo["file"], "、".join(miss) or "（无）"))
        return 0 if tot["pending"] == 0 else 1

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = "http://127.0.0.1:%d/" % a.port
    print("确认页：%s" % url)
    print("会话 id：%s（页面自动带上；HTTP 不接受任何路径参数）" % RENDER["session_id"])
    print("模型 %d 个，待确认 %d 项（已完成 %d）"
          % (len(payload["models"]), tot["pending"] + tot["done"], tot["done"]))
    print("渲染模式：%s%s%s"
          % ("自动续跑" if RENDER["auto"] else "手动（页面按钮）",
             "｜自检（不开 Blender）" if RENDER["self_test"] else "",
             "｜已登记 %d 个渲染目标" % len(RENDER["job"]["targets"])))
    for mo in payload["models"]:
        print("  %-22s %d 项" % (mo["file"], len(mo["items"])))

    _launch_auto_watcher()
    if a.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
