"""材质准入口径（strict / diagnostic）Web 暴露的守卫测试（2026-10-14）。

背景：驱动与主脚本早就支持 ``--material-policy strict|diagnostic``，但入口脚本
直渲路径、确认服务自动续跑、适配层白名单全部**硬编码 strict** —— Web 用户选不到
宽松诊断模式。本文件守住步骤 3 的连接点：

* 入口脚本：``--material-policy`` 参数、``render()``/``write_job()`` 透传；
* 确认服务：自动续跑从 job 文件读口径（不再硬编码）；
* 适配层：白名单、校验、命令行透传；
* 前端：类型 / store / 向导选择器（源码契约）。

真机口径（2026-10-14 凌晨 91.blend 三连跑）：strict 无映射 → REJECTED；
diagnostic → DIAGNOSTIC（洋红棋盘、不可交付）；strict + 侧车映射 → SUCCESS。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "render_pipeline"
ENTRY = PIPELINE / "开始渲染.py"
CONFIRM = PIPELINE / "_tools" / "confirm_server.py"
ADAPTER = ROOT / "src" / "server" / "render_pipeline_adapter.py"
WEB_TYPES = ROOT / "web" / "src" / "api" / "render-types.ts"
WEB_STORE = ROOT / "web" / "src" / "state" / "render-store.ts"
WEB_WIZARD = ROOT / "web" / "src" / "features" / "render" / "RenderWizard.tsx"


def _load_entry():
    spec = importlib.util.spec_from_file_location("toon_entry_policy", ENTRY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fake_launch_factory(captured: dict, tmp_path: Path):
    """记录 argv 并立即写 SUCCESS 完成标记，让 render() 的轮询立刻结束。"""

    def fake_launch(argv, log_path):
        captured["argv"] = argv
        with open(argv[argv.index("--done") + 1], "w", encoding="utf-8") as f:
            f.write("SUCCESS\n")
        return {"__raw__": '@@LAUNCH@@{"pid": 1}'}

    return fake_launch


# -- 入口脚本：单元级 ---------------------------------------------------------


def test_render_honors_diagnostic_policy(tmp_path: Path, monkeypatch) -> None:
    """diagnostic 必须原样进驱动 argv（过去这里硬编码 strict）。"""
    mod = _load_entry()
    model = tmp_path / "m.pmx"
    model.write_bytes(b"PMX ")
    source = tmp_path / "source.blend"
    source.write_bytes(b"BLENDER-source")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    mod.LOCK_DIR = str(tmp_path / ".locks")
    mod.BLENDER = "blender"
    mod.SOURCE_BLEND = str(source)

    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(mod, "launch_blender", _fake_launch_factory(captured, tmp_path))
    rc, outcome = mod.render(str(model), str(out_dir), "faithful", False,
                             str(tmp_path / "maps"), policy="diagnostic")
    assert rc == 0
    assert outcome["status"] == "SUCCESS"
    argv = captured["argv"]
    assert argv[argv.index("--material-policy") + 1] == "diagnostic"


def test_render_defaults_to_strict_policy(tmp_path: Path, monkeypatch) -> None:
    mod = _load_entry()
    model = tmp_path / "m.pmx"
    model.write_bytes(b"PMX ")
    source = tmp_path / "source.blend"
    source.write_bytes(b"BLENDER-source")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    mod.LOCK_DIR = str(tmp_path / ".locks")
    mod.BLENDER = "blender"
    mod.SOURCE_BLEND = str(source)

    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(mod, "launch_blender", _fake_launch_factory(captured, tmp_path))
    rc, _outcome = mod.render(str(model), str(out_dir), "faithful", False,
                              str(tmp_path / "maps"))
    assert rc == 0
    argv = captured["argv"]
    assert argv[argv.index("--material-policy") + 1] == "strict"


def test_write_job_records_material_policy(tmp_path: Path) -> None:
    mod = _load_entry()
    model = tmp_path / "scene.blend"
    model.write_bytes(b"BLENDER-scene")
    mod.JOB_DIR = str(tmp_path / "jobs")
    mod.BLENDER = "blender"
    mod.SOURCE_BLEND = ""
    job_path = mod.write_job(str(model), str(tmp_path / "out"), "faithful", False,
                             str(tmp_path / "maps"), policy="diagnostic")
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    assert job["material_policy"] == "diagnostic"
    assert job["model_kind"] == "blend"


# -- 适配层：白名单与校验 -----------------------------------------------------


def test_adapter_validate_settings_policy() -> None:
    from src.server.render_pipeline_adapter import validate_settings
    from src.server import errors

    assert validate_settings({})["material_policy"] == "strict"
    assert validate_settings({"material_policy": "diagnostic"})["material_policy"] == "diagnostic"
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_settings({"material_policy": "loose"})
    assert excinfo.value.code == errors.PARAM_INVALID


def test_settings_request_accepts_policy() -> None:
    """Pydantic 请求模型必须收 material_policy（extra=forbid 下漏字段会 422）。"""
    from src.server.models import RenderSettingsRequest

    body = RenderSettingsRequest(model_id="model-x", material_policy="diagnostic")
    assert body.material_policy == "diagnostic"
    assert RenderSettingsRequest(model_id="model-x").material_policy == "strict"


# -- 源码级契约 ---------------------------------------------------------------


def test_entry_source_threads_policy_through() -> None:
    text = ENTRY.read_text(encoding="utf-8")
    assert 'ap.add_argument("--material-policy", default="strict",' in text
    assert 'choices=("strict", "diagnostic"),' in text
    # 直渲 argv 用参数而不是硬编码
    assert '"--material-policy", policy,' in text
    # job 文件与确认路径透传
    assert '"material_policy": policy,' in text
    assert "policy=a.material_policy" in text


def test_confirm_server_reads_policy_from_job() -> None:
    text = CONFIRM.read_text(encoding="utf-8")
    assert '"--material-policy", job.get("material_policy") or "strict",' in text
    assert '"material_policy": "strict",' in text  # _derive_job_from_conf 默认口径


def test_adapter_source_passes_policy_flag() -> None:
    text = ADAPTER.read_text(encoding="utf-8")
    assert '"material_policy"' in text
    assert '"--material-policy",' in text
    assert 'str(job.settings.get("material_policy", "strict"))' in text


def test_frontend_exposes_policy_selector() -> None:
    types = WEB_TYPES.read_text(encoding="utf-8")
    assert 'material_policy?: "strict" | "diagnostic"' in types
    store = WEB_STORE.read_text(encoding="utf-8")
    assert 'materialPolicy: "strict" | "diagnostic"' in store
    assert "material_policy: state.materialPolicy" in store
    assert "setMaterialPolicy(" in store
    wizard = WEB_WIZARD.read_text(encoding="utf-8")
    assert 'data-testid="render-material-policy"' in wizard
    assert "setMaterialPolicy(" in wizard
