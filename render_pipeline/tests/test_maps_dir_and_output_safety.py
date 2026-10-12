# -*- coding: utf-8 -*-
"""
F6 / F7 回归：侧车映射目录贯穿 + 输出目录不可用的结构化错误。

背景（均为真机验收发现的缺陷）：
  F6  --maps-dir 只在「预检 + 确认服务」生效，渲染侧硬编码仓库内的
      model_material_maps/。后果：隔离目录形同虚设；用户确认结果到不了渲染侧；
      渲染过程把映射与「待填写」模板写进**仓库代码目录**（模板还被误标
      source=user_confirmed）。
  F7  ensure_previews() 从不创建 PREVIEW_DIR → 出厂首次运行预览必然派发失败；
      --out 指向已存在文件 / 不存在的盘符时抛裸 traceback，没有稳定错误码。

这里用两种手段守：
  ① 行为测试：能直接调的（ensure_out_dir / ensure_previews）真跑；
  ② 源码契约测试：跨进程派发的 argv 无法在单测里真跑，就断言"这条线必须存在"，
     防止有人把 --maps-dir 的传导或默认目录又改回仓库内。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
sys.path.insert(0, str(_PIPELINE))

ENTRY = _PIPELINE / "开始渲染.py"
DRIVER = _PIPELINE / "一键渲染_通用驱动.py"
MAIN_SCRIPT = _PIPELINE / "一键卡通渲染.py"
CONFIRM_SERVER = _PIPELINE / "_tools" / "confirm_server.py"
BUILD_CONF = _PIPELINE / "_tools" / "build_confirmation.py"


def _entry_module():
    """按路径加载 开始渲染.py（文件名非 ASCII，只能走 spec）。"""
    spec = importlib.util.spec_from_file_location("start_render_entry", ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _text(path):
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ F7 行为
class TestOutputDirStructuredError:
    """F7：输出目录不可用必须是结构化错误，不能是裸 traceback"""

    def test_existing_file_is_structured_error(self, tmp_path):
        m = _entry_module()
        f = tmp_path / "not_a_dir.txt"
        f.write_text("x", encoding="utf-8")
        ok, err = m.ensure_out_dir(str(f))
        assert ok is False
        assert err["code"] == "OUTPUT_NOT_WRITABLE"
        assert err["retryable"] is False
        assert err["message"] and err["hint"]

    def test_missing_drive_is_structured_error(self):
        m = _entry_module()
        ok, err = m.ensure_out_dir("Q:/__toon_no_such_drive__/out")
        assert ok is False
        assert err["code"] == "OUTPUT_NOT_WRITABLE"
        assert err["message"] and err["hint"]

    def test_normal_path_creates_nested_dirs(self, tmp_path):
        m = _entry_module()
        target = tmp_path / "a" / "b" / "c"
        ok, err = m.ensure_out_dir(str(target))
        assert (ok, err) == (True, None)
        assert target.is_dir()


# ------------------------------------------------------------------ F7 行为
class TestEnsurePreviewsCreatesDir:
    """F7：预览目录必须由 ensure_previews 自己创建（出厂首次运行靠它）"""

    def _conf(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"items": [{
            "material": "表情", "requires_user": True, "previews": {
                "mask_only": "m_mask.png",
                "overview_highlighted": "m_overview.png",
                "proposed_group": "m_proposed.png"}}]}), encoding="utf-8")
        return p

    def test_preview_dir_is_created_and_done_marker_is_not_in_repo(self, tmp_path, monkeypatch):
        m = _entry_module()
        previews = tmp_path / "runtime" / "confirmation" / "previews"
        monkeypatch.setattr(m, "PREVIEW_DIR", str(previews))
        monkeypatch.setattr(m, "BLENDER", "blender")
        monkeypatch.setattr(m, "SOURCE_BLEND", "src.blend")

        argv_seen = {}

        def fake_launch(argv, logp):
            argv_seen["argv"] = argv
            return {}          # 不含 @@LAUNCH@@ → 走「派发失败」分支，不进 600s 等待

        monkeypatch.setattr(m, "launch_blender", fake_launch)

        assert previews.is_dir() is False, "前置条件：目录一开始不存在"
        ok = m.ensure_previews("模型.pmx", str(self._conf(tmp_path)))

        assert previews.is_dir(), "★ ensure_previews 必须先创建 PREVIEW_DIR（F7）"
        assert ok is False, "stub 派发不成功，应返回 False"

        argv = argv_seen["argv"]
        done = argv[argv.index("--done") + 1]
        assert not str(done).startswith(str(_PIPELINE)), \
            "完成标记不得写进仓库代码目录（F6）：%s" % done
        assert str(previews) in done
        assert argv[argv.index("--out") + 1] == str(previews)


# ------------------------------------------------------------------ F6 契约
class TestMapsDirIsThreadedThrough:
    """F6：--maps-dir 必须贯穿 入口 → 确认服务 → 驱动 → 主脚本"""

    def test_entry_passes_maps_dir_to_driver(self):
        src = _text(ENTRY)
        assert '"--maps-dir", maps_dir or default_maps_dir()' in src
        assert "def render(pmx, out_dir, mode, auto_frame, maps_dir=None)" in src

    def test_entry_passes_maps_dir_to_confirm_server(self):
        assert '"--maps-dir", maps_dir or default_maps_dir()' in _text(ENTRY)

    def test_confirm_server_passes_maps_dir_to_driver(self):
        src = _text(CONFIRM_SERVER)
        assert '"--maps-dir"' in src
        assert 'job.get("maps_dir")' in src
        assert '"TOON_MAPS_DIR"' in src
        assert "default_maps_dir()" in src

    def test_driver_accepts_and_forwards_maps_dir(self):
        src = _text(DRIVER)
        assert 'MAPS_DIR = arg("--maps-dir")' in src
        assert 'os.environ["TOON_MAPS_DIR"] = MAPS_DIR' in src

    def test_main_script_reads_maps_dir_from_env(self):
        src = _text(MAIN_SCRIPT)
        assert 'MAPS_DIR = os.environ.get("TOON_MAPS_DIR") or MC.default_maps_dir()' in src

    def test_build_confirmation_accepts_maps_dir(self):
        src = _text(BUILD_CONF)
        assert 'ap.add_argument("--maps-dir"' in src


class TestRepoIsNeverWrittenByRun:
    """F6：禁止向仓库内 model_material_maps/ 写运行数据"""

    BANNED = (
        'os.path.join(V3_DIR, "model_material_maps")',
        'os.path.join(V31, "model_material_maps")',
        'os.path.join(HERE, "model_material_maps")',
        'os.path.join(_HERE, "model_material_maps")',
    )

    def test_no_source_file_hardcodes_repo_maps_dir(self):
        hits = []
        for path in _PIPELINE.rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = _text(path)
            for bad in self.BANNED:
                if bad in text:
                    hits.append("%s: %s" % (path.relative_to(_PIPELINE), bad))
        assert not hits, "仍把运行数据指向仓库内映射目录：" + "; ".join(hits)

    def test_template_is_written_to_output_dir_not_maps_dir(self):
        src = _text(MAIN_SCRIPT)
        # 模板落输出目录（运行数据），不再落 maps_dir
        assert '"%s.material-map.template.json" % fp.split(":", 1)[1][:16]' in src

    def test_provenance_records_the_actually_used_map(self):
        """F8：清单必须记「实际用到」的那份映射哈希，而不是恒 null"""
        src = _text(MAIN_SCRIPT)
        assert "RESOLVED_MAP_PATH" in src
        assert '"resolved_by": MAP_RESOLVED_BY' in src
        assert "_maps_dir_in_repo()" in src


# ------------------------------------------------------ provenance 路径脱敏
class TestProvenancePathRedaction:
    """清单/provenance 不得带用户名路径；默认映射目录以 %LOCALAPPDATA% 形式展示。"""

    def test_default_maps_dir_is_shown_as_localappdata(self):
        from material_classifier import default_maps_dir, display_path
        old = os.environ.pop("TOON_MAPS_DIR", None)
        try:
            shown = display_path(default_maps_dir())
        finally:
            if old is not None:
                os.environ["TOON_MAPS_DIR"] = old
        assert shown.startswith("%LOCALAPPDATA%" + os.sep), shown
        assert "CartoonModelShader" in shown, shown
        username = (os.environ.get("USERNAME") or "").strip()
        if username:
            assert username.lower() not in shown.lower(), shown

    def test_username_in_path_falls_back_to_basename(self):
        from material_classifier import display_path
        username = (os.environ.get("USERNAME") or "").strip()
        if not username:
            return  # 极少数无 USERNAME 的环境：本用例无意义
        fake = os.path.join("Q:", os.sep, "srv", username, "deep", "map.json")
        assert display_path(fake) == "map.json"

    def test_unrelated_path_is_kept(self):
        from material_classifier import display_path
        path = os.path.join("R:", os.sep, "assets", "model", "map.json")
        # 与用户名无关的工程盘路径原样保留（清单仍可定位到项目盘）
        assert display_path(path) == os.path.abspath(path)

    def test_none_and_empty_pass_through(self):
        from material_classifier import display_path
        assert display_path(None) is None
        assert display_path("") == ""

    def test_main_script_redacts_provenance_paths(self):
        src = _text(MAIN_SCRIPT)
        assert "MC.display_path(MAPS_DIR)" in src
        assert "MC.display_path(V3_DIR)" in src
        assert "MC.display_path(SOURCE_BLEND)" in src


# ------------------------------------------------------------------ 默认目录
class TestDefaultMapsDir:
    def test_default_is_user_data_not_repo(self):
        from material_classifier import default_maps_dir
        d = os.path.abspath(default_maps_dir())
        assert not d.startswith(os.path.abspath(str(_PIPELINE)) + os.sep), d
        assert d.endswith(os.path.join("CartoonModelShader", "model_material_maps")), d

    def test_entry_driver_confirm_share_the_same_default(self):
        from material_classifier import default_maps_dir
        old = os.environ.pop("TOON_MAPS_DIR", None)
        try:
            m = _entry_module()
            assert os.path.abspath(m.default_maps_dir()) == os.path.abspath(default_maps_dir())
        finally:
            if old is not None:
                os.environ["TOON_MAPS_DIR"] = old


# ------------------------------------------------------------------ F4 规则
class TestRulesVersionBumped:
    def test_rules_version_reflects_exclusion_support(self):
        from material_classifier import Rules
        r = Rules()
        got = tuple(int(x) for x in str(r.version).split("."))
        assert got >= (3, 1, 0), "unless 排除机制上线后规则版本必须提升：%s" % r.version
        eye = [x for x in r.semantic_rules if x["id"] == "eye"][0]
        assert eye["unless"] is not None


# ------------------------------------------------------------------ O2 终态措辞
class TestFinalLineMatchesStructuredOutcome:
    """末行必须与结构化终态一致：**只有 SUCCESS 才允许出现「完成」**。

    真机验收观察（O2）：`--out` 不可写时 `render()` 已返回 1，main 却仍无条件
    打印「[3/3] 完成（输出：…）」—— 末行与事实相反，逐字读到终端的人会被误导。
    """

    def _capture(self, m, outcome, out_dir, confirm_path=False):
        lines = []
        orig = m.out
        m.out = lambda *a: lines.append(" ".join(str(x) for x in a))
        try:
            m.report_final(outcome, out_dir, confirm_path=confirm_path)
        finally:
            m.out = orig
        return "\n".join(lines)

    def test_failure_never_says_completed(self, tmp_path):
        m = _entry_module()
        text = self._capture(m, {
            "status": "FAILED", "stage": "准备输出目录",
            "error": {"code": "OUTPUT_NOT_WRITABLE",
                      "message": "输出目录不可用：该路径已存在且不是一个目录。",
                      "hint": "换一个 --out 目录。"},
        }, str(tmp_path))
        assert "完成" not in text, text
        assert "OUTPUT_NOT_WRITABLE" in text, text
        assert "准备输出目录" in text, text

    def test_success_says_completed(self, tmp_path):
        m = _entry_module()
        text = self._capture(m, {"status": "SUCCESS", "token": "SUCCESS"}, str(tmp_path))
        assert "完成" in text, text
        assert str(tmp_path) in text, text

    def test_confirm_path_failure_points_at_retry(self, tmp_path):
        m = _entry_module()
        text = self._capture(m, {"status": "FAILED", "stage": "确认与渲染",
                                 "token": "REJECTED",
                                 "error": {"code": "REJECTED",
                                           "message": "终态 REJECTED"}},
                             str(tmp_path), confirm_path=True)
        assert "REJECTED" in text, text
        assert "确认与渲染" in text, text
        assert "重试渲染" in text, text
        assert "完成" not in text, text

    def test_render_returns_structured_outcome_when_out_unusable(self, tmp_path):
        """行为验证：输出目录不可用时 render() 直接返回 (1, 结构化终态)。"""
        m = _entry_module()
        f = tmp_path / "not_a_dir.txt"
        f.write_text("x", encoding="utf-8")
        rc, outcome = m.render(str(tmp_path / "m.pmx"), str(f), "faithful", False)
        assert rc == 1
        assert outcome["status"] == "FAILED"
        assert outcome["stage"] == "准备输出目录"
        assert outcome["error"]["code"] == "OUTPUT_NOT_WRITABLE"

    def test_main_delegates_final_line_to_report_final(self):
        """契约：main 不得自行拼接 [3/3] 末行 —— 一律交给 report_final。"""
        body = _text(ENTRY).split("def main(", 1)[1]
        assert "[3/3]" not in body, "main 里出现了自拼的 [3/3] 末行"
        assert "report_final(" in body


# ------------------------------------------------------------------ O4 逻辑路径
class TestRenderStatusLogicalPaths:
    """O4：确认服务对外只给逻辑路径，真实绝对路径留在服务端内部。"""

    @staticmethod
    def _load_confirm_server():
        spec = importlib.util.spec_from_file_location("confirm_server_under_test", CONFIRM_SERVER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_out_root_targets_and_results_are_logical(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        mod = self._load_confirm_server()

        root = str(tmp_path / "runtime" / "output")
        out_dir = os.path.join(root, "m_1011")
        monkeypatch.setitem(mod.RENDER, "job", {
            "out_root": root,
            "targets": [{"label": "m.pmx", "out": out_dir}],
        })
        monkeypatch.setitem(mod.RENDER, "results", [
            {"label": "m.pmx", "outcome": "SUCCESS", "out_dir": out_dir,
             "map_file": os.path.join(str(tmp_path), "maps", "abc.material-map.json")},
        ])

        st = mod.render_status()
        child = "<OUTPUT_DIR>" + os.sep + "m_1011"
        assert st["out_root"] == "<OUTPUT_DIR>"
        assert st["targets"][0]["out"] == child
        assert st["results"][0]["out_dir"] == child
        assert st["outputs"] == [child]
        # 用户名 / 绝对路径不得到响应里
        assert str(tmp_path) not in repr(st)
        assert "USERPROFILE" not in repr(st) or "%USERPROFILE%" in repr(st)

    def test_map_file_is_logical_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        mod = self._load_confirm_server()
        root = str(tmp_path / "runtime" / "output")
        map_file = os.path.join(str(tmp_path), "maps", "x.material-map.json")
        monkeypatch.setitem(mod.RENDER, "job", {"out_root": root, "targets": []})
        monkeypatch.setitem(mod.RENDER, "results", [
            {"label": "m.pmx", "outcome": "SUCCESS", "out_dir": "", "map_file": map_file},
        ])
        st = mod.render_status()
        assert st["results"][0]["map_file"].startswith("%LOCALAPPDATA%")
        assert str(tmp_path) not in repr(st)

