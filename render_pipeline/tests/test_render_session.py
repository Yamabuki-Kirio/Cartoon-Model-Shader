# -*- coding: utf-8 -*-
"""
渲染会话回归测试（v3.1）
=====================================================================================
覆盖用户点名的 6 条验收：

1. 自动续跑与页面按钮**不会产生两个 Blender 进程**
   —— 并发调用 try_start_render 只有一个 started=True；
   —— 再加一层跨进程文件锁（render_lock）兜底，本文件也单独测它。
2. 刷新页面不会重复触发 —— 重放同一个 POST 拿到的仍是同一个任务（attempt 不变）。
3. 浏览器不能提交任意路径 —— `_validate_job` 三重校验 + HTTP 层根本不读路径字段。
4. 重复点击返回同一个任务 —— 同上。
5. 只有 SUCCESS 才显示「成品完成」 —— 状态机给的就是 run_contract 的判定。
6. DIAGNOSTIC / REJECTED / FAILED 都不得显示成品成功 —— 逐一断言 shippable=False。

运行：
    python -m unittest tests.test_render_session -v
"""
import glob
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

V31 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V31)
sys.path.insert(0, os.path.join(V31, "_tools"))

#: 测试临时目录一律建在**系统临时目录**，不建在仓库里。
#  以前这里写的是 ``dir=V31``：确认服务子进程刚退出时 Windows 上文件还可能被占用，
#  ``shutil.rmtree(..., ignore_errors=True)`` 会**静默**留下残留目录
#  （实测留下过 render_pipeline/_t_http_*，内含 fixture.pmx 与 job.json），
#  进而把 tests/test_render_pipeline_repo_guard.py 的「仓库不得有生成物」守卫打红。
#  运行数据不该写进仓库 —— 测试产出的临时数据同理。
_TEST_TMP = tempfile.gettempdir()


def _cleanup(path):
    """Windows 上删临时目录可能撞文件占用 —— 重试几次，别静默留垃圾。"""
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.2)

import confirm_server as CS          # noqa: E402
import run_contract as RC           # noqa: E402
from render_lock import RenderLock, LockBusy   # noqa: E402

PY = sys.executable
FP_A = "a" * 64
FP_B = "b" * 64


def _real_pmx():
    hits = sorted(glob.glob(os.path.join(CS.ROOT, "**", "迪奥娜.pmx"), recursive=True))
    return hits[0] if hits else None


def http(method, url, obj=None, timeout=15):
    data = json.dumps(obj).encode("utf-8") if obj is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_terminal(base, sid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            _c, st = http("GET", "%s/api/session/%s/status" % (base, sid))
            if st.get("state") in ("success", "rejected", "diagnostic", "failed"):
                return st
        except Exception:
            pass
        time.sleep(0.3)
    return None


# =====================================================================================
#  一、进程内状态机
# =====================================================================================
class TestRenderStateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="_t_rs_", dir=_TEST_TMP)
        CS.STATE["models"] = {}
        CS.STATE["order"] = []
        CS.STATE["maps_dir"] = os.path.join(self.tmp, "maps")
        CS.LOCK_DIR = os.path.join(self.tmp, "locks")
        CS.RENDER.update({
            "session_id": "test-sid", "auto": False, "self_test": True,
            "job": {"out_root": os.path.join(V31, "output"),
                    "targets": [{"fingerprint": FP_A, "pmx": "X:/fake.pmx",
                                 "label": "假模型.pmx", "out": os.path.join(self.tmp, "out")}]},
            "job_path": None, "state": "idle", "attempt": 0, "outcome": None,
            "error": None, "results": [], "history": [],
            "started_at": None, "ended_at": None, "thread": None,
        })
        CS.RENDER["auto_event"].clear()
        CS.STATE["models"][FP_A] = {
            "fingerprint": FP_A, "conf_path": None, "map_path": None,
            "conf": {"model": {"file": "假模型.pmx", "path": "X:/fake.pmx",
                               "fingerprint": "sha256:" + FP_A},
                     "items": [{"material": "测试材质", "index": 0, "stage": "SUGGESTED",
                                "requires_user": True, "previews": {}, "choices": [],
                                "reasons": []}]},
            "assignments": {}, "file": "假模型.pmx", "display_name": "假模型",
        }
        CS.STATE["order"].append(FP_A)

    def tearDown(self):
        if CS.RENDER.get("thread"):
            CS.RENDER["thread"].join(timeout=10)
        _cleanup(self.tmp)

    def _confirm(self):
        CS.STATE["models"][FP_A]["assignments"]["测试材质"] = {"group": "Cel_Cloth"}

    # ---- 1 未确认时拒绝
    def test_pending_blocks_render(self):
        ok, code, res = CS.try_start_render()
        self.assertFalse(ok)
        self.assertEqual(code, 409)
        self.assertIn("未确认", res["error"])
        self.assertEqual(CS.RENDER["attempt"], 0, "被拒绝的请求不得消耗 attempt")

    # ---- 2 并发只有一个真正开始
    def test_concurrent_start_only_one_wins(self):
        self._confirm()
        starts, results = [], []
        barrier = threading.Barrier(6)

        def go():
            barrier.wait()
            _ok, _c, res = CS.try_start_render(retry=True)
            starts.append(bool(res.get("started")))
            results.append(res)

        ths = [threading.Thread(target=go) for _ in range(6)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        self.assertEqual(sum(starts), 1, "并发请求里只能有一个真正启动渲染：%s" % starts)
        self.assertEqual(CS.RENDER["attempt"], 1)
        self.assertEqual(len([r for r in results if r.get("already")]), 5)

    # ---- 3/4 重复点击返回同一个任务
    def test_repeat_request_returns_same_task(self):
        self._confirm()
        _ok, _c, first = CS.try_start_render()
        self.assertTrue(first["started"])
        CS.RENDER["thread"].join(timeout=10)          # 自检模式，瞬间结束
        _ok, _c, again = CS.try_start_render()
        self.assertFalse(again["started"])
        self.assertTrue(again["already"])
        self.assertEqual(again["attempt"], 1, "重复请求不得新起任务")
        self.assertEqual(len(CS.RENDER["history"]), 1)

    # ---- 失败后允许重试，且确认结果保留
    def test_failure_allows_retry_without_reconfirming(self):
        self._confirm()
        CS.try_start_render()
        CS.RENDER["thread"].join(timeout=10)
        self.assertEqual(CS.RENDER["outcome"], "FAILED")
        self.assertEqual(CS.pending_items(), [], "重试不应要求重新标注")
        _ok, _c, retry = CS.try_start_render(retry=True)
        self.assertTrue(retry["started"], "失败后应允许显式重试")
        self.assertEqual(CS.RENDER["attempt"], 2)

    # ---- 5/6 只有 SUCCESS 可交付
    def test_only_success_is_shippable(self):
        self._confirm()
        for bad in ("REJECTED", "DIAGNOSTIC", "FAILED"):
            CS.RENDER["state"] = "done"
            CS.RENDER["outcome"] = bad
            st = CS.render_status()
            self.assertFalse(st["shippable"], "%s 不得被标为成品" % bad)
            self.assertNotEqual(st["state_label"], "成品完成")
            self.assertNotIn("✓", st["state_label"])
            _ok, _c, res = CS.try_start_render(retry=True)
            self.assertTrue(res["started"], "%s 之后应允许重试" % bad)
            CS.RENDER["thread"].join(timeout=10)
        CS.RENDER["state"] = "done"
        CS.RENDER["outcome"] = "SUCCESS"
        st = CS.render_status()
        self.assertTrue(st["shippable"])
        self.assertEqual(st["state_label"], "成品完成")
        # 成功之后不再重跑（避免无意义地再起一个 Blender）
        _ok, _c, res = CS.try_start_render(retry=True)
        self.assertFalse(res["started"])
        self.assertEqual(res["reason"], "already_succeeded")

    # ---- 自检模式绝不可能产出成品
    def test_self_test_can_never_succeed(self):
        self._confirm()
        CS.try_start_render()
        CS.RENDER["thread"].join(timeout=10)
        self.assertNotEqual(CS.RENDER["outcome"], "SUCCESS")


# =====================================================================================
#  二、job 校验（浏览器不能注入路径）
# =====================================================================================
class TestJobValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="_t_job_", dir=_TEST_TMP)
        self.old_root = CS.ROOT
        CS.ROOT = os.path.join(self.tmp, "models")
        os.makedirs(CS.ROOT, exist_ok=True)
        self.pmx = os.path.join(CS.ROOT, "fixture.pmx")
        with open(self.pmx, "wb") as f:
            f.write(b"synthetic-pmx-fixture")
        self.fp = CS.sha256_of(self.pmx)
        CS.STATE["models"] = {self.fp: {"fingerprint": self.fp, "file": "fixture.pmx"}}
        CS.STATE["order"] = [self.fp]
        CS.STATE["maps_dir"] = os.path.join(self.tmp, "maps")

    def tearDown(self):
        CS.ROOT = self.old_root
        _cleanup(self.tmp)

    def _job(self, targets):
        return {"targets": targets, "out_root": os.path.join(self.tmp, "out")}

    def test_unregistered_fingerprint_dropped(self):
        job = CS._validate_job(self._job([
            {"fingerprint": FP_B, "pmx": self.pmx, "label": "别人.pmx"}]))
        self.assertEqual(job["targets"], [], "未登记的指纹不得被执行")

    def test_hash_mismatch_dropped(self):
        job = CS._validate_job(self._job([
            {"fingerprint": self.fp, "pmx": os.path.join(CS.ROOT, "不存在.pmx")}]))
        self.assertEqual(job["targets"], [])

    def test_outside_root_dropped(self):
        outside = os.path.join(self.tmp, "outside.pmx")
        with open(outside, "wb") as f:
            f.write(b"synthetic-pmx-fixture")
        job = CS._validate_job(self._job([
            {"fingerprint": self.fp, "pmx": outside, "label": "越界"}]))
        self.assertEqual(job["targets"], [], "素材根目录之外的路径必须被丢弃")

    def test_valid_target_kept_and_out_confined(self):
        job = CS._validate_job(self._job([
            {"fingerprint": self.fp, "pmx": self.pmx, "label": "迪奥娜.pmx",
             "out": "C:/evil_out"}]))
        self.assertEqual(len(job["targets"]), 1)
        self.assertIsNone(job["targets"][0]["out"], "越界的输出目录必须被清空")

    def test_render_entry_never_reads_paths_from_body(self):
        """源码级守卫：/render 分支里不得出现从请求体取路径的写法。"""
        with io.open(os.path.join(V31, "_tools", "confirm_server.py"),
                     encoding="utf-8") as f:
            src = f.read()
        i = src.index("if action == \"render\":")
        seg = src[i:i + 400]
        for bad in ("pmx", "path", "out_dir", "cmd", "exec"):
            self.assertNotIn('payload.get("%s")' % bad, seg,
                             "/render 不得从请求体读取 %s" % bad)


# =====================================================================================
#  三、跨进程文件锁
# =====================================================================================
class TestRenderLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="_t_lock_", dir=_TEST_TMP)
        self.p = os.path.join(self.tmp, "a.render.lock")

    def tearDown(self):
        _cleanup(self.tmp)

    def test_second_acquire_is_refused(self):
        a = RenderLock(self.p, tag="a").acquire()
        try:
            with self.assertRaises(LockBusy):
                RenderLock(self.p, tag="b").acquire()
        finally:
            a.release()
        # 释放后可以再拿
        b = RenderLock(self.p, tag="b").acquire()
        b.release()

    def test_holder_recorded_for_forensics(self):
        a = RenderLock(self.p, tag="probe").acquire()
        try:
            with io.open(self.p, encoding="utf-8") as f:
                info = json.load(f)
            self.assertEqual(info["tag"], "probe")
            self.assertEqual(info["pid"], os.getpid())
        finally:
            a.release()

    def test_stale_lock_reclaimed(self):
        with io.open(self.p, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "tag": "dead", "ts": 0}, f)
        lk = RenderLock(self.p, tag="new", stale_sec=1)
        lk.acquire()
        lk.release()


# =====================================================================================
#  四、HTTP 层（真实起服务，自检模式：不启动 Blender）
# =====================================================================================
class TestHttpSurface(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="_t_http_", dir=_TEST_TMP)
        self.model_root = os.path.join(self.tmp, "models")
        os.makedirs(self.model_root, exist_ok=True)
        self.maps = os.path.join(self.tmp, "maps")
        os.makedirs(self.maps, exist_ok=True)
        self.pmx = os.path.join(self.model_root, "fixture.pmx")
        with open(self.pmx, "wb") as f:
            f.write(b"synthetic-http-pmx-fixture")
        self.fp = CS.sha256_of(self.pmx)
        self.conf = os.path.join(self.tmp, "%s_confirmation.json" % self.fp[:16])
        with io.open(self.conf, "w", encoding="utf-8") as f:
            json.dump({"schema": "toon-confirmation/1",
                       "model": {"file": "fixture.pmx", "path": self.pmx,
                                 "display_name": "测试", "fingerprint": "sha256:" + self.fp},
                       "items": [{"material": "测试材质", "index": 0, "stage": "SUGGESTED",
                                  "requires_user": True, "previews": {}, "choices": [],
                                  "reasons": []}]}, f, ensure_ascii=False)
        self.out_dir = os.path.join(self.tmp, "out")
        self.job = os.path.join(self.tmp, "job.json")
        with io.open(self.job, "w", encoding="utf-8") as f:
            json.dump({"schema": "toon-render-job/1", "auto_render": False,
                       "mode": "faithful", "out_root": self.tmp,
                       "maps_dir": self.maps,
                       "targets": [{"fingerprint": self.fp, "pmx": self.pmx,
                                    "label": "注册标签.pmx", "out": self.out_dir}]},
                      f, ensure_ascii=False)
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = subprocess.Popen(
            [PY, os.path.join(V31, "_tools", "confirm_server.py"),
             "--confirmation", self.conf, "--maps-dir", self.maps,
             "--model-root", self.model_root, "--runtime-dir", self.tmp,
             "--job", self.job, "--self-test", "--port", str(self.port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.sid = None
        t0 = time.time()
        while time.time() - t0 < 20:
            try:
                _c, d = http("GET", self.base + "/api/session", timeout=2)
                self.sid = d.get("session_id")
                if self.sid:
                    break
            except Exception:
                time.sleep(0.3)
        if not self.sid:
            self.proc.kill()
            self.fail("确认服务未启动")

    def tearDown(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        out = b""
        if self.proc.stdout:
            try:
                out = self.proc.stdout.read()
            except Exception:
                pass
            self.proc.stdout.close()
        if out and os.environ.get("TEST_SHOW_SERVER_LOG"):
            sys.stderr.write(out.decode("utf-8", "replace"))
        _cleanup(self.tmp)

    def test_unknown_session_id_rejected(self):
        code, _b = http("POST", self.base + "/api/session/deadbeef/render", {})
        self.assertEqual(code, 404)
        code, _b = http("GET", self.base + "/api/session/deadbeef/status")
        self.assertEqual(code, 404)

    def test_pending_blocks_and_open_output_blocked(self):
        code, body = http("POST", "%s/api/session/%s/render" % (self.base, self.sid), {})
        self.assertEqual(code, 409)
        self.assertIn("未确认", body["error"])
        code, body = http("POST", "%s/api/session/%s/open-output" % (self.base, self.sid), {})
        self.assertEqual(code, 409)

    def test_full_flow_single_task_and_no_path_injection(self):
        # 未确认
        _c, st = http("GET", "%s/api/session/%s/status" % (self.base, self.sid))
        self.assertEqual(st["state"], "waiting")
        self.assertEqual(st["pending"], 1)
        # 确认
        code, body = http("POST", self.base + "/api/answer",
                          {"model_fingerprint": self.fp, "material": "测试材质", "class": "cloth"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        _c, st = http("GET", "%s/api/session/%s/status" % (self.base, self.sid))
        self.assertIn(st["state"], ("ready", "rendering", "failed"))

        # 带着恶意路径字段重复点两次，且并发
        starts, results = [], []
        barrier = threading.Barrier(2)

        def go():
            barrier.wait()
            _c2, b2 = http("POST", "%s/api/session/%s/render" % (self.base, self.sid),
                           {"retry": False, "pmx": "C:/evil.pmx", "out": "C:/evil",
                            "cmd": "calc"})
            starts.append(bool(b2.get("started")))
            results.append(b2)

        ths = [threading.Thread(target=go) for _ in range(2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        self.assertEqual(sum(starts), 1, "两次点击只能起一个任务：%s" % starts)

        st = wait_terminal(self.base, self.sid)
        self.assertIsNotNone(st, "自检模式应当很快到达终态")
        self.assertEqual(st["attempt"], 1)
        # 浏览器传的路径全部无效：执行的仍是服务端登记的目标
        self.assertEqual(st["targets"][0]["label"], "注册标签.pmx")
        self.assertEqual(st["results"][0]["label"], "注册标签.pmx")
        self.assertNotIn("evil", json.dumps(st, ensure_ascii=False))
        # 自检恒为 FAILED，且绝不可交付
        self.assertEqual(st["state"], "failed")
        self.assertFalse(st["shippable"])
        self.assertNotEqual(st["state_label"], "成品完成")
        # 再次点击：仍是同一个任务
        _c, again = http("POST", "%s/api/session/%s/render" % (self.base, self.sid), {})
        self.assertFalse(again["started"])
        self.assertEqual(again["attempt"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
