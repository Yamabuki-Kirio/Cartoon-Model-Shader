"""MVP-03：本地预设存储、校验与 API 用例。

覆盖三块：
1. **存储位置与文件名** —— ``%LOCALAPPDATA%`` 解析、环境变量覆盖、文件名净化；
2. **预设内容** —— schema 校验、参数白名单、敏感内容拒绝、加载成草稿；
3. **存储与 API** —— 保存/读取/重命名/复制/删除、错误码、响应不泄漏本机路径。

CI 配置守卫（``.github/workflows/test.yml`` 必须满足的那几条）单独放在
``tests/test_ci_workflow.py``，与工作流文件同批提交。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.server import errors, presets
from src.server.app import create_app
from src.server.config import AppConfig, BlenderMCPConfig, ServerConfig
from tests.support import authed, unauthed

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 一份（虚构的）能力表：AgX 与 Standard 的合法 look 不同，用于验证依赖枚举
LOOK_MAP = {
    "AgX": [
        {"value": "None", "label": "None"},
        {"value": "AgX - High Contrast", "label": "AgX - High Contrast"},
        {"value": "AgX - Punchy", "label": "AgX - Punchy"},
    ],
    "Standard": [
        {"value": "None", "label": "None"},
        {"value": "High Contrast", "label": "High Contrast"},
    ],
}


# =============================================================================
#  夹具
# =============================================================================


@pytest.fixture()
def store(tmp_path: Path) -> presets.PresetStore:
    return presets.PresetStore(tmp_path / "presets")


@pytest.fixture()
def client(store: presets.PresetStore) -> TestClient:
    """预设接口**不需要** Blender：这里指向一个必然连不上的端口，证明这一点。

    自 v3.1 起**所有写接口**统一要求 ``X-Toon-Tuner-Token``，因此这里经
    ``tests.support.authed`` 构造客户端（令牌只在这一处注入）。
    拒绝路径由 ``test_preset_writes_require_session_token`` 显式覆盖。
    """
    config = AppConfig(blender_mcp=BlenderMCPConfig(port=1), server=ServerConfig())
    app = create_app(config, preset_store=store)
    with authed(app) as test_client:
        yield test_client


def test_preset_writes_require_session_token(store: presets.PresetStore) -> None:
    """预设是写接口，必须和其余写接口一样受令牌闸门保护；只读接口不受影响。"""
    config = AppConfig(blender_mcp=BlenderMCPConfig(port=1), server=ServerConfig())
    app = create_app(config, preset_store=store)

    with unauthed(app) as anon:
        assert anon.post("/api/presets", json=body()).status_code == 401
        assert anon.put("/api/presets/zzz", json=body()).status_code == 401
        assert anon.post("/api/presets/zzz/rename", json={"name": "x"}).status_code == 401
        assert anon.post("/api/presets/zzz/duplicate", json={}).status_code == 401
        assert anon.delete("/api/presets/zzz").status_code == 401
        # 只读接口保持开放
        assert anon.get("/api/presets").status_code == 200


def body(name: str = "测试预设", **overrides) -> dict:
    payload = {
        "name": name,
        "parameters": {
            "color.exposure": {"configured_value": 0.2, "effective_value": 0.2, "active": True}
        },
    }
    payload.update(overrides)
    return payload


def preset_from(parameters: dict, **overrides) -> dict:
    raw = {"name": "中间预设", "parameters": parameters}
    raw.update(overrides)
    return presets.normalize_preset(raw)


# =============================================================================
#  1. 存储位置
# =============================================================================


def test_presets_dir_uses_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(presets.ENV_PRESET_DIR, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    expected = tmp_path / "Local" / "CartoonModelShader" / "presets"
    assert presets.presets_dir() == expected


def test_presets_dir_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv(presets.ENV_PRESET_DIR, str(tmp_path / "custom"))
    assert presets.presets_dir() == tmp_path / "custom"


def test_presets_dir_falls_back_to_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(presets.ENV_PRESET_DIR, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    expected = Path.home() / ".local" / "share" / "CartoonModelShader" / "presets"
    assert presets.presets_dir() == expected


def test_default_presets_dir_is_outside_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """预设**绝不能**落在仓库里（否则会被误提交）。"""
    monkeypatch.delenv(presets.ENV_PRESET_DIR, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    directory = presets.presets_dir().resolve()
    assert REPO_ROOT.resolve() not in directory.parents
    assert directory != REPO_ROOT.resolve()


def test_storage_display_has_no_absolute_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(presets.ENV_PRESET_DIR, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    text = presets.storage_display()
    assert text == "%LOCALAPPDATA%\\CartoonModelShader\\presets"
    assert not re.search(r"[A-Za-z]:[\\/]", text)
    assert str(tmp_path) not in text


def test_storage_display_marks_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(presets.ENV_PRESET_DIR, str(tmp_path / "custom"))
    assert presets.storage_display() == "%TOON_TUNER_PRESET_DIR%"


def test_display_for_custom_dir_is_neutral(tmp_path: Path) -> None:
    """自定义目录不回路径 —— 相对部分可能嵌套用户名。"""
    text = presets.display_for(tmp_path / "个别的" / "presets")
    assert text == presets.CUSTOM_DIR_DISPLAY
    assert str(tmp_path) not in text
    assert not re.search(r"[A-Za-z]:[\\/]", text)


# =============================================================================
#  2. 文件名净化
# =============================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("柔和辉光", "柔和辉光"),
        ("a/b", "a-b"),
        ("a\\b", "a-b"),
        ("a:b*c?d", "a-b-c-d"),
        ("a|b<c>d", "a-b-c-d"),
        ('quote"name', "quote-name"),
        ("trailing...", "trailing"),
        ("trailing   ", "trailing"),
        ("  spaced  ", "spaced"),
        ("multi   space", "multi space"),
        ("dash--dash", "dash-dash"),
        ("", "preset"),
        ("...", "preset"),
        ("   ", "preset"),
        ("CON", "CON-preset"),
        ("nul", "nul-preset"),
        ("com3", "com3-preset"),
    ],
)
def test_slugify(raw: str, expected: str) -> None:
    assert presets.slugify(raw) == expected


def test_slugify_keeps_cjk_and_caps_length() -> None:
    long_name = "很长的预设名字" * 30
    slug = presets.slugify(long_name)
    assert len(slug) <= 60
    assert slug
    # 中文本身是合法文件名，不该被抹平
    assert "很长的预设名字" in slug


def test_slugify_does_not_change_stored_name(tmp_path: Path) -> None:
    """净化的只是文件名，预设里的 name 原样保留。"""
    store = presets.PresetStore(tmp_path / "presets")
    saved = store.save(body(name="柔和/辉光: v1"))
    assert saved["name"] == "柔和/辉光: v1"
    assert [p.name for p in (tmp_path / "presets").glob("*.json")] == ["柔和-辉光- v1.json"]


# =============================================================================
#  3. 校验：schema 与顶层字段
# =============================================================================


def test_normalize_fills_defaults() -> None:
    preset = presets.normalize_preset({"name": "  柔光  ", "parameters": {}})
    assert preset["schema"] == presets.PRESET_SCHEMA
    assert preset["name"] == "柔光"
    assert preset["pipeline_mode"] == presets.DEFAULT_PIPELINE_MODE
    assert preset["framing_mode"] == "current_camera"
    assert preset["framing_margin"] == pytest.approx(0.15)
    assert preset["preview_quality"] == presets.DEFAULT_PREVIEW_QUALITY
    assert preset["parameters"] == {}
    assert len(preset["preset_id"]) == 12
    assert preset["created_at"] == preset["updated_at"]


def test_normalize_rejects_unknown_top_level_field() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset({"name": "x", "parameters": {}, "blend_path": "a"})
    assert excinfo.value.code == errors.PRESET_INVALID
    assert "blend_path" in excinfo.value.message


def test_normalize_rejects_non_object() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset(["not", "a", "dict"])
    assert excinfo.value.code == errors.PRESET_INVALID


def test_normalize_accepts_missing_or_null_schema() -> None:
    assert presets.normalize_preset({"name": "a", "parameters": {}})["schema"] == presets.PRESET_SCHEMA
    assert (
        presets.normalize_preset({"name": "b", "schema": None, "parameters": {}})["schema"]
        == presets.PRESET_SCHEMA
    )


@pytest.mark.parametrize("bad_schema", ["toon-tuner-preset/0", "toon-tuner-preset/2", "", "x", 1])
def test_old_or_unknown_schema_is_refused_not_migrated(bad_schema: object) -> None:
    """旧 schema 必须**明确报错**，不能静默迁移、更不能静默套用。"""
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset({"name": "旧预设", "schema": bad_schema, "parameters": {}})
    error = excinfo.value
    assert error.code == errors.PRESET_SCHEMA_UNSUPPORTED
    assert error.retryable is False
    assert error.http_status == 400
    assert error.details["supported"] == [presets.PRESET_SCHEMA]


@pytest.mark.parametrize("bad_name", ["", "   ", None, 12, {"a": 1}, "x" * 81])
def test_normalize_rejects_bad_name(bad_name: object) -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset({"name": bad_name, "parameters": {}})
    assert excinfo.value.code == errors.PRESET_INVALID


def test_normalize_rejects_non_dict_parameters() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset({"name": "x", "parameters": [1, 2]})
    assert excinfo.value.code == errors.PRESET_INVALID


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("pipeline_mode", "cinematic"),
        ("framing_mode", "auto_whole_body"),
        ("preview_quality", "ultra"),
        ("framing_margin", 0.9),
        ("framing_margin", -0.1),
        ("framing_margin", "big"),
        ("framing_margin", True),
    ],
)
def test_normalize_rejects_bad_runtime_settings(field: str, bad_value: object) -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset({"name": "x", "parameters": {}, field: bad_value})
    assert excinfo.value.code == errors.PRESET_INVALID
    assert field in excinfo.value.message


def test_normalize_keeps_explicit_identity_and_created_at() -> None:
    preset = presets.normalize_preset(
        {"name": "x", "parameters": {}},
        preset_id="fixedid1234",
        created_at="2026-01-02T03:04:05+08:00",
        updated_at="2026-02-03T04:05:06+08:00",
    )
    assert preset["preset_id"] == "fixedid1234"
    assert preset["created_at"] == "2026-01-02T03:04:05+08:00"
    assert preset["updated_at"] == "2026-02-03T04:05:06+08:00"


def test_normalize_rejects_bad_timestamp() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset({"name": "x", "parameters": {}, "created_at": "昨天"})
    assert excinfo.value.code == errors.PRESET_INVALID


# =============================================================================
#  4. 校验：参数
# =============================================================================


def test_parameter_shorthand_expands_to_triple() -> None:
    preset = preset_from({"color.exposure": 0.35})
    assert preset["parameters"]["color.exposure"] == {
        "configured_value": pytest.approx(0.35),
        "effective_value": pytest.approx(0.35),
        "active": True,
    }


def test_parameter_triple_is_preserved() -> None:
    preset = preset_from(
        {"color.look": {"configured_value": "AgX - High Contrast", "effective_value": "High Contrast", "active": False}}
    )
    entry = preset["parameters"]["color.look"]
    assert entry["configured_value"] == "AgX - High Contrast"
    assert entry["effective_value"] == "High Contrast"
    assert entry["active"] is False


def test_parameter_effective_defaults_to_configured() -> None:
    preset = preset_from({"color.gamma": {"configured_value": 1.4}})
    entry = preset["parameters"]["color.gamma"]
    assert entry["effective_value"] == pytest.approx(1.4)
    assert entry["active"] is True


def test_parameter_allows_none_value() -> None:
    """辉光节点组不存在时基线里就是 None —— 不该因此拒绝整个预设。"""
    preset = preset_from({"glow.strength": {"configured_value": None, "effective_value": None}})
    assert preset["parameters"]["glow.strength"]["effective_value"] is None


def test_unknown_parameter_is_rejected() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset({"name": "x", "parameters": {"material.base_color": 1}})
    assert excinfo.value.code == errors.PRESET_INVALID
    assert excinfo.value.details["parameter"] == "material.base_color"


@pytest.mark.parametrize(
    "param_id, value",
    [
        ("color.exposure", 99),
        ("color.exposure", -99),
        ("color.exposure", True),
        ("color.exposure", "0.5"),
        ("color.gamma", 0.0),
        ("glow.strength", 11),
        ("glow.threshold", -0.1),
    ],
)
def test_float_parameter_bounds_are_enforced(param_id: str, value: object) -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset({"name": "x", "parameters": {param_id: value}})
    assert excinfo.value.code == errors.PRESET_INVALID


@pytest.mark.parametrize(
    "param_id, value",
    [
        ("color.view_transform", ""),
        ("color.view_transform", "   "),
        ("color.view_transform", 1),
        ("glow.type", "x" * 300),
    ],
)
def test_enum_parameter_structure_is_checked(param_id: str, value: object) -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset({"name": "x", "parameters": {param_id: value}})
    assert excinfo.value.code == errors.PRESET_INVALID


def test_enum_parameter_does_not_pin_local_blender_set() -> None:
    """预设要能跨 Blender 版本使用 —— 不拿本机枚举卡枚举取值。"""
    preset = preset_from({"color.view_transform": "某台机器上才有的视图"})
    assert preset["parameters"]["color.view_transform"]["effective_value"] == "某台机器上才有的视图"


def test_parameter_rejects_unknown_field() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset(
            {"name": "x", "parameters": {"color.exposure": {"configured_value": 1, "zzz": 2}}}
        )
    assert excinfo.value.code == errors.PRESET_INVALID
    assert "zzz" in excinfo.value.message


@pytest.mark.parametrize("bad_active", ["yes", 1, None])
def test_parameter_active_must_be_bool(bad_active: object) -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset(
            {"name": "x", "parameters": {"color.exposure": {"configured_value": 1, "active": bad_active}}}
        )
    assert excinfo.value.code == errors.PRESET_INVALID


# =============================================================================
#  5. 敏感内容扫描
# =============================================================================


@pytest.mark.parametrize(
    "text",
    [
        r"C:\models\角色.blend",
        r"\\nas\share\model.blend",
        "/Users/someone/work/model.blend",
        r"%TEMP%\toon-tuner-previews\preview_abc.png",
        r"D:\assets\tex\face.png",
        r"F:\rig\miku.vmd",
    ],
)
def test_scan_flags_sensitive_strings(text: str) -> None:
    findings = presets.scan_forbidden({"name": text})
    assert findings, text


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "ok", "mcp_token": "abc"},
        {"name": "ok", "api_key": "abc"},
        {"name": "ok", "blend_password": "abc"},
    ],
)
def test_scan_flags_credential_field_names(payload: dict) -> None:
    findings = presets.scan_forbidden(payload)
    assert any("凭据" in item["reason"] for item in findings)


@pytest.mark.parametrize(
    "value",
    ["ghp_" + "a" * 30, "github_pat_" + "b" * 30, "sk-" + "c" * 24, "token=abcdefghijklmnop"],
)
def test_scan_flags_credential_values(value: str) -> None:
    assert presets.scan_forbidden({"name": value})


def test_clean_preset_has_no_findings() -> None:
    preset = preset_from(
        {
            "color.exposure": 0.2,
            "color.view_transform": "AgX",
            "color.look": "AgX - High Contrast",
            "glow.type": "Bloom",
        },
        pipeline_mode="enhanced",
        framing_mode="auto_full_body",
        preview_quality="high",
    )
    assert presets.scan_forbidden(preset) == []


def test_forbidden_content_in_name_is_rejected() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset({"name": r"E:\project\角色.blend", "parameters": {}})
    error = excinfo.value
    assert error.code == errors.PRESET_INVALID
    assert error.retryable is False
    assert error.details["findings"]


def test_forbidden_content_in_value_is_rejected() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.normalize_preset(
            {"name": "x", "parameters": {"color.view_transform": r"C:\ocio\config.ocio"}}
        )
    assert excinfo.value.code == errors.PRESET_INVALID


def test_forbidden_content_never_reaches_disk(store: presets.PresetStore) -> None:
    with pytest.raises(errors.ToonTunerError):
        store.save(body(name=r"C:\models\角色.blend"))
    assert not list(store.directory.glob("*.json"))
    assert not store.directory.exists()


# =============================================================================
#  6. 加载成草稿
# =============================================================================


def test_materialize_takes_active_parameters_only() -> None:
    preset = preset_from(
        {
            "color.exposure": {"configured_value": 0.2, "effective_value": 0.2, "active": True},
            "glow.size": {"configured_value": 0.5, "effective_value": 0.5, "active": False},
        }
    )
    result = presets.materialize_draft(preset)
    assert result["draft"] == {"color.exposure": pytest.approx(0.2)}
    assert any(note["reason"] == "inactive" for note in result["notes"])


def test_materialize_prefers_effective_value() -> None:
    preset = preset_from(
        {"color.look": {"configured_value": "AgX - High Contrast", "effective_value": "High Contrast"}}
    )
    result = presets.materialize_draft(preset)
    assert result["draft"]["color.look"] == "High Contrast"
    assert any(note["reason"] == "migrated_on_save" for note in result["notes"])


def test_materialize_falls_back_to_configured_value() -> None:
    preset = preset_from({"color.gamma": {"configured_value": 1.3}})
    assert presets.materialize_draft(preset)["draft"]["color.gamma"] == pytest.approx(1.3)


def test_materialize_skips_null_value() -> None:
    preset = preset_from({"glow.size": {"configured_value": None, "effective_value": None, "active": True}})
    result = presets.materialize_draft(preset)
    assert result["draft"] == {}
    assert any(note["reason"] == "no_value" for note in result["notes"])


def test_materialize_migrates_legacy_look_label() -> None:
    """旧预设里的短档位名要按当前视图规范化成真实 identifier。"""
    preset = preset_from({"color.look": "High Contrast"})
    result = presets.materialize_draft(
        preset, baseline_values={"color.view_transform": "AgX"}, look_map=LOOK_MAP
    )
    assert result["draft"]["color.look"] == "AgX - High Contrast"
    note = next(n for n in result["notes"] if n["parameter"] == "color.look")
    assert note["from"] == "High Contrast"
    assert note["to"] == "AgX - High Contrast"


def test_materialize_rejects_look_invalid_for_view() -> None:
    """严格路径：视图与能力表都知道，档位不合法就必须报稳定错误。"""
    preset = preset_from({"color.look": "AgX - Punchy"})
    with pytest.raises(errors.ToonTunerError) as excinfo:
        presets.materialize_draft(
            preset, baseline_values={"color.view_transform": "Standard"}, look_map=LOOK_MAP
        )
    error = excinfo.value
    assert error.code == errors.INVALID_DEPENDENT_ENUM
    assert error.details["parameter"] == "color.look"
    assert error.details["value"] == "AgX - Punchy"
    assert error.details["depends_on"] == {"color.view_transform": "Standard"}
    assert error.details["allowed"] == ["None", "High Contrast"]
    assert error.retryable is False
    # 平铺形状（与 MVP-02 的稳定错误一致）
    payload = error.to_payload()["error"]
    assert payload["allowed"] == ["None", "High Contrast"]


def test_materialize_accepts_look_valid_for_view() -> None:
    preset = preset_from({"color.look": "AgX - Punchy"})
    result = presets.materialize_draft(
        preset, baseline_values={"color.view_transform": "AgX"}, look_map=LOOK_MAP
    )
    assert result["draft"]["color.look"] == "AgX - Punchy"
    assert result["notes"] == []


def test_materialize_uses_preset_view_transform_over_baseline() -> None:
    preset = preset_from({"color.view_transform": "AgX", "color.look": "AgX - Punchy"})
    result = presets.materialize_draft(
        preset, baseline_values={"color.view_transform": "Standard"}, look_map=LOOK_MAP
    )
    assert result["draft"]["color.look"] == "AgX - Punchy"


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"look_map": None}, "look_map_unavailable"),
        ({"look_map": LOOK_MAP}, "view_transform_unknown"),
    ],
)
def test_materialize_defers_when_view_cannot_be_determined(kwargs: dict, reason: str) -> None:
    """判不了就明说判不了 —— 带出原值 + 一条 note，提交预览时再硬校验。"""
    preset = preset_from({"color.look": "AgX - Punchy"})
    result = presets.materialize_draft(preset, **kwargs)
    assert result["draft"]["color.look"] == "AgX - Punchy"
    note = next(n for n in result["notes"] if n["parameter"] == "color.look")
    assert note["reason"] == reason
    assert note["detail"]


def test_materialize_defers_when_view_missing_from_capability() -> None:
    preset = preset_from({"color.view_transform": "Filmic", "color.look": "AgX - Punchy"})
    result = presets.materialize_draft(preset, look_map=LOOK_MAP)
    note = next(n for n in result["notes"] if n["parameter"] == "color.look")
    assert note["reason"] == "view_transform_not_in_capability"
    assert note["view_transform"] == "Filmic"


def test_materialize_ignores_look_when_preset_has_none() -> None:
    preset = preset_from({"color.exposure": 0.4})
    result = presets.materialize_draft(preset, look_map=None)
    assert result["draft"] == {"color.exposure": pytest.approx(0.4)}
    assert result["notes"] == []


# =============================================================================
#  7. 存储 CRUD
# =============================================================================


def test_save_creates_file_and_lists(tmp_path: Path) -> None:
    store = presets.PresetStore(tmp_path / "presets")
    saved = store.save(body(name="柔和辉光"))
    files = sorted(p.name for p in (tmp_path / "presets").glob("*.json"))
    assert files == ["柔和辉光.json"]
    listing = store.list()
    assert [item["preset_id"] for item in listing["presets"]] == [saved["preset_id"]]
    assert listing["skipped"] == []
    assert listing["quality_tiers"]["fast"]["samples"] == 4
    assert listing["quality_tiers"]["standard"]["nominal_resolution"] == [540, 990]
    assert listing["quality_tiers"]["high"]["samples"] == 16
    assert listing["pipeline_modes"] == ["faithful", "enhanced"]
    assert listing["defaults"]["preview_quality"] == "standard"


def test_get_round_trips_every_field(store: presets.PresetStore) -> None:
    saved = store.save(
        body(
            name="全字段",
            pipeline_mode="enhanced",
            framing_mode="auto_upper_body",
            framing_margin=0.3,
            preview_quality="high",
            parameters={
                "color.exposure": {"configured_value": 0.5, "effective_value": 0.5, "active": True},
                "color.look": {"configured_value": "AgX - High Contrast", "effective_value": "AgX - High Contrast", "active": False},
            },
        )
    )
    loaded = store.get(saved["preset_id"])
    assert loaded == saved
    assert loaded["framing_margin"] == pytest.approx(0.3)


def test_save_overwrite_preserves_identity_and_created_at(store: presets.PresetStore) -> None:
    saved = store.save(body(name="覆盖我"))
    overwritten = store.save(body(name="覆盖我", parameters={"color.exposure": 1.5}), preset_id=saved["preset_id"])
    assert overwritten["preset_id"] == saved["preset_id"]
    assert overwritten["created_at"] == saved["created_at"]
    assert overwritten["parameters"]["color.exposure"]["effective_value"] == pytest.approx(1.5)
    assert len(store.list()["presets"]) == 1
    assert len(list(store.directory.glob("*.json"))) == 1


def test_save_overwrite_does_not_take_client_supplied_id(store: presets.PresetStore) -> None:
    saved = store.save(body(name="身份"))
    updated = store.save(
        {**body(name="身份"), "preset_id": "forged-id-999"},
        preset_id=saved["preset_id"],
    )
    assert updated["preset_id"] == saved["preset_id"]


def test_save_overwrite_missing_target_is_not_found(store: presets.PresetStore) -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        store.save(body(name="x"), preset_id="doesnotexist")
    assert excinfo.value.code == errors.PRESET_NOT_FOUND


def test_rename_keeps_identity_and_renames_file(store: presets.PresetStore) -> None:
    saved = store.save(body(name="旧名字"))
    renamed = store.rename(saved["preset_id"], "  新名字  ")
    assert renamed["preset_id"] == saved["preset_id"]
    assert renamed["name"] == "新名字"
    assert renamed["created_at"] == saved["created_at"]
    assert renamed["updated_at"] >= saved["updated_at"]
    files = sorted(p.name for p in store.directory.glob("*.json"))
    assert files == ["新名字.json"]
    assert store.get(saved["preset_id"])["name"] == "新名字"


def test_rename_to_existing_name_conflicts(store: presets.PresetStore) -> None:
    first = store.save(body(name="甲"))
    store.save(body(name="乙"))
    with pytest.raises(errors.ToonTunerError) as excinfo:
        store.rename(first["preset_id"], "乙")
    assert excinfo.value.code == errors.PRESET_NAME_CONFLICT
    assert excinfo.value.http_status == 409


def test_rename_to_same_name_is_allowed(store: presets.PresetStore) -> None:
    saved = store.save(body(name="同名"))
    renamed = store.rename(saved["preset_id"], "同名")
    assert renamed["name"] == "同名"


def test_rename_missing_is_not_found(store: presets.PresetStore) -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        store.rename("missing", "x")
    assert excinfo.value.code == errors.PRESET_NOT_FOUND


def test_duplicate_creates_new_identity(store: presets.PresetStore) -> None:
    saved = store.save(body(name="原件", parameters={"color.exposure": 0.7}))
    copy = store.duplicate(saved["preset_id"])
    assert copy["preset_id"] != saved["preset_id"]
    assert copy["name"] == "原件 副本"
    assert copy["parameters"] == saved["parameters"]
    assert copy["created_at"] >= saved["created_at"]
    assert len(store.list()["presets"]) == 2
    assert len(list(store.directory.glob("*.json"))) == 2


def test_duplicate_does_not_inherit_source_identity(store: presets.PresetStore) -> None:
    """回归：``preset_id=None`` 曾回退成源身份，复制出的文件被判重复而消失。"""
    saved = store.save(body(name="回归"))
    copy = store.duplicate(saved["preset_id"])
    ids = [item["preset_id"] for item in store.list()["presets"]]
    assert sorted(ids) == sorted([saved["preset_id"], copy["preset_id"]])
    assert store.list()["skipped"] == []


def test_duplicate_with_explicit_name(store: presets.PresetStore) -> None:
    saved = store.save(body(name="原件"))
    copy = store.duplicate(saved["preset_id"], "指定副本")
    assert copy["name"] == "指定副本"


def test_duplicate_name_conflict(store: presets.PresetStore) -> None:
    saved = store.save(body(name="原件"))
    store.save(body(name="占用中"))
    with pytest.raises(errors.ToonTunerError) as excinfo:
        store.duplicate(saved["preset_id"], "占用中")
    assert excinfo.value.code == errors.PRESET_NAME_CONFLICT


def test_duplicate_auto_name_avoids_collision(store: presets.PresetStore) -> None:
    saved = store.save(body(name="原件"))
    store.duplicate(saved["preset_id"])
    second = store.duplicate(saved["preset_id"])
    assert second["name"] == "原件 副本 2"
    assert len(store.list()["presets"]) == 3


def test_duplicate_long_name_stays_within_limit(store: presets.PresetStore) -> None:
    saved = store.save(body(name="长" * presets.NAME_MAX_LENGTH))
    copy = store.duplicate(saved["preset_id"])
    assert len(copy["name"]) <= presets.NAME_MAX_LENGTH
    assert copy["name"].endswith("副本")


def test_duplicate_missing_is_not_found(store: presets.PresetStore) -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        store.duplicate("missing")
    assert excinfo.value.code == errors.PRESET_NOT_FOUND


def test_delete_removes_file(store: presets.PresetStore) -> None:
    saved = store.save(body(name="待删"))
    deleted = store.delete(saved["preset_id"])
    assert deleted["preset_id"] == saved["preset_id"]
    assert not list(store.directory.glob("*.json"))
    with pytest.raises(errors.ToonTunerError) as excinfo:
        store.get(saved["preset_id"])
    assert excinfo.value.code == errors.PRESET_NOT_FOUND


def test_delete_twice_is_not_found(store: presets.PresetStore) -> None:
    saved = store.save(body(name="删两次"))
    store.delete(saved["preset_id"])
    with pytest.raises(errors.ToonTunerError) as excinfo:
        store.delete(saved["preset_id"])
    assert excinfo.value.code == errors.PRESET_NOT_FOUND


def test_name_conflict_ignores_surrounding_whitespace(store: presets.PresetStore) -> None:
    store.save(body(name="重名"))
    with pytest.raises(errors.ToonTunerError) as excinfo:
        store.save(body(name="  重名  "))
    assert excinfo.value.code == errors.PRESET_NAME_CONFLICT


def test_name_conflict_is_case_insensitive(store: presets.PresetStore) -> None:
    store.save(body(name="SoftGlow"))
    with pytest.raises(errors.ToonTunerError) as excinfo:
        store.save(body(name="softglow"))
    assert excinfo.value.code == errors.PRESET_NAME_CONFLICT


def test_slug_collision_keeps_both_presets(store: presets.PresetStore) -> None:
    """两个不同名字净化后可能撞文件名 —— 不能因此互相覆盖。"""
    first = store.save(body(name="a/b"))
    second = store.save(body(name="a:b"))
    files = sorted(p.name for p in store.directory.glob("*.json"))
    assert files == ["a-b-2.json", "a-b.json"]
    assert store.get(first["preset_id"])["name"] == "a/b"
    assert store.get(second["preset_id"])["name"] == "a:b"
    assert len(store.list()["presets"]) == 2


def test_atomic_write_leaves_no_temp_files(store: presets.PresetStore) -> None:
    saved = store.save(body(name="原子"))
    store.rename(saved["preset_id"], "原子改名")
    assert list(store.directory.glob(".tmp-*")) == []


def test_corrupt_file_is_reported_not_fatal(store: presets.PresetStore) -> None:
    store.save(body(name="好的"))
    (store.directory / "broken.json").write_text("{ 这不是 JSON", encoding="utf-8")
    listing = store.list()
    assert [item["name"] for item in listing["presets"]] == ["好的"]
    assert len(listing["skipped"]) == 1
    assert listing["skipped"][0]["code"] == errors.PRESET_INVALID
    assert listing["skipped"][0]["file"] == "broken.json"


def test_legacy_schema_file_is_reported(store: presets.PresetStore) -> None:
    store.ensure()
    (store.directory / "legacy.json").write_text(
        json.dumps({"schema": "toon-tuner-preset/0", "name": "老预设", "parameters": {}}),
        encoding="utf-8",
    )
    listing = store.list()
    assert listing["presets"] == []
    assert listing["skipped"][0]["code"] == errors.PRESET_SCHEMA_UNSUPPORTED


def test_duplicate_preset_id_on_disk_is_reported(store: presets.PresetStore) -> None:
    store.ensure()
    payload = {"schema": presets.PRESET_SCHEMA, "preset_id": "same-id-here", "name": "一", "parameters": {}}
    (store.directory / "one.json").write_text(json.dumps(payload), encoding="utf-8")
    (store.directory / "two.json").write_text(
        json.dumps({**payload, "name": "二"}), encoding="utf-8"
    )
    listing = store.list()
    assert len(listing["presets"]) == 1
    assert listing["skipped"][0]["file"] == "two.json"
    assert "preset_id" in listing["skipped"][0]["message"]


def test_non_object_file_is_reported(store: presets.PresetStore) -> None:
    store.ensure()
    (store.directory / "array.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert store.list()["skipped"][0]["code"] == errors.PRESET_INVALID


def test_store_ignores_non_json_files(store: presets.PresetStore) -> None:
    store.ensure()
    (store.directory / "notes.txt").write_text("随便写点什么", encoding="utf-8")
    store.save(body(name="正常"))
    assert len(store.list()["presets"]) == 1
    assert store.list()["skipped"] == []


def test_store_ignores_leftover_temp_files(store: presets.PresetStore) -> None:
    """点开头的临时文件不算预设 —— ``Path.glob`` 与 shell 不同，会匹配到它们。"""
    store.ensure()
    (store.directory / ".tmp-orphan.json").write_text("{ 半个 JSON", encoding="utf-8")
    store.save(body(name="正常"))
    listing = store.list()
    assert [item["name"] for item in listing["presets"]] == ["正常"]
    assert listing["skipped"] == []


def test_list_sorted_by_name(store: presets.PresetStore) -> None:
    for name in ["c", "a", "b"]:
        store.save(body(name=name))
    assert [item["name"] for item in store.list()["presets"]] == ["a", "b", "c"]


def test_summary_counts_active(store: presets.PresetStore) -> None:
    saved = store.save(
        body(
            name="计数",
            parameters={
                "color.exposure": {"configured_value": 1, "effective_value": 1, "active": True},
                "glow.size": {"configured_value": 0.5, "effective_value": 0.5, "active": False},
            },
        )
    )
    summary = presets.summary_of(saved)
    assert summary["parameter_count"] == 2
    assert summary["active_count"] == 1


def test_listing_exposes_no_absolute_path(store: presets.PresetStore) -> None:
    store.save(body(name="路径检查"))
    listing = store.list()
    text = json.dumps(listing, ensure_ascii=False)
    assert not re.search(r"[A-Za-z]:[\\/]", text)
    assert "/Users/" not in text
    assert "/home/" not in text
    assert listing["storage"] == presets.CUSTOM_DIR_DISPLAY


# =============================================================================
#  8. API
# =============================================================================


def test_api_list_shape(client: TestClient) -> None:
    response = client.get("/api/presets")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["protocol"] == presets.PRESET_PROTOCOL
    assert payload["presets"] == []
    assert set(payload["quality_tiers"]) == set(presets.PREVIEW_QUALITY_TIERS)
    assert payload["framing_modes"]
    assert not re.search(r"[A-Za-z]:[\\/]", response.text)


def test_api_crud_round_trip(client: TestClient) -> None:
    created = client.post("/api/presets", json=body(name="接口预设"))
    assert created.status_code == 201
    detail = created.json()["preset"]
    preset_id = detail["preset_id"]
    assert detail["schema"] == presets.PRESET_SCHEMA
    assert detail["parameters"]["color.exposure"]["active"] is True

    listed = client.get("/api/presets").json()
    assert [item["preset_id"] for item in listed["presets"]] == [preset_id]

    fetched = client.get(f"/api/presets/{preset_id}")
    assert fetched.status_code == 200
    assert fetched.json()["preset"]["name"] == "接口预设"

    put = client.put(f"/api/presets/{preset_id}", json=body(name="接口预设", preview_quality="fast"))
    assert put.status_code == 200
    assert put.json()["preset"]["preview_quality"] == "fast"
    assert put.json()["preset"]["preset_id"] == preset_id
    assert put.json()["preset"]["created_at"] == detail["created_at"]

    renamed = client.post(f"/api/presets/{preset_id}/rename", json={"name": "改名了"})
    assert renamed.status_code == 200
    assert renamed.json()["preset"]["preset_id"] == preset_id

    duplicated = client.post(f"/api/presets/{preset_id}/duplicate", json={})
    assert duplicated.status_code == 201
    assert duplicated.json()["preset"]["name"] == "改名了 副本"

    deleted = client.delete(f"/api/presets/{preset_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"]["name"] == "改名了"
    assert client.get(f"/api/presets/{preset_id}").status_code == 404


def test_api_missing_preset_is_404(client: TestClient) -> None:
    response = client.get("/api/presets/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == errors.PRESET_NOT_FOUND


@pytest.mark.parametrize(
    "method, path, payload, expected_code",
    [
        ("post", "/api/presets", {"name": "x", "parameters": {"nope": 1}}, errors.PRESET_INVALID),
        ("post", "/api/presets", {"name": ""}, errors.PRESET_INVALID),
        (
            "post",
            "/api/presets",
            {"name": "x", "schema": "toon-tuner-preset/0", "parameters": {}},
            errors.PRESET_SCHEMA_UNSUPPORTED,
        ),
        ("post", "/api/presets", {"name": r"C:\x.blend", "parameters": {}}, errors.PRESET_INVALID),
        ("post", "/api/presets", {"name": "x", "preview_quality": "ultra"}, errors.PRESET_INVALID),
        ("post", "/api/presets/zzz/rename", {"name": "n"}, errors.PRESET_NOT_FOUND),
        ("post", "/api/presets/zzz/duplicate", {}, errors.PRESET_NOT_FOUND),
        ("delete", "/api/presets/zzz", None, errors.PRESET_NOT_FOUND),
    ],
)
def test_api_error_codes(
    client: TestClient, method: str, path: str, payload: dict | None, expected_code: str
) -> None:
    call = getattr(client, method)
    response = call(path, json=payload) if payload is not None else call(path)
    assert response.status_code in (400, 404, 409), response.text
    error = response.json()["error"]
    assert error["code"] == expected_code
    assert error["hint"]
    assert "retryable" in error


def test_api_name_conflict_is_409(client: TestClient) -> None:
    client.post("/api/presets", json=body(name="撞名"))
    response = client.post("/api/presets", json=body(name="撞名"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == errors.PRESET_NAME_CONFLICT


def test_api_rejects_unknown_top_level_field(client: TestClient) -> None:
    response = client.post("/api/presets", json={**body(name="x"), "surprise": 1})
    assert response.status_code == 422


def test_api_accepts_null_schema(client: TestClient) -> None:
    response = client.post("/api/presets", json={**body(name="空 schema"), "schema": None})
    assert response.status_code == 201
    assert response.json()["preset"]["schema"] == presets.PRESET_SCHEMA


def test_api_does_not_need_blender(client: TestClient) -> None:
    """预设接口指向一个连不上的端口也能工作 —— 存储与 Blender 无关。"""
    assert client.post("/api/presets", json=body(name="无 Blender")).status_code == 201
    assert client.get("/api/presets").status_code == 200


def test_api_response_never_leaks_store_path(client: TestClient, store: presets.PresetStore) -> None:
    client.post("/api/presets", json=body(name="路径"))
    preset_id = client.get("/api/presets").json()["presets"][0]["preset_id"]
    for path in ("/api/presets", f"/api/presets/{preset_id}"):
        text = client.get(path).text
        assert str(store.directory) not in text
        assert str(store.directory).replace("\\", "\\\\") not in text
        assert not re.search(r"[A-Za-z]:[\\/]", text)
