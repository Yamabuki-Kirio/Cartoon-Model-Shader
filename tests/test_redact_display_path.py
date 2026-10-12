"""O4：对外只给**逻辑路径**，服务端内部仍持有真实绝对路径。

背景（真机验收 O4/F10）：接口会回传含用户名的绝对路径 —— 例如
`render.out_root` / `targets[].out`、场景探针的 `blender.file_path`、
输出目录不可用的错误 `details.out_dir`。这些字段页面与日志都会读到，
而路径本身在**展示层**没有信息量（用户名尤其不该出去）。

约定（见 `docs/接口文档.md` §1.3）：

* 用户目录下 ⇒ `%LOCALAPPDATA%\\…` / `%APPDATA%\\…` / `%USERPROFILE%\\…`（更具体的优先）；
* 给了渲染输出根 ⇒ `<OUTPUT_DIR>[\\子路径]`；
* 仍带当前用户名 ⇒ 只留文件名；
* 与用户名无关的路径 ⇒ 原样保留；
* **例外**：`应用到工程` 的确认载荷保留真实绝对路径（覆盖前必须看清写到哪）。
"""

from __future__ import annotations

import os

import pytest

from src.server import scene_probe
from src.server.redact import display_path, redact_value
from src.server.session import _project_dirty_summary


# --------------------------------------------------------------------- display_path

class TestDisplayPathUserDirs:
    def test_localappdata_becomes_placeholder(self, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
        monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")
        monkeypatch.setenv("USERPROFILE", r"C:\Users\someone")
        got = display_path(r"C:\Users\someone\AppData\Local\CartoonModelShader\model_material_maps")
        assert got == r"%LOCALAPPDATA%\CartoonModelShader\model_material_maps"
        assert "someone" not in got

    def test_more_specific_placeholder_wins(self, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
        monkeypatch.setenv("USERPROFILE", r"C:\Users\someone")
        got = display_path(r"C:\Users\someone\AppData\Local\Temp\x")
        assert got.startswith("%LOCALAPPDATA%")
        assert "%USERPROFILE%" not in got

    def test_username_fallback_keeps_only_basename(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.setenv("USERNAME", "someone")
        got = display_path(r"D:\mine\someone\proj\a.blend")
        assert got == "a.blend"
        assert "someone" not in got

    def test_unrelated_path_passes_through(self, monkeypatch):
        monkeypatch.setenv("USERNAME", "someone")
        got = display_path(r"A:\B-Model\1009\model.pmx")
        assert got == os.path.abspath(r"A:\B-Model\1009\model.pmx")

    def test_none_and_empty_pass_through(self):
        assert display_path(None) is None
        assert display_path("") == ""


class TestDisplayPathOutputRoot:
    def test_root_becomes_output_dir(self):
        root = r"C:\tmp\run\output"
        assert display_path(root, root=root) == "<OUTPUT_DIR>"

    def test_child_of_root_is_relative(self):
        root = r"C:\tmp\run\output"
        got = display_path(os.path.join(root, "model_1011"), root=root)
        assert got == "<OUTPUT_DIR>" + os.sep + "model_1011"

    def test_sibling_is_not_mistaken_for_child(self):
        root = r"C:\tmp\run\output"
        got = display_path(r"C:\tmp\run\output-other\x", root=root)
        assert got != "<OUTPUT_DIR>" + os.sep + "x"
        assert not got.startswith("<OUTPUT_DIR>")

    def test_root_takes_priority_over_user_placeholder(self, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
        root = r"C:\Users\someone\AppData\Local\Temp\run\output"
        assert display_path(root, root=root) == "<OUTPUT_DIR>"


def test_redact_value_still_masks_paths_in_free_text():
    """老能力没被破坏：自由文本里的路径仍然只留 `<path>`。"""
    text = redact_value({"reason": r"failed at C:\Users\someone\proj\a.blend"})
    assert text["reason"] == "failed at <path>"


# --------------------------------------------------- 服务端应用的脱敏点

class TestSceneProbeRedactsFilePath:
    def test_normalized_file_path_is_logical(self, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
        monkeypatch.setenv("USERPROFILE", r"C:\Users\someone")
        payload = {
            "protocol": "toon-tuner-scene-probe/1",
            "blender": {
                "version": "5.2.1 LTS",
                "file_path": r"C:\Users\someone\AppData\Local\Temp\proj\成品.blend",
                "is_saved": True,
            },
            "scene": {"name": "Scene"},
            "objects": {},
        }
        got = scene_probe.normalize_probe(payload)
        assert got["blender"]["file_name"] == "成品.blend"
        assert got["blender"]["file_path"].startswith("%LOCALAPPDATA%")
        assert "someone" not in got["blender"]["file_path"]


class TestProjectDirtySummary:
    """O3 的后端判据：只有「基线干净 → 预览后变脏」才该提示。"""

    def test_flagged_only_when_baseline_was_clean(self):
        got = _project_dirty_summary(False, {"is_dirty": True, "file_name": "a.blend"})
        assert got["dirty_flagged"] is True
        assert got["dirty_at_baseline"] is False
        assert got["dirty_after_preview"] is True
        assert got["file_name"] == "a.blend"

    def test_already_dirty_is_not_flagged(self):
        got = _project_dirty_summary(True, {"is_dirty": True})
        assert got["dirty_flagged"] is False

    def test_still_clean_is_not_flagged(self):
        got = _project_dirty_summary(False, {"is_dirty": False})
        assert got["dirty_flagged"] is False

    def test_missing_state_never_crashes_and_never_flags(self):
        got = _project_dirty_summary(False, None)
        assert got["dirty_flagged"] is False
        assert got["dirty_after_preview"] is None
        assert got["file_name"] is None

    def test_never_carries_absolute_path(self):
        got = _project_dirty_summary(
            False, {"is_dirty": True, "file_name": "a.blend", "filepath": r"C:\Users\x\a.blend"}
        )
        assert set(got) == {"dirty_at_baseline", "dirty_after_preview", "dirty_flagged", "file_name"}
        assert "C:" not in repr(got)
