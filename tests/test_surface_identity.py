"""三层身份测试（技术方案决策 1、2、3）。

要钉住的行为只有一个重点：**三种变化的严重度必须不同**。

* 重命名/删除 ⇒ 致命（草稿与确认令牌作废）
* 结构变化（节点增删、色标数量、材质槽）⇒ 致命
* 普通值变化 ⇒ **非致命**，只作为外部改动报告

如果值变化也算致命，用户在 Blender 里动一下色标，工具就会把草稿全废掉。
"""

from __future__ import annotations

import pytest

from src.server import errors, surface
from src.server.surface import identity as idmod


def record(name: str, **structure) -> idmod.ObjectRecord:
    return idmod.ObjectRecord(
        identity=idmod.ObjectIdentity(
            object_type="NODE_GROUP", name=name, source="AI_Compositor"
        ),
        structure={
            "node_count": structure.get("node_count", 2),
            "color_ramp_elements": structure.get("color_ramp_elements", 3),
            "material_slots": structure.get("material_slots", 4),
            "link_count": structure.get("link_count"),
        },
    )


def records(*items: idmod.ObjectRecord) -> dict[str, idmod.ObjectRecord]:
    return {item.key: item for item in items}


# -- 身份层 ----------------------------------------------------------------


def test_identity_key_uses_type_source_name_not_fingerprint() -> None:
    one = record("Cel_Skin")
    two = record("Cel_Hair")
    # 结构完全相同，但身份不同 —— 这正是「不能用指纹匹配对象」的原因
    assert one.structure_signature() == two.structure_signature()
    assert one.key != two.key
    assert one.key == "NODE_GROUP:AI_Compositor/Cel_Skin"


def test_renamed_object_is_identity_missing_and_fatal() -> None:
    baseline = records(record("Cel_Skin"), record("Cel_Hair"))
    current = records(record("Cel_Skin"), record("Cel_Hair.001"))

    verdict = idmod.compare(baseline, current)
    assert verdict.kind == idmod.VERDICT_IDENTITY_MISSING
    assert verdict.fatal is True
    assert verdict.identity_missing == ["NODE_GROUP:AI_Compositor/Cel_Hair"]
    assert verdict.identity_added == ["NODE_GROUP:AI_Compositor/Cel_Hair.001"]


def test_deleted_object_is_identity_missing() -> None:
    baseline = records(record("Cel_Skin"), record("Cel_Hair"))
    verdict = idmod.compare(baseline, records(record("Cel_Skin")))
    assert verdict.kind == idmod.VERDICT_IDENTITY_MISSING
    assert verdict.fatal is True


# -- 结构层 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"node_count": 3},
        {"color_ramp_elements": 2},
        {"material_slots": 7},
        {"link_count": 5},
    ],
)
def test_structural_changes_are_fatal(change: dict) -> None:
    baseline = records(record("Cel_Skin"))
    current = records(record("Cel_Skin", **change))
    verdict = idmod.compare(baseline, current)
    assert verdict.kind == idmod.VERDICT_STRUCTURE_CHANGED
    assert verdict.fatal is True
    assert verdict.structure_changed == ["NODE_GROUP:AI_Compositor/Cel_Skin"]


def test_ramp_element_count_change_is_structural() -> None:
    """决策 3：色标数量变化必须被识别为结构变化。"""
    baseline = records(record("Cel_Skin", color_ramp_elements=3))
    current = records(record("Cel_Skin", color_ramp_elements=4))
    verdict = idmod.compare(baseline, current)
    assert verdict.kind == idmod.VERDICT_STRUCTURE_CHANGED
    assert verdict.fatal is True


def test_structure_hash_changes_with_structure() -> None:
    before = idmod.structure_hash(records(record("Cel_Skin")))
    after = idmod.structure_hash(records(record("Cel_Skin", color_ramp_elements=4)))
    assert before != after
    # 相同结构 ⇒ 相同哈希（稳定）
    assert idmod.structure_hash(records(record("Cel_Skin"))) == before


def test_structure_hash_is_order_independent() -> None:
    a = records(record("Cel_Skin"), record("Cel_Hair"))
    b = {item.key: item for item in reversed(list(a.values()))}
    assert idmod.structure_hash(a) == idmod.structure_hash(b)


# -- 值层 ------------------------------------------------------------------


def test_value_change_is_reported_but_not_fatal() -> None:
    baseline = records(record("Cel_Skin"))
    current = records(record("Cel_Skin"))

    verdict = idmod.compare(
        baseline,
        current,
        baseline_values={"cel.Cel_Skin.ramp": 0.4, "color.exposure": 0.0},
        current_values={"cel.Cel_Skin.ramp": 0.55, "color.exposure": 0.0},
    )
    assert verdict.kind == idmod.VERDICT_VALUE_CHANGED
    assert verdict.fatal is False, "值变化不得作废草稿"
    assert verdict.value_changed == [
        {"id": "cel.Cel_Skin.ramp", "baseline": 0.4, "current": 0.55}
    ]


def test_value_change_ignores_float_noise() -> None:
    verdict = idmod.compare(
        records(record("Cel_Skin")),
        records(record("Cel_Skin")),
        baseline_values={"color.exposure": 1.0},
        current_values={"color.exposure": 1.0 + 1e-9},
    )
    assert verdict.kind == idmod.VERDICT_OK
    assert verdict.value_changed == []


def test_value_change_detects_nested_list_difference() -> None:
    verdict = idmod.compare(
        records(record("Cel_Skin")),
        records(record("Cel_Skin")),
        baseline_values={"ramp": [0.1, 0.2, 0.3, 1.0]},
        current_values={"ramp": [0.1, 0.2, 0.9, 1.0]},
    )
    assert verdict.kind == idmod.VERDICT_VALUE_CHANGED
    assert verdict.fatal is False


def test_value_fingerprint_is_stable_and_order_independent() -> None:
    first = idmod.value_fingerprint({"a": 1, "b": 2})
    second = idmod.value_fingerprint({"b": 2, "a": 1})
    assert first == second
    assert first != idmod.value_fingerprint({"a": 1, "b": 3})


def test_verdict_public_payload_shape() -> None:
    verdict = idmod.compare(records(record("Cel_Skin")), records())
    payload = verdict.to_public()
    assert payload["kind"] == idmod.VERDICT_IDENTITY_MISSING
    assert payload["fatal"] is True
    assert set(payload) >= {
        "kind",
        "fatal",
        "identity_missing",
        "identity_added",
        "renamed",
        "structure_changed",
        "value_changed",
    }


# -- 与探针输出对接 --------------------------------------------------------


def test_records_from_describe_shape() -> None:
    describe = {
        "compositor_group": "AI_Compositor",
        "managed_groups": [],
        "reference_groups": [],
        "groups": [
            {
                "name": "Cel_Skin",
                "exists": True,
                "role": "managed",
                "node_count": 2,
                "ramp_element_counts": [3],
                "material_count": 5,
            },
            {"name": "Cel_Hair", "exists": False, "role": "managed"},
        ],
    }
    built = idmod.records_from_describe(describe)
    assert list(built) == ["NODE_GROUP:AI_Compositor/Cel_Skin"]
    assert built["NODE_GROUP:AI_Compositor/Cel_Skin"].structure["color_ramp_elements"] == 3


# -- 错误码登记 ------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    ["STRUCTURE_CHANGED", "IDENTITY_MISSING", "UNSUPPORTED_PARAM", "NOT_EDITABLE",
     "SCHEMA_INVALID", "INVALID_BINDING", "ASSET_UNKNOWN", "FRONTEND_NOT_BUILT",
     "PRESET_INCOMPATIBLE"],
)
def test_new_error_codes_are_registered(code: str) -> None:
    """新错误码必须登记 meta，否则会被静默降级成 INTERNAL_ERROR。"""
    exc = errors.ToonTunerError(getattr(errors, code), "x")
    assert exc.code == code, f"{code} 未登记 _ERROR_META"
    assert 400 <= exc.http_status < 600


def test_surface_package_exports_expected_names() -> None:
    for name in ("validate_draft", "Binding", "PlanOp", "build_apply_code", "compare"):
        assert hasattr(surface, name), name
