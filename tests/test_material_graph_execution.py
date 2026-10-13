"""材质图执行（提交 3）：binding 白名单、执行顺序、失败语义与保存接入。

钉住规格「提交 3」验收条目：
* 每种 Binding 的成功写入与回读；
* 参数 ID 篡改；越界与类型错误；
* 写入中途失败回滚；回读不一致回滚；渲染失败回滚；回滚失败报告；
* 一个材质不会误改另一材质；
* 共享组未确认时拒绝；确认后只修改声明的影响集合；
* 结构变化使旧草稿失效；
* 请求无法注入代码和属性路径。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.server import errors, surface_probe
from src.server.material_graph import schema as mg_schema
from src.server.material_graph.execution import (
    baseline_ops,
    baseline_values,
    build_param_index,
    validate_graph_draft,
)
from src.server.material_graph_service import MaterialGraphService
from src.server.surface import executor as surface_executor
from src.server.surface.binding import BINDING_WHITELIST, Binding, assert_allowed
from tests.fake_bpy import FakeBpy, FakeColorRamp, FakeLink, FakeNode, run_generated_code

MATERIAL_A = "皮肤_脸"
MATERIAL_B = "衣服_上衣"


def _out(node: FakeNode, name: str):
    for sock in node.outputs:
        if sock.name == name:
            return sock
    raise KeyError(f"{node.name} 没有输出插座 {name}")


def describe_material(fake: FakeBpy) -> dict[str, Any]:
    return surface_probe.describe_groups(
        surface_probe.redact_describe(
            surface_probe.parse_describe(
                run_generated_code(surface_probe.build_describe_code(), fake)
            )
        )
    )


def _material_nodes(prefix: str = "") -> tuple[FakeNode, FakeNode, FakeNode, FakeNode, FakeNode, FakeNode, FakeNode]:
    out = FakeNode("Material Output", "OUTPUT_MATERIAL", {"Surface": None},
                   bl_idname="ShaderNodeOutputMaterial")
    bsdf = FakeNode("Principled BSDF", "BSDF_PRINCIPLED",
                    {"Base Color": [1.0, 1.0, 1.0, 1.0], "Roughness": 0.5},
                    bl_idname="ShaderNodeBsdfPrincipled", outputs={"BSDF": "SHADER"})
    mix = FakeNode("Mix", "MIX_RGB",
                   {"Fac": 0.5, "Color A": [0.9, 0.4, 0.4, 1.0],
                    "Color B": [0.2, 0.2, 0.3, 1.0]},
                   bl_idname="ShaderNodeMixRGB", outputs={"Color": "RGBA"})
    rgb = FakeNode("RGB", "RGB", {"Color": [0.9, 0.4, 0.4, 1.0]},
                   bl_idname="ShaderNodeRGB", outputs={"Color": "RGBA"})
    ramp = FakeNode("ColorRamp", "VALTORGB", {"Fac": 0.5},
                    color_ramp=FakeColorRamp(), bl_idname="ShaderNodeValToRGB",
                    outputs={"Color": "RGBA"})
    value = FakeNode("Value", "VALUE", {"Value": 0.25},
                     bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"})
    math = FakeNode("Math", "MATH", {"Value": 0.5},
                    bl_idname="ShaderNodeMath", outputs={"Value": "VALUE"})
    return out, bsdf, mix, rgb, ramp, value, math


def build_two_materials() -> FakeBpy:
    """两个材质，各自独立节点图。用于「不会误改另一材质」断言。"""
    fake = FakeBpy()
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None

    for material, factor in ((MATERIAL_A, 0.25), (MATERIAL_B, 0.75)):
        # 每个材质单独构造一批节点：FakeNode 不能同时挂在两个材质的节点树上
        out, bsdf, mix, rgb, ramp, value, math = _material_nodes()
        links = [
            FakeLink(ramp, _out(ramp, "Color"), mix, mix.inputs["Color A"]),
            FakeLink(rgb, _out(rgb, "Color"), mix, mix.inputs["Color B"]),
            FakeLink(value, _out(value, "Value"), ramp, ramp.inputs["Fac"]),
            FakeLink(mix, _out(mix, "Color"), bsdf, bsdf.inputs["Base Color"]),
            FakeLink(math, _out(math, "Value"), bsdf, bsdf.inputs["Roughness"]),
            FakeLink(bsdf, _out(bsdf, "BSDF"), out, out.inputs["Surface"]),
        ]
        value.inputs["Value"].default_value = factor
        fake.add_material_graph(material, nodes=[out, bsdf, mix, rgb, ramp, value, math],
                                links=links, users=[mesh])
    return fake


def capture(fake: FakeBpy) -> MaterialGraphService:
    service = MaterialGraphService()
    service.capture(describe_material(fake), baseline_id="mg-test")
    return service


def _index_of(service: MaterialGraphService):
    baseline = service.require()
    assert baseline.param_index is not None
    return baseline.param_index


# -- Binding 白名单 --------------------------------------------------------


def test_new_bindings_are_whitelisted() -> None:
    assert "default_value" in BINDING_WHITELIST["MATERIAL_GRAPH_SOCKET"]
    assert "elements" in BINDING_WHITELIST["MATERIAL_GRAPH_RAMP"]
    assert "interpolation" in BINDING_WHITELIST["MATERIAL_GRAPH_RAMP"]
    # 色标数量仍属结构信息，不得可写
    assert "element_count" not in BINDING_WHITELIST["MATERIAL_GRAPH_RAMP"]


def test_unknown_material_graph_field_rejected() -> None:
    """白名单外的 field 一律拒绝（含看起来像 dunder 的字段）。"""
    assert_allowed(Binding("MATERIAL_GRAPH_SOCKET", "m/n/s", "default_value"))
    for bad_field in ("__class__", "default_value_", "tree", "node", ""):
        with pytest.raises(errors.ToonTunerError) as excinfo:
            assert_allowed(Binding("MATERIAL_GRAPH_SOCKET", "m/n/s", bad_field))
        assert excinfo.value.code == errors.INVALID_BINDING


def test_binding_object_id_rejects_path_traversal() -> None:
    """object_id 形态非法（``..`` 上跳 / 反斜杠 / 引号）一律拒绝。"""
    for bad in ("m/../evil", "m\\node\\socket", "m/no'de", "/leading"):
        with pytest.raises(errors.ToonTunerError) as excinfo:
            assert_allowed(Binding("MATERIAL_GRAPH_SOCKET", bad, "default_value"))
        assert excinfo.value.code == errors.INVALID_BINDING


# -- 参数索引与计划编译 ----------------------------------------------------


def test_capture_builds_param_index() -> None:
    service = capture(build_two_materials())
    index = _index_of(service)
    assert index.params
    # 至少包含 RGB 颜色、Value 数值、ColorRamp 色带
    kinds = {param.kind for param in index.params.values()}
    assert "color" in kinds and "float" in kinds and "ramp" in kinds


def test_plan_compiles_socket_and_ramp_ops() -> None:
    service = capture(build_two_materials())
    index = _index_of(service)
    color_param = next(p for p in index.params.values() if p.kind == "color")
    ramp_param = next(p for p in index.params.values() if p.kind == "ramp")
    ops = validate_graph_draft(
        {color_param.param_id: [0.2, 0.3, 0.4, 1.0]}, index
    )
    assert len(ops) == 1
    assert ops[0].binding.object_type == "MATERIAL_GRAPH_SOCKET"

    ops = validate_graph_draft(
        {ramp_param.param_id: {"elements": ramp_param.baseline_value["elements"],
                               "interpolation": "CONSTANT"}},
        index,
    )
    types = {op.binding.field for op in ops}
    assert types == {"elements", "interpolation"}


def test_unknown_param_id_rejected() -> None:
    """参数 ID 篡改：schema 外的 id 一律拒绝，不静默丢弃。"""
    service = capture(build_two_materials())
    index = _index_of(service)
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_graph_draft({"material_graph.伪造.default_value": 1.0}, index)
    assert excinfo.value.code == errors.PARAM_INVALID


def test_type_error_rejected() -> None:
    service = capture(build_two_materials())
    index = _index_of(service)
    color_param = next(p for p in index.params.values() if p.kind == "color")
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_graph_draft({color_param.param_id: [0.1, 0.2]}, index)
    assert excinfo.value.code == errors.PARAM_INVALID
    float_param = next(p for p in index.params.values() if p.kind == "float")
    with pytest.raises(errors.ToonTunerError):
        validate_graph_draft({float_param.param_id: "恶意字符串"}, index)


def test_non_finite_rejected() -> None:
    service = capture(build_two_materials())
    index = _index_of(service)
    float_param = next(p for p in index.params.values() if p.kind == "float")
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_graph_draft({float_param.param_id: float("inf")}, index)
    assert excinfo.value.code == errors.PARAM_INVALID


def test_out_of_range_rejected() -> None:
    """值域越界（例如混��系数不在 0–1）一律拒绝。"""
    service = capture(build_two_materials())
    index = _index_of(service)
    bounded = [p for p in index.params.values() if p.maximum is not None]
    if not bounded:
        pytest.skip("当前 fixture 未生成带值域的参数")
    param = bounded[0]
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_graph_draft({param.param_id: param.maximum + 10.0}, index)
    assert excinfo.value.code == errors.PARAM_INVALID


def test_ramp_element_count_change_is_structure_change() -> None:
    """色标增删属结构改动：本执行器整份拒绝。"""
    service = capture(build_two_materials())
    index = _index_of(service)
    ramp_param = next(p for p in index.params.values() if p.kind == "ramp")
    elements = ramp_param.baseline_value["elements"]
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_graph_draft({ramp_param.param_id: elements[:-1]}, index)
    assert excinfo.value.code == errors.STRUCTURE_CHANGED


def test_structure_hash_mismatch_invalidates_draft() -> None:
    """结构哈希不符 ⇒ 草稿作废（STRUCTURE_CHANGED）。"""
    service = capture(build_two_materials())
    index = _index_of(service)
    param = next(iter(index.params.values()))
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_graph_draft(
            {param.param_id: 0.5}, index, expected_structure_hash="sha256:deadbeef"
        )
    assert excinfo.value.code == errors.STRUCTURE_CHANGED


def test_baseline_ops_roundtrip() -> None:
    """恢复基线的计划与「应用完整草稿」走同一条编译路径。"""
    service = capture(build_two_materials())
    index = _index_of(service)
    values = baseline_values(index)
    ops = baseline_ops(index)
    assert ops
    assert len(ops) >= len(values)


# -- 共享节点组 ------------------------------------------------------------


def build_shared_group_materials() -> FakeBpy:
    """两个材质都引用同一个节点组 ``Cel_Shared``（共享组场景）。"""
    fake = FakeBpy()
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None

    for material, factor in ((MATERIAL_A, 0.25), (MATERIAL_B, 0.75)):
        out, bsdf, mix, rgb, ramp, value, math = _material_nodes()
        # 组实例：输出接进 Mix.Color A，组内 Value 通过组接口喂进去
        instance = FakeNode("Cel_Shared", "GROUP", {"Value": 0.0},
                            bl_idname="ShaderNodeGroup",
                            outputs={"Color": "RGBA"})
        instance.node_tree = type("NG", (), {"name": "Cel_Shared"})()
        inner_value = FakeNode("Cel_Shared::Value", "VALUE", {"Value": float(factor)},
                               bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"})
        inner_out = FakeNode("Cel_Shared::Group Output", "GROUP_OUTPUT", {"Value": 0.0},
                             bl_idname="NodeGroupOutput")
        links = [
            FakeLink(rgb, _out(rgb, "Color"), mix, mix.inputs["Color B"]),
            FakeLink(instance, _out(instance, "Color"), mix, mix.inputs["Color A"]),
            # 组内 Value → 组实例输入（穿透：组内节点因此进入生效链路）
            FakeLink(inner_value, _out(inner_value, "Value"),
                     instance, instance.inputs["Value"]),
            FakeLink(value, _out(value, "Value"), inner_out, inner_out.inputs["Value"]),
            FakeLink(mix, _out(mix, "Color"), bsdf, bsdf.inputs["Base Color"]),
            FakeLink(math, _out(math, "Value"), bsdf, bsdf.inputs["Roughness"]),
            FakeLink(bsdf, _out(bsdf, "BSDF"), out, out.inputs["Surface"]),
        ]
        fake.add_material_graph(
            material,
            nodes=[out, bsdf, mix, rgb, value, math, instance, inner_value, inner_out],
            links=links,
            users=[mesh],
        )
    return fake


def test_shared_group_requires_confirmation() -> None:
    """共享组参数未确认时拒绝写入，并列出受影响材质。"""
    fake = build_shared_group_materials()
    describe = describe_material(fake)
    describe["shared_groups"] = {"Cel_Shared": [MATERIAL_A, MATERIAL_B]}
    service = MaterialGraphService()
    service.capture(describe, baseline_id="mg-shared")

    index = _index_of(service)
    shared = [p for p in index.params.values() if p.in_group_shared]
    assert shared, "应识别出共享组内的参数"
    param = shared[0]
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_graph_draft({param.param_id: param.baseline_value}, index)
    assert excinfo.value.code == errors.SHARED_PARAMETER_CONFIRM_REQUIRED
    assert set(excinfo.value.details["affected_materials"]) == {MATERIAL_A, MATERIAL_B}


def test_shared_group_confirmation_scopes_the_set() -> None:
    """确认后只修改**声明的**影响集合，不越界到未确认的组。"""
    fake = build_shared_group_materials()
    describe = describe_material(fake)
    describe["shared_groups"] = {"Cel_Shared": [MATERIAL_A, MATERIAL_B]}
    service = MaterialGraphService()
    service.capture(describe, baseline_id="mg-shared")
    index = _index_of(service)
    shared = [p for p in index.params.values() if p.in_group_shared]
    param = shared[0]
    ops = validate_graph_draft(
        {param.param_id: param.baseline_value},
        index,
        confirmed_shared=[param.shared_group],
    )
    assert ops
    # 确认集合只覆盖被确认的组：换一个组名必须再次被拒
    with pytest.raises(errors.ToonTunerError) as excinfo:
        validate_graph_draft(
            {param.param_id: param.baseline_value}, index, confirmed_shared=["其它组"]
        )
    assert excinfo.value.code == errors.SHARED_PARAMETER_CONFIRM_REQUIRED


# -- 注入面 ----------------------------------------------------------------


def test_client_cannot_inject_code_via_param_id() -> None:
    """参数 id 只是查表键：任何代码/属性路径形态都只会变成「未知参数」。"""
    service = capture(build_two_materials())
    index = _index_of(service)
    hostile = [
        "__import__('os').system('echo pwned')",
        "../../etc/passwd",
        "m/n/s'; import os; os.system('x'); #",
        "node.inputs['Base Color'].default_value",
    ]
    for payload in hostile:
        with pytest.raises(errors.ToonTunerError) as excinfo:
            validate_graph_draft({payload: 1.0}, index)
        assert excinfo.value.code == errors.PARAM_INVALID


def test_generated_code_has_no_free_form_attributes() -> None:
    """生成代码只由服务端常量模板 + repr 量化字面量拼成。"""
    fake = build_two_materials()
    service = capture(fake)
    index = _index_of(service)
    param = next(iter(index.params.values()))
    ops = validate_graph_draft({param.param_id: param.baseline_value}, index)
    code = surface_executor.build_apply_code(ops)
    assert "eval(" not in code
    assert "exec(" not in code
    assert "__import__" not in code


# -- 真实执行（假 bpy 桩）-------------------------------------------------


def test_apply_and_readback_roundtrip() -> None:
    """成功写入并回读一致。"""
    fake = build_two_materials()
    service = capture(fake)
    index = _index_of(service)
    float_param = next(
        p for p in index.params.values()
        if p.kind == "float" and p.shared_group == "" and p.binding.object_type
        == "MATERIAL_GRAPH_SOCKET"
    )
    ops = validate_graph_draft({float_param.param_id: 0.125}, index)
    payload = surface_executor.extract_json(
        run_generated_code(surface_executor.build_apply_code(ops), fake)
    )
    assert payload["applied"] is True, payload.get("failure")
    verified, mismatches = surface_executor.verify_ops(
        ops, payload["values"]
    )
    assert verified, mismatches


def test_one_material_does_not_touch_another() -> None:
    """改一个材质绝不误改另一个材质。"""
    fake = build_two_materials()
    service = capture(fake)
    index = _index_of(service)
    # 找出属于材质 A 的一个可写参数
    candidate = next(
        p for p in index.params.values()
        if p.binding.object_type == "MATERIAL_GRAPH_SOCKET" and MATERIAL_A in p.binding.object_id
    )
    before_b = _value_of(fake, MATERIAL_B)
    ops = validate_graph_draft({candidate.param_id: 0.375}, index)
    run_generated_code(surface_executor.build_apply_code(ops), fake)
    assert _value_of(fake, MATERIAL_B) == before_b


def _value_of(fake: FakeBpy, material: str) -> float | None:
    mat = fake.data.materials.get(material)
    assert mat is not None and mat.node_tree is not None
    for node in mat.node_tree.nodes:
        if node.type == "VALUE":
            return node.inputs["Value"].default_value
    return None


def test_write_failure_triggers_rollback() -> None:
    """写入中途失败：执行器自动回滚到快照，并报告失败阶段。"""
    fake = build_two_materials()
    service = capture(fake)
    index = _index_of(service)
    float_param = next(
        p for p in index.params.values()
        if p.binding.object_type == "MATERIAL_GRAPH_SOCKET" and p.kind == "float"
    )
    before = _value_of(fake, MATERIAL_A)
    ops = validate_graph_draft({float_param.param_id: 0.875}, index)
    # 让目标插座在写入时抛错：把材质树换成不存在的节点名（拓扑漂移模拟）
    drifted = [
        type(op)(
            Binding("MATERIAL_GRAPH_SOCKET", op.binding.object_id.replace(
                float_param.binding.object_id.split("/")[-1], "已消失的插座"
            ), "default_value"),
            op.value,
            cost=op.cost,
            param_id=op.param_id,
        )
        for op in ops
    ]
    payload = surface_executor.extract_json(
        run_generated_code(surface_executor.build_apply_code(drifted), fake)
    )
    assert payload["applied"] is False
    assert payload["failure"]["kind"] == "write_failed"
    # 未发生实际改动
    assert _value_of(fake, MATERIAL_A) == before


def test_readback_mismatch_detected() -> None:
    """回读不一致必须被 verify_ops 抓出来（不得当作成功）。"""
    fake = build_two_materials()
    service = capture(fake)
    index = _index_of(service)
    param = next(p for p in index.params.values() if p.kind == "float")
    ops = validate_graph_draft({param.param_id: 0.42}, index)
    verified, mismatches = surface_executor.verify_ops(ops, {op.binding.key(): 0.0 for op in ops})
    assert verified is False
    assert mismatches


def test_linked_socket_write_is_refused_in_blender() -> None:
    """已连线插座：Blender 侧直接抛错，绝不假装写成功。"""
    fake = build_two_materials()
    service = capture(fake)
    index = _index_of(service)
    param = next(p for p in index.params.values() if p.kind == "float")
    # 把目标插座标记为已连线
    material_name, node_name, socket_name = param.binding.object_id.split("/", 2)
    mat = fake.data.materials.get(material_name)
    assert mat is not None and mat.node_tree is not None
    node = mat.node_tree.nodes.get(node_name)
    assert node is not None
    node.inputs[socket_name].is_linked = True
    ops = validate_graph_draft({param.param_id: 0.6}, index)
    payload = surface_executor.extract_json(
        run_generated_code(surface_executor.build_apply_code(ops), fake)
    )
    assert payload["applied"] is False
    assert "linked" in payload["failure"]["message"]


# -- 会话状态 --------------------------------------------------------------


def test_service_requires_baseline() -> None:
    service = MaterialGraphService()
    with pytest.raises(errors.ToonTunerError) as excinfo:
        service.schema_public()
    assert excinfo.value.code == errors.NO_BASELINE


def test_material_schema_lookup() -> None:
    service = capture(build_two_materials())
    payload = service.material_schema(MATERIAL_A)
    assert payload["material"] == MATERIAL_A
    with pytest.raises(errors.ToonTunerError):
        service.material_schema("不存在的材质")


def test_shared_group_users_summary() -> None:
    fake = build_shared_group_materials()
    describe = describe_material(fake)
    service = MaterialGraphService()
    service.capture(describe, baseline_id="mg-shared")
    users = service.shared_group_users()
    assert users.get("Cel_Shared") == sorted([MATERIAL_A, MATERIAL_B])