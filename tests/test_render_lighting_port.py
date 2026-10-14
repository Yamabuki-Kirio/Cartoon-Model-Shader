"""渲染方案移植守卫（PR #8 重写版）。

主脚本 / 驱动只能在 Blender 里完整执行，pytest 环境跑不了它们，因此这里用
**源码级契约**守住方案系统的关键连接点：

* 主脚本：从环境变量解析固定方案、``setup_lights`` 走注册表、辉光修复默认值、
  manifest 记录 ``render_scheme``；
* 驱动：``--scheme`` / ``--scheme-params`` / ``--scheme-param``，旧
  ``--lighting`` / ``--fog-density`` 仅作弃用别名；
* 入口脚本与确认服务：确认路径（job 文件 → 自动续跑）的方案透传；
* 服务端适配层：白名单与命令行透传只走固定方案 id。

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


# -- 主脚本 ----------------------------------------------------------------


def test_main_script_resolves_scheme_from_env() -> None:
    """灯光方案不再用 if/else 分支：改成从环境变量解析固定方案 id。"""
    text = _text(MAIN)
    # 环境变量契约（驱动写入、主脚本读取）
    assert 'TOON_SCHEME' in text
    assert 'TOON_SCHEME_PARAMS' in text
    assert 'os.environ.get("TOON_SCHEME")' in text
    # 旧字段保留为弃用别名，由注册表统一翻译
    assert 'TOON_LIGHTING_PROFILE' in text
    # 方案由注册表解析，不在主脚本里硬编码分支
    assert "def _resolve_scheme_from_env(" in text
    assert "resolve_scheme_id" in text
    # 体积雾方案的资源名仍是主脚本常量（供旧排查工具识别）
    assert 'VOLUME_FOG_COLLECTION = "TOON_体积雾灯光组"' in text
    assert 'VOLUME_FOG_CONTROLLER = "TOON_灯光总控"' in text


def test_main_script_dispatches_through_registry() -> None:
    """``setup_lights`` 必须走注册表 apply，而不是本地 if/else 调度。"""
    text = _text(MAIN)
    assert "def setup_lights(scene, targets=None):" in text
    # 调用点必须把 targets 传进去（体积雾方案按模型包围盒缩放）。
    assert "lights = setup_lights(scene, targets)" in text
    # 执行体走固定方案类
    assert "SCHEME_CLASS" in text
    assert "scheme.apply(scene" in text
    # 旧的本地调度与裸 ValueError 必须消失（已迁到注册表）
    assert 'if LIGHTING_PROFILE == "legacy":' not in text
    assert 'raise ValueError("LIGHTING_PROFILE 必须是 volume_fog 或 legacy' not in text


def test_main_script_restores_scene_after_render() -> None:
    """规格 §四：渲染结束必须恢复场景，并保留稳定错误码。"""
    text = _text(MAIN)
    assert "def restore_scene_scheme(" in text
    assert "restore_scheme" in text
    assert "RENDER_SCHEME_RESTORE_FAILED" in text
    # 恢复结果进 manifest，但**不**改渲染终态
    assert "SCENE_RESTORE_STATE" in text
    assert '"scene_restore"' in text


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


def test_main_script_manifest_records_scheme() -> None:
    """规格 §七：run_manifest 增加 render_scheme，并保留光影旧字段。"""
    text = _text(MAIN)
    assert '"render_scheme": _render_scheme_block(),' in text
    assert "def _render_scheme_block():" in text
    # 旧字段仍写出（兼容排查工具），但取值改为从方案派生
    assert '"lighting_profile":' in text
    assert '"volume_fog_density":' in text


# -- 驱动 ------------------------------------------------------------------


def test_driver_exposes_scheme_flags() -> None:
    text = _text(DRIVER)
    assert 'SCHEME = arg("--scheme")' in text
    assert '--scheme-params' in text
    assert '--scheme-param' in text
    # 旧参数保留但只作弃用别名
    assert 'LIGHTING = arg("--lighting")' in text
    assert 'FOG_DENSITY = arg("--fog-density")' in text
    assert "旧参数（弃用）" in text
    # 归一必须走注册表
    assert "SCHEME_REG.resolve_scheme_id(" in text
    assert "SCHEME_REG.normalize_parameters(" in text
    # 环境变量必须在 exec 主脚本之前设好（主脚本顶部从环境变量取值）。
    assert 'os.environ["TOON_SCHEME"] = SCHEME_ID' in text
    assert 'os.environ["TOON_SCHEME_PARAMS"] = json.dumps(SCHEME_PARAMS' in text


def test_driver_conflict_rejects_new_and_old() -> None:
    """新旧同时出现且语义冲突时必须拒绝，而不是静默取其一。"""
    text = _text(DRIVER)
    assert "RENDER_SCHEME_PARAM_INVALID" in text
    assert "只适用于 volume_fog 方案" in text
    assert "新旧雾密度冲突" in text


def test_driver_exposes_glow_flags() -> None:
    text = _text(DRIVER)
    for flag in ("--glow-smoothness", "--glow-quality"):
        assert flag in text
    assert 'ns["GLOW"]["Smoothness"] = glow_over["Smoothness"] = float(GLOW_SMOOTHNESS)' in text
    assert 'ns["GLOW"]["Quality"] = glow_over["Quality"] = GLOW_QUALITY' in text


# -- 入口脚本与确认服务 ----------------------------------------------------


def test_entry_passes_scheme_through() -> None:
    entry = _text(ENTRY)
    assert 'ap.add_argument("--scheme"' in entry
    assert 'ap.add_argument("--scheme-param"' in entry
    assert 'ap.add_argument("--scheme-params"' in entry
    # 调驱动时优先用结构化方案参数
    assert '"--scheme", scheme,' in entry
    assert '"--scheme-params"' in entry
    # job 文件（确认路径）记录方案
    assert '"scheme": scheme,' in entry
    assert '"fog_density": params.get("fog_density", 0.018),' in entry
    # 旧字段保留但默认 None（弃用），不再作为主路径
    assert 'ap.add_argument("--lighting", default=None' in entry
    assert 'ap.add_argument("--fog-density", type=float, default=None' in entry


def test_confirm_server_resumes_with_scheme() -> None:
    confirm = _text(CONFIRM)
    # 续跑优先读 job 文件的结构化方案字段
    assert '_scheme = job.get("scheme")' in confirm
    assert '_scheme_args = ["--scheme", str(_scheme)]' in confirm
    # 仅在旧 schema（无 scheme）时兜底用 --lighting / --fog-density
    assert '"--lighting", job.get("lighting") or "volume_fog",' in confirm
    assert '"--fog-density"' in confirm


# -- 服务端适配层 ----------------------------------------------------------


def test_adapter_whitelist_and_cmd_use_scheme() -> None:
    text = _text(ADAPTER)
    # 白名单：新增 scheme / scheme_parameters
    assert '"scheme", "scheme_parameters"' in text
    # 下发命令行改走固定方案
    assert '"--scheme",' in text
    assert '"--scheme-params",' in text
    # 只允许固定方案 id（再挡一次，防注入）
    assert "def _quote_scheme_id(" in text
    # 不再下发旧的 --lighting / --fog-density
    assert '"--lighting",' not in text
    assert '"--fog-density",' not in text


def test_adapter_scheme_id_is_strictly_whitelisted() -> None:
    """规格 §一：禁止浏览器提交脚本路径 / 模块路径 / 命令片段。"""
    from src.server.render_pipeline_adapter import _quote_scheme_id

    assert _quote_scheme_id("classic") == "classic"
    assert _quote_scheme_id("volume_fog") == "volume_fog"
    for bad in ("/evil.py", "os.system", "a;b", "../../x", "CLASSIC", ""):
        try:
            _quote_scheme_id(bad)
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "code", None) == "RENDER_SCHEME_INVALID", bad
        else:
            raise AssertionError("应拒绝非白名单方案 id：%r" % bad)
