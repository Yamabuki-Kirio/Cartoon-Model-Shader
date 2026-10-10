# -*- coding: utf-8 -*-
"""
开始渲染 —— v3.1 的【唯一入口】
=====================================================================================
用户只需要做一件事：

    选择 PMX  →  开始渲染

内部自动串起整条流程：

    预检 → 自动分类 → 查找侧车映射 → 严格准入
         ├─ 无需确认  → 直接渲染（本进程就是唯一渲染者）
         └─ 需要确认  → 自动打开本地确认页 → 全部确认后**由服务端**继续渲染
                        （本进程只做监视与汇报，不再自己起渲染）

为什么需要确认时不由本进程渲染
-------------------------------------------------------------------------------------
一旦页面上也有「开始渲染」按钮，就有两个触发点了。如果本进程也渲染，就可能出现
两个 Blender。所以约定：**确认路径下，渲染任务归服务端唯一所有**；
本进程只写 job 文件、启动服务、轮询状态、报告终态。服务端内部再用锁 + 文件锁
保证"自动续跑"和"页面按钮"只会有一个真正开始。

用法：
    python 开始渲染.py --pmx "模型.pmx"
    python 开始渲染.py --fingerprint 545a9ba0        # 按指纹反查（推荐，免手写路径）
    python 开始渲染.py --pmx "模型.pmx" --mode enhanced --auto-frame

参数（除 --pmx/--fingerprint 外都有默认值）：
    --out <目录>          输出目录，默认 output/<模型名>_<时间戳>（每次运行都是新目录）
    --mode faithful|enhanced
    --auto-frame 0|1      构图
    --no-confirm-ui       即使需要确认也不开页面（只打印缺什么）
    --port <n>            本地确认页端口，默认 8770
    --maps-dir <目录>     侧车映射目录；默认=用户数据目录（不在仓库内），
                          经入口 → 确认服务 → 驱动 → 主脚本全程贯穿（F6）
"""
import argparse
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "_tools"))

from material_classifier import display_path      # noqa: E402
from render_lock import RenderLock, LockBusy    # noqa: E402

ROOT = os.environ.get("TOON_MODEL_ROOT", "")
SOURCE_BLEND = os.environ.get("TOON_SRC_BLEND", "")
BLENDER = os.environ.get("TOON_BLENDER", "")
RUNTIME_DIR = os.environ.get("TOON_RUNTIME_DIR", os.path.join(HERE, "runtime"))
CONF_DIR = os.path.join(RUNTIME_DIR, "confirmation")
PREVIEW_DIR = os.path.join(CONF_DIR, "previews")
JOB_DIR = os.path.join(CONF_DIR, "jobs")
LOCK_DIR = os.path.join(CONF_DIR, ".locks")
PY = sys.executable

TERMINAL = ("success", "rejected", "diagnostic", "failed")


def default_maps_dir():
    """
    侧车映射的默认目录 —— 与项目既有约定一致：**用户数据不进仓库**。

    %LOCALAPPDATA%\\CartoonModelShader\\model_material_maps（用 TOON_MAPS_DIR 可覆盖）。
    仓库内的 render_pipeline/model_material_maps/ 只放随代码分发的只读样例
    （example.material-map.json，由 tests/test_render_pipeline_repo_guard.py 守卫），
    运行数据（用户确认结果、待填模板）一律不写进去。
    """
    env = os.environ.get("TOON_MAPS_DIR")
    if env:
        return os.path.abspath(env)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "CartoonModelShader", "model_material_maps")


def ensure_out_dir(out_dir):
    """
    创建输出目录，失败时给**稳定错误码 + 可读说明**（F7）。

    以前这里是裸 os.makedirs：--out 指向「已存在的文件」或「不存在的盘符」时
    直接抛 FileExistsError / FileNotFoundError 的 traceback，调用方既拿不到
    错误码也拿不到可读原因。返回 (ok, error_dict)。
    """
    try:
        os.makedirs(out_dir, exist_ok=True)
        return True, None
    except FileExistsError:
        return False, {"code": "OUTPUT_NOT_WRITABLE",
                       "message": "输出目录不可用：该路径已存在且不是一个目录。",
                       "retryable": False,
                       "hint": "换一个 --out 目录，或先删掉/改名同名的文件。",
                       # ★ O4：错误响应只给**逻辑路径**，不回显用户名目录。
                       "details": {"out_dir": display_path(out_dir)}}
    except OSError as e:
        return False, {"code": "OUTPUT_NOT_WRITABLE",
                       "message": "无法创建输出目录：%s" % (getattr(e, "strerror", None) or e),
                       "retryable": False,
                       "hint": "检查该路径所在磁盘是否存在、是否有写权限，或换一个 --out 目录。",
                       "details": {"out_dir": display_path(out_dir),
                                   "errno": getattr(e, "errno", None)}}


def report_final(outcome, out_dir, confirm_path=False):
    """
    末行必须与**结构化终态**一致 —— 只有 ``status == "SUCCESS"`` 才允许出现「完成」。

    以前直渲路径在 ``render()`` 返回后**无条件**打印「[3/3] 完成（输出：…）」，
    哪怕 ``render()`` 已经因为 ``--out`` 不可写（``OUTPUT_NOT_WRITABLE``）返回 1，
    末行仍说「完成」，与事实相反。失败时必须给 **失败阶段 + 错误码**。
    """
    err = (outcome or {}).get("error") or {}
    if (outcome or {}).get("status") == "SUCCESS":
        out("[3/3] 完成（输出：%s）" % out_dir)
        return
    stage = (outcome or {}).get("stage") or "渲染"
    line = "[3/3] 失败（阶段：%s" % stage
    if err.get("code"):
        line += "；错误码 %s" % err["code"]
    line += "） —— 未产出成品"
    out(line)
    if err.get("message"):
        out("      ↳ %s" % err["message"])
    if err.get("hint"):
        out("      ↳ %s" % err["hint"])
    tail = "按契约不可交付"
    if confirm_path:
        tail += "；确认结果已保留，可直接重试渲染"
    out("      （%s；输出目录：%s）" % (tail, out_dir))


def out(*a):
    print(*a, flush=True)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def locate(fingerprint=None, pmx=None, name=None):
    """按指纹/文件名反查真实路径 —— 禁止手写路径（踩过三次坑）。"""
    if pmx:
        return pmx if os.path.isfile(pmx) else None
    for r, _ds, fs in os.walk(ROOT):
        for f in sorted(fs):
            if not f.lower().endswith(".pmx"):
                continue
            p = os.path.join(r, f)
            if name and f == name:
                return p
            if fingerprint:
                try:
                    if sha256_of(p).startswith(fingerprint):
                        return p
                except Exception:
                    continue
    return None


# ---------------------------------------------------------------- 与确认服务通信
def http_json(url, data=None, timeout=10):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- Blender 派发
def channel_ready(timeout=2.0):
    """常驻 Blender 通道（9876）是否在线 —— 这是渲染唯一可用的通路。"""
    try:
        s = socket.create_connection(("127.0.0.1", 9876), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


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


def launch_blender(argv, log_path):
    code = ("import subprocess, json\n"
            "p = subprocess.Popen(%r, stdout=open(%r,'w',encoding='utf-8',errors='replace'),"
            " stderr=subprocess.STDOUT)\n"
            "print('@@LAUNCH@@' + json.dumps({'pid': p.pid}))\n" % (argv, log_path))
    return send({"type": "execute_code", "params": {"code": code}}, timeout=60)


# ---------------------------------------------------------------- 各步骤
def preflight(pmx, maps_dir=None):
    """预检 + 分级，产出确认清单。返回 (conf_path, payload)。"""
    import build_confirmation as BC
    from material_classifier import MaterialClassifier
    clf = MaterialClassifier()
    payload = BC.build_one(pmx, clf, maps_dir or default_maps_dir())
    if not payload:
        return None, None
    os.makedirs(CONF_DIR, exist_ok=True)
    fp = payload["model"]["fingerprint"].split(":", 1)[1]
    jp = os.path.join(CONF_DIR, "%s_confirmation.json" % fp[:16])
    with io.open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return jp, payload


def ensure_previews(pmx, conf_path):
    """缺预览图就补（串行、按指纹派发）。"""
    # ★ F7：预览目录以前从不创建（preflight 只建 CONF_DIR），派发后 Blender 侧
    #   open(log_path,'w') 直接 FileNotFoundError → 只打印「预览图派发失败」。
    #   出厂默认配置首次运行必然踩到，确认页因此拿不到遮罩 / 透视高亮 / 推荐图。
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    with io.open(conf_path, encoding="utf-8") as f:
        conf = json.load(f)
    req = [x for x in conf["items"] if x.get("requires_user")]
    need = []
    for x in req:
        for k in ("mask_only", "overview_highlighted", "proposed_group"):
            p = os.path.join(PREVIEW_DIR, os.path.basename(x["previews"][k]))
            if not os.path.isfile(p):
                need.append(p)
    if not need:
        return True
    out("  生成 %d 张预览图（首次需要，之后会复用）…" % len(need))
    # ★ F6：完成标记以前写在仓库代码目录（_tools/_preview_run.done）——那是运行数据，
    #   会把仓库工作树弄脏。改到 PREVIEW_DIR（运行目录）下。
    done = os.path.join(PREVIEW_DIR, "_preview_run.done")
    logp = os.path.join(PREVIEW_DIR, "_%s.log" % hashlib.sha256(pmx.encode()).hexdigest()[:8])
    for p in (done, logp):
        try:
            os.remove(p)
        except Exception:
            pass
    argv = [BLENDER, "--background", "--factory-startup", "--python",
            os.path.join(HERE, "_tools", "preview_gen.py"), "--",
            "--pmx", pmx, "--confirmation", conf_path, "--out", PREVIEW_DIR,
            "--resolve", SOURCE_BLEND, "--log", logp, "--done", done]
    r = launch_blender(argv, logp)
    if "@@LAUNCH@@" not in json.dumps(r):
        out("  !! 预览图派发失败：%s" % str(r)[:150])
        return False
    t0 = time.time()
    while time.time() - t0 < 600:
        if os.path.isfile(done):
            break
        time.sleep(1.5)
    st = open(done, encoding="utf-8").read().strip() if os.path.isfile(done) else "(超时)"
    out("  预览图：%s" % st)
    return st.startswith("OK")


def write_job(pmx, out_dir, mode, auto_frame, maps_dir=None):
    """把渲染任务写成 job 文件 —— 只有本地进程能写，浏览器无法提供。"""
    fp = sha256_of(pmx)
    os.makedirs(JOB_DIR, exist_ok=True)
    job_path = os.path.join(JOB_DIR, "%s.job.json" % fp[:16])
    job = {
        "schema": "toon-render-job/1",
        "auto_render": True,
        "mode": mode,
        "auto_frame": 1 if auto_frame else 0,
        "source_blend": SOURCE_BLEND,
        "blender": BLENDER,
        "driver": os.path.join(HERE, "一键渲染_通用驱动.py"),
        "script": os.path.join(HERE, "一键卡通渲染.py"),
        "out_root": os.path.dirname(out_dir),
        "maps_dir": maps_dir or default_maps_dir(),
        "session_label": os.path.basename(pmx),
        "targets": [{"fingerprint": fp, "pmx": pmx,
                     "label": os.path.basename(pmx), "out": out_dir}],
    }
    with io.open(job_path, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=1)
    return job_path


def run_confirm_and_render(pmx, conf_path, out_dir, mode, auto_frame, port, maps_dir=None):
    """
    启动确认页 → 等用户确认 → **服务端自动续跑渲染** → 轮询终态。
    本进程全程不启动渲染，因此不可能与页面按钮产生两个 Blender。

    返回 ``(token, outcome)``：``token`` 是大写终态，``outcome`` 与 ``render()``
    同构（``status`` / ``stage`` / ``error``），供 ``report_final`` 打印末行。
    """
    ok, err = ensure_out_dir(out_dir)
    if not ok:
        return "FAILED", {"status": "FAILED", "stage": "准备输出目录", "error": err}
    job_path = write_job(pmx, out_dir, mode, auto_frame, maps_dir)
    logp = os.path.join(out_dir, "confirm_server.log")
    buf = open(logp, "w", encoding="utf-8", errors="replace")
    srv = subprocess.Popen([PY, os.path.join(HERE, "_tools", "confirm_server.py"),
                            "--confirmation", conf_path, "--port", str(port),
                            "--auto-render", "--job", job_path,
                            "--model-root", ROOT,
                            "--source-blend", SOURCE_BLEND,
                            "--blender", BLENDER,
                            "--runtime-dir", RUNTIME_DIR,
                            "--maps-dir", maps_dir or default_maps_dir()],
                           stdout=buf, stderr=subprocess.STDOUT)
    url = "http://127.0.0.1:%d/" % port
    try:
        sid = None
        t0 = time.time()
        while time.time() - t0 < 30:
            if srv.poll() is not None:
                out("  !! 确认服务启动失败（退出码 %s），见 %s" % (srv.returncode, logp))
                return "FAILED", {"status": "FAILED", "stage": "启动确认服务",
                                  "error": {"code": "CONFIRM_SERVER_START_FAILED",
                                            "message": "确认服务启动失败（退出码 %s）" % srv.returncode}}
            try:
                d = http_json(url + "api/session", timeout=3)
                sid = d.get("session_id")
                if sid:
                    break
            except Exception:
                time.sleep(0.5)
        if not sid:
            out("  !! 确认服务未就绪（30s 超时），见 %s" % logp)
            return "FAILED", {"status": "FAILED", "stage": "启动确认服务",
                              "error": {"code": "CONFIRM_SERVER_NOT_READY",
                                        "message": "确认服务未就绪（30s 超时）"}}

        out("  已启动本地确认页：%s" % url)
        out("  会话 id：%s（渲染任务由服务端唯一持有）" % sid)
        try:
            webbrowser.open(url)
        except Exception:
            pass
        out("  请在页面上逐条确认；全部确认后服务端会自动继续渲染，无需再点任何按钮。")

        prev = None
        while True:
            try:
                st = http_json("%sapi/session/%s/status" % (url, sid), timeout=5)
            except Exception:
                if srv.poll() is not None:
                    out("  !! 确认服务已退出（退出码 %s），见 %s" % (srv.returncode, logp))
                    return "FAILED", {"status": "FAILED", "stage": "确认与渲染",
                                      "error": {"code": "CONFIRM_SERVER_EXITED",
                                                "message": "确认服务已退出（退出码 %s）"
                                                           % srv.returncode}}
                time.sleep(1.5)
                continue
            key = (st.get("state"), st.get("attempt"), st.get("pending"))
            if key != prev:
                prev = key
                extra = ""
                if st.get("state") == "waiting":
                    extra = "（还需确认 %d 项）" % st.get("pending", 0)
                elif st.get("state") == "rendering":
                    extra = "（第 %d 次尝试）" % st.get("attempt", 0)
                out("  [%s] %s %s" % (time.strftime("%H:%M:%S"),
                                      st.get("state_label") or st.get("state"), extra))
            if st.get("state") in TERMINAL:
                failures = [r for r in (st.get("results") or []) if r.get("error")]
                for r in (st.get("results") or []):
                    out("      %-24s %s  %s"
                        % (str(r.get("label"))[:24], r.get("outcome"),
                           r.get("out_dir") or ""))
                    if r.get("error"):
                        out("        ↳ %s" % r["error"])
                token = (st.get("outcome") or "FAILED").upper()
                if token == "SUCCESS":
                    return token, {"status": "SUCCESS", "token": token}
                return token, {
                    "status": "FAILED", "stage": "确认与渲染", "token": token,
                    "error": {"code": token,
                              "message": (failures[0]["error"] if failures
                                          else "终态 %s：按契约不得进入成品流程" % token)},
                }
            time.sleep(1.5)
    except KeyboardInterrupt:
        out("\n  ！中断：确认服务会被停止；若渲染已在后台启动，它会继续跑完。")
        raise
    finally:
        try:
            srv.terminate()
        except Exception:
            pass
        try:
            buf.close()
        except Exception:
            pass


def render(pmx, out_dir, mode, auto_frame, maps_dir=None):
    """
    无需确认时的直接渲染。带跨进程锁，防止重复起 Blender。

    返回 ``(rc, outcome)``。``outcome`` 是**结构化终态**：

    * ``{"status": "SUCCESS", "token": "SUCCESS"}``
    * ``{"status": "FAILED", "stage": <失败阶段>, "error": {...}}``

    调用方一律用 ``report_final`` 打印末行 —— 未拿到 SUCCESS 就绝不允许出现「完成」。
    """
    ok, err = ensure_out_dir(out_dir)
    if not ok:
        return 1, {"status": "FAILED", "stage": "准备输出目录", "error": err}
    os.makedirs(LOCK_DIR, exist_ok=True)
    lockp = os.path.join(LOCK_DIR, "%s.render.lock" % sha256_of(pmx)[:16])
    try:
        lk = RenderLock(lockp, tag="direct:start-render").acquire()
    except LockBusy as e:
        out("  !! 已有渲染在跑，拒绝重复启动：%s" % json.dumps(e.holder, ensure_ascii=False))
        return 1, {"status": "FAILED", "stage": "获取渲染锁",
                   "error": {"code": "RENDER_BUSY",
                             "message": "已有渲染在跑，拒绝重复启动",
                             "holder": e.holder}}
    try:
        logp = os.path.join(out_dir, "render.stdout.log")
        done = os.path.join(out_dir, "render.done")
        for p in (logp, done):
            try:
                os.remove(p)
            except Exception:
                pass
        argv = [BLENDER, "--background", "--factory-startup", "--python",
                os.path.join(HERE, "一键渲染_通用驱动.py"), "--",
                "--pmx", pmx, "--out", out_dir, "--mode", mode,
                "--material-policy", "strict",
                "--resolve", SOURCE_BLEND,
                "--script", os.path.join(HERE, "一键卡通渲染.py"),
                "--auto-frame", "1" if auto_frame else "0",
                # ★ F6：直渲路径过去完全不传映射目录 —— 于是已确认过的模型
                #   （预检「映射命中 1 / 需确认 0」→ 走这条路）会把确认结果丢掉。
                "--maps-dir", maps_dir or default_maps_dir(),
                "--log", logp, "--done", done]
        out("  渲染中…（日志 %s）" % logp)
        r = launch_blender(argv, logp)
        if "@@LAUNCH@@" not in json.dumps(r):
            out("  !! 渲染派发失败：%s" % str(r)[:150])
            return 1, {"status": "FAILED", "stage": "派发渲染进程",
                       "error": {"code": "RENDER_DISPATCH_FAILED",
                                 "message": "渲染派发失败：%s" % str(r)[:150]}}
        t0 = time.time()
        while time.time() - t0 < 1800:
            if os.path.isfile(done):
                break
            time.sleep(2)
        st = open(done, encoding="utf-8").read().strip() if os.path.isfile(done) else "(超时)"
        out("  渲染终态：%s" % st)
        lines = [ln.strip() for ln in st.splitlines() if ln.strip()]
        token = lines[0] if lines else ""
        if token == "SUCCESS":
            return 0, {"status": "SUCCESS", "token": token}
        return 1, {"status": "FAILED", "stage": "渲染",
                   "error": {"code": token or "RENDER_TIMEOUT",
                             "message": "渲染终态：%s（按契约不可交付）"
                                        % (token or "超时未返回终态")}}
    finally:
        lk.release()


def main():
    global ROOT, SOURCE_BLEND, BLENDER, RUNTIME_DIR, CONF_DIR, PREVIEW_DIR, JOB_DIR, LOCK_DIR
    ap = argparse.ArgumentParser(description="开始渲染（v3.1 唯一入口）")
    ap.add_argument("--pmx")
    ap.add_argument("--fingerprint")
    ap.add_argument("--name")
    ap.add_argument("--out")
    ap.add_argument("--mode", default="faithful", choices=("faithful", "enhanced"))
    ap.add_argument("--auto-frame", type=int, default=0)
    ap.add_argument("--no-confirm-ui", action="store_true")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--model-root", default=ROOT,
                    help="按名称/指纹查找 PMX 的素材根目录；也可设 TOON_MODEL_ROOT")
    ap.add_argument("--source-blend", default=SOURCE_BLEND,
                    help="提供节点组的源 .blend；也可设 TOON_SRC_BLEND")
    ap.add_argument("--blender", default=BLENDER,
                    help="Blender 可执行文件；也可设 TOON_BLENDER")
    ap.add_argument("--runtime-dir", default=RUNTIME_DIR,
                    help="确认数据、预览、任务与锁的运行目录")
    ap.add_argument("--maps-dir", default=None,
                    help="侧车映射目录；不传则用用户数据目录"
                         "（%%LOCALAPPDATA%%\\CartoonModelShader\\model_material_maps，"
                         "TOON_MAPS_DIR 可覆盖）。**默认不指向仓库**，运行数据不入库")
    a = ap.parse_args()

    ROOT = os.path.abspath(a.model_root) if a.model_root else ""
    SOURCE_BLEND = os.path.abspath(a.source_blend) if a.source_blend else ""
    BLENDER = os.path.abspath(a.blender) if a.blender else ""
    RUNTIME_DIR = os.path.abspath(a.runtime_dir)
    a.maps_dir = os.path.abspath(a.maps_dir) if a.maps_dir else default_maps_dir()
    CONF_DIR = os.path.join(RUNTIME_DIR, "confirmation")
    PREVIEW_DIR = os.path.join(CONF_DIR, "previews")
    JOB_DIR = os.path.join(CONF_DIR, "jobs")
    LOCK_DIR = os.path.join(CONF_DIR, ".locks")

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if not a.pmx and not ROOT:
        out("缺少模型根目录：使用 --model-root 或设置 TOON_MODEL_ROOT。")
        return 2
    pmx = locate(a.fingerprint, a.pmx, a.name)
    if not pmx:
        out("找不到模型。请用 --pmx <路径> 或 --fingerprint <指纹前若干位>。")
        return 2

    model_name = os.path.splitext(os.path.basename(pmx))[0]
    if not SOURCE_BLEND or not os.path.isfile(SOURCE_BLEND):
        out("缺少有效源工程：使用 --source-blend 或设置 TOON_SRC_BLEND。")
        return 2
    if not BLENDER or not os.path.isfile(BLENDER):
        out("缺少有效 Blender：使用 --blender 或设置 TOON_BLENDER。")
        return 2
    out_dir = a.out or os.path.join(RUNTIME_DIR, "output",
                                    "%s_%s" % (model_name, time.strftime("%m%d_%H%M%S")))
    out("=" * 72)
    out("开始渲染：%s" % os.path.basename(pmx))
    out("  %s" % pmx)
    out("=" * 72)
    if not channel_ready():
        out("  ！提示：常驻 Blender 通道（127.0.0.1:9876）当前不可用。")
        out("           渲染依赖该通道，请先在 Blender 中开启 MCP 监听。")
        out("           请先在 Blender 中开启 MCP 监听，否则渲染阶段会直接失败。")

    out("[1/3] 预检与自动分类…")
    conf_path, payload = preflight(pmx, a.maps_dir)
    if not payload:
        report_final({"status": "FAILED", "stage": "预检与自动分类",
                      "error": {"code": "PREFLIGHT_FAILED",
                                "message": "预检失败，无法解析该 PMX"}}, out_dir)
        return 1
    s = payload["summary_all"]
    out("  材质 %d：语义自动 %d / 映射命中 %d / 需确认 %d / 无依据 %d"
        % (s["total"], s["semantic_auto_confirmed"], s["model_map_confirmed"],
           s["structure_suggested"], s["unresolved"]))

    requiring = [x for x in payload["items"] if x["requires_user"]]
    if not requiring:
        out("[2/3] 无需人工确认 —— 直接渲染")
        rc, outcome = render(pmx, out_dir, a.mode, bool(a.auto_frame), a.maps_dir)
        report_final(outcome, out_dir)
        return rc

    out("[2/3] 有 %d 个材质需要确认：%s"
        % (len(requiring), "、".join(x["material"] for x in requiring)))
    for x in requiring:
        out("      · %s → 建议 %s（%s）"
            % (x["material"], x["proposed_class"] or "—", x["stage"]))
    if a.no_confirm_ui:
        out("  --no-confirm-ui：只报告，不打开确认页")
        report_final({"status": "FAILED", "stage": "等待人工确认",
                      "error": {"code": "CONFIRMATION_REQUIRED",
                                "message": "有 %d 个材质需要人工确认，"
                                           "--no-confirm-ui 下只报告不渲染" % len(requiring)}},
                     out_dir)
        return 1

    ensure_previews(pmx, conf_path)
    token, outcome = run_confirm_and_render(pmx, conf_path, out_dir, a.mode,
                                            bool(a.auto_frame), a.port, a.maps_dir)
    report_final(outcome, out_dir, confirm_path=True)
    return 0 if token == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
