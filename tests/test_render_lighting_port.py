"""体积雾灯光与辉光增量的移植守卫（2026-10-11）。

主脚本 / 驱动只能在 Blender 里完整执行，pytest 环境跑不了它们，因此这里用
**源码级契约**守住移植的关键连接点：

* 主脚本：灯光配置块、``setup_lights`` 调度器、GLOW 修复默认值、manifest 记录；
* 驱动：``--lighting`` / ``--fog-density`` / 辉光开关与环境变量贯穿；
* 入口脚本与确认服务：确认路径（job 文件 → 自动续跑）的参数透传；
* 服务端适配层：白名单与命令行透传。

运行期的真实验证由真机验收覆盖（91.blend 严格 SUCCESS 样本）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "render_pipeline"
MAIN = PIPELINE / "一键卡通渲染.py"
DRIVER = PIPELINE / "一键渲染_通用驱动.py"
ENTRY = PIPELINE / "开始渲染.py"
CONFIRM = PIPELINE / "_tools" / "confirm_server.py"
ADAPTER = ROOT / "src" / "server" / "render_pipeline_adapter.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_main_script_has_lighting_config_and_dispatcher() -> None:
    text = _text(MAIN)
    assert 'LIGHTING_PROFILE = os.environ.get("TOON_LIGHTING_PROFILE", "volume_fog")' in text
    assert 'VOLUME_FOG_DENSITY = float(os.environ.get("TOON_FOG_DENSITY", "0.018"))' in text
    assert 'VOLUME_FOG_COLLECTION = "TOON_体积雾灯光组"' in text
    assert 'VOLUME_FOG_CONTROLLER = "TOON_灯光总控"' in text
    assert "def setup_volumetric_lights(scene, targets):" in text
    assert "def setup_legacy_lights(scene):" in text
    # 调度器：legacy 走旧灯函数；volume_fog 走体积雾函数；其余值显式报错。
    assert 'if LIGHTING_PROFILE == "legacy":' in text
    assert 'raise ValueError("LIGHTING_PROFILE 必须是 volume_fog 或 legacy' in text
    # 调用点必须把 targets 传进去（体积雾方案按模型包围盒缩放）。
    assert "lights = setup_lights(scene, targets)" in text


def test_main_script_glow_defaults_are_fixed_values() -> None:
    text = _text(MAIN)
    m = re.search(r"GLOW = \{[^}]*\}", text)
    assert m, "应能抓到 GLOW 字典"
    glow = m.group(0)
    for fragment in ('"Type": "Bloom"', '"Quality": "High"', '"Threshold": 0.55',
                     '"Smoothness": 0.35', '"Strength": 0.85', '"Size": 0.62',
                     '"Maximum": 8.0'):
        assert fragment in glow, fragment
    # 饱和度按用户裁决不移植（新版报告原值 1.08）。
    assert "Saturation" not in glow
    # Type / Quality 必须从 GLOW 字典取（旧代码硬编码 Medium，改了字典不生效）。
    assert '("Type", GLOW["Type"]), ("Quality", GLOW["Quality"])' in text
    assert '("glare_type", GLOW["Type"].upper()), ("quality", GLOW["Quality"].upper())' in text


def test_main_script_manifest_records_lighting() -> None:
    text = _text(MAIN)
    assert '"lighting_profile": LIGHTING_PROFILE,' in text
    assert '"volume_fog_density": VOLUME_FOG_DENSITY,' in text


def test_driver_exposes_lighting_and_glow_flags() -> None:
    text = _text(DRIVER)
    assert 'LIGHTING = (arg("--lighting", "volume_fog")' in text
    assert 'FOG_DENSITY = float(arg("--fog-density", "0.018"))' in text
    assert 'raise SystemExit("--lighting 必须是 volume_fog 或 legacy' in text
    assert "0.0 <= FOG_DENSITY <= 0.1" in text
    # 环境变量必须在 exec 主脚本之前设好（主脚本顶部从环境变量取值）。
    assert 'os.environ["TOON_LIGHTING_PROFILE"] = LIGHTING' in text
    assert 'os.environ["TOON_FOG_DENSITY"] = str(FOG_DENSITY)' in text
    for flag in ("--glow-smoothness", "--glow-quality"):
        assert flag in text
    assert 'ns["GLOW"]["Smoothness"] = glow_over["Smoothness"] = float(GLOW_SMOOTHNESS)' in text
    assert 'ns["GLOW"]["Quality"] = glow_over["Quality"] = GLOW_QUALITY' in text


def test_entry_and_confirm_paths_pass_lighting_through() -> None:
    entry = _text(ENTRY)
    assert 'ap.add_argument("--lighting"' in entry
    assert '"--lighting", lighting,' in entry
    assert '"fog_density": fog_density,' in entry  # job 文件（确认路径）
    confirm = _text(CONFIRM)
    assert '"--lighting", job.get("lighting") or "volume_fog",' in confirm
    assert '"--fog-density"' in confirm


def test_adapter_whitelist_and_cmd_include_lighting() -> None:
    text = _text(ADAPTER)
    assert '"lighting", "fog_density"' in text
    assert 'str(job.settings.get("lighting", "volume_fog"))' in text
    assert 'str(job.settings.get("fog_density", 0.018))' in text
