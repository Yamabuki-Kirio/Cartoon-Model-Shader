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
