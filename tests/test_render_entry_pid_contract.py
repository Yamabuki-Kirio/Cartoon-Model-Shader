"""固定入口脚本（render_pipeline/开始渲染.py）的 PID 上报契约测试。

契约：入口脚本经 MCP 派发真实 Blender 后，**必须**把真实 PID 打印为
``@@BLENDER_PID@@{"pid": N}``；取不到时打印 ``{"pid": null}``（调用方据此告警）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRY = REPO_ROOT / "render_pipeline" / "开始渲染.py"


def _load_entry():
    spec = importlib.util.spec_from_file_location("toon_entry_render", ENTRY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_report_blender_pid_parses_and_prints(capsys) -> None:
    mod = _load_entry()
    response = {"__raw__": "noise @@LAUNCH@@{\"pid\": 4321} trailing"}
    mod.report_blender_pid(response)
    captured = capsys.readouterr().out
    assert '@@BLENDER_PID@@{"pid": 4321}' in captured


def test_report_blender_pid_null_when_absent(capsys) -> None:
    mod = _load_entry()
    mod.report_blender_pid({"__error__": "timeout"})
    captured = capsys.readouterr().out
    # 取不到 PID 时必须显式 null，而不是静默不打印
    assert '@@BLENDER_PID@@{"pid": null}' in captured


def test_report_blender_pid_accepts_string_response(capsys) -> None:
    mod = _load_entry()
    mod.report_blender_pid('prefix @@LAUNCH@@{"pid": 99}\nmore')
    assert '@@BLENDER_PID@@{"pid": 99}' in capsys.readouterr().out


def test_launch_blender_prints_pid_from_mock_mcp(monkeypatch, capsys) -> None:
    """launch_blender 必须把 MCP 响应里的 PID 转发到 stdout（不是只检查存在）。"""
    mod = _load_entry()

    def fake_send(cmd, timeout=60):
        return {"__raw__": '@@LAUNCH@@{"pid": 5555}'}

    monkeypatch.setattr(mod, "send", fake_send)
    mod.launch_blender(["blender", "--background"], "/tmp/x.log")
    assert '@@BLENDER_PID@@{"pid": 5555}' in capsys.readouterr().out


def test_entry_source_has_no_legacy_marker() -> None:
    """入口脚本必须已改为上报 @@BLENDER_PID@@；不得只依赖 @@LAUNCH@@ 的存在性检查。"""
    text = ENTRY.read_text(encoding="utf-8")
    assert "@@BLENDER_PID@@" in text
    assert "report_blender_pid" in text
