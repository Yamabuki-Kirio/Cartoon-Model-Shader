"""材质深度识别（提交 2）：反向追踪 + 语义/置信度 + 递归参数 schema。

钉住规格「提交 2」验收条目：
* 标准 BSDF 链；已连接输入继续向上追踪；
* Value / RGB / ColorRamp / Math / Map Range / 颜色调整 / 混合；
* 混合两侧路径；一层与多层节点组；循环节点组；深度超过 8；多输出歧义；
* 结构哈希不受数值变化影响；共享节点组影响范围正确；推测结果不被标为确定。

所有断言跑在假 ``bpy`` 桩上：桩**真实执行**服务端生成的探针代码。
"""

from __future__ import annotations

from typing import Any

from src.server import surface_probe
from src.server.material_graph import schema as mg_schema
from src.server.material_graph import semantics, trace
from src.server.material_graph.graph import build_graph
from src.server.material_graph.probe import build_material_graph
from tests.fake_bpy import FakeBpy, FakeColorRamp, FakeLink, FakeNode, run_generated_code

MATERIAL = "皮肤_脸"


def _in(node: FakeNode, name: str):
    return node.inputs[name]


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


def _graph(fake: FakeBpy, name: str):
    describe = describe_material(fake)
    material = build_material_graph(describe, name)
    assert material.available
    return material, material.graph


def build_full_graph() -> tuple[FakeBpy, str]:
    """覆盖语义：Value / RGB / ColorRamp / Math / Map Range / HueSat / Mix。"""
    fake = FakeBpy()
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None

    output = FakeNode("Material Output", "OUTPUT_MATERIAL", {"Surface": None},
                      bl_idname="ShaderNodeOutputMaterial")
    bsdf = FakeNode("Principled BSDF", "BSDF_PRINCIPLED",
                    {"Base Color": [1.0, 1.0, 1.0, 1.0], "Roughness": 0.5},
                    bl_idname="ShaderNodeBsdfPrincipled", outputs={"BSDF": "SHADER"})
    hue = FakeNode("Hue Saturation", "HUE_SAT",
                   {"Color": [0.9, 0.4, 0.4, 1.0], "Fac": 1.0, "Hue": 0.5,
                    "Saturation": 1.0, "Value": 1.0},
                   bl_idname="ShaderNodeHueSaturation", outputs={"Color": "RGBA"})
    mix = FakeNode("Mix", "MIX_RGB",
                   {"Fac": 0.5, "Color A": [0.9, 0.4, 0.4, 1.0],
                    "Color B": [0.2, 0.2, 0.3, 1.0], "Color": [0.5, 0.5, 0.5, 1.0]},
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
    map_range = FakeNode("Map Range", "MAP_RANGE",
                         {"Value": 0.5, "From Min": 0.0, "From Max": 1.0,
                          "To Min": 0.0, "To Max": 1.0},
                         bl_idname="ShaderNodeMapRange", outputs={"Result": "VALUE"})

    links = [
        FakeLink(hue, _out(hue, "Color"), bsdf, _in(bsdf, "Base Color")),
        FakeLink(mix, _out(mix, "Color"), hue, _in(hue, "Color")),
        FakeLink(rgb, _out(rgb, "Color"), mix, _in(mix, "Color A")),
        FakeLink(ramp, _out(ramp, "Color"), mix, _in(mix, "Color B")),
        FakeLink(value, _out(value, "Value"), ramp, _in(ramp, "Fac")),
        FakeLink(map_range, _out(map_range, "Result"), bsdf, _in(bsdf, "Roughness")),
        FakeLink(math, _out(math, "Value"), map_range, _in(map_range, "Value")),
        FakeLink(bsdf, _out(bsdf, "BSDF"), output, _in(output, "Surface")),
    ]
    fake.add_material_graph(
        MATERIAL,
        nodes=[output, bsdf, hue, mix, rgb, ramp, value, math, map_range],
        links=links,
        users=[mesh],
    )
    return fake, MATERIAL


def build_shader_graph() -> tuple[FakeBpy, str]:
    """标准 BSDF 链：Principled ← Add Shader ← (Diffuse, Emission)。"""
    fake = FakeBpy()
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None
    output = FakeNode("Material Output", "OUTPUT_MATERIAL", {"Surface": None},
                      bl_idname="ShaderNodeOutputMaterial")
    add = FakeNode("Add Shader", "ADD_SHADER",
                   {"Shader": None, "Shader_001": None},
                   bl_idname="ShaderNodeAddShader", outputs={"Shader": "SHADER"})
    diffuse = FakeNode("Diffuse BSDF", "BSDF_DIFFUSE",
                       {"Color": [0.8, 0.3, 0.3, 1.0], "Roughness": 0.4},
                       bl_idname="ShaderNodeBsdfDiffuse", outputs={"BSDF": "SHADER"})
    emission = FakeNode("Emission", "EMISSION",
                        {"Color": [0.1, 0.6, 0.9, 1.0], "Strength": 1.5},
                        bl_idname="ShaderNodeEmission", outputs={"Emission": "SHADER"})
    links = [
        FakeLink(diffuse, _out(diffuse, "BSDF"), add, _in(add, "Shader")),
        FakeLink(emission, _out(emission, "Emission"), add, _in(add, "Shader_001")),
        FakeLink(add, _out(add, "Shader"), output, _in(output, "Surface")),
    ]
    fake.add_material_graph(MATERIAL, nodes=[output, add, diffuse, emission],
                            links=links, users=[mesh])
    return fake, MATERIAL


# -- 反向追踪 --------------------------------------------------------------


def test_trace_reaches_all_nodes() -> None:
    fake, name = build_full_graph()
    _material, graph = _graph(fake, name)
    result = trace.trace_from_output(graph)
    assert result.ok
    reachable = {item.name for item in result.nodes}
    assert {
        "Principled BSDF", "Hue Saturation", "Mix", "RGB", "ColorRamp",
        "Value", "Math", "Map Range",
    } <= reachable
    assert result.entry == "Principled BSDF"
    assert result.entry_output == "BSDF"
    assert "OUTPUT_MATERIAL" not in {item.node.type for item in result.nodes}


def test_trace_excludes_isolated_nodes() -> None:
    """孤立节点（未连到输出）不进入生效链路。"""
    fake, name = build_full_graph()
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None
    orphan = FakeNode("Orphan Value", "VALUE", {"Value": 9.0},
                      bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"})
    material = build_material_graph(
        {"materials": [{"name": name, "users": ["model_mesh"], "graph": {
            "nodes": [n for n in describe_material(fake)["materials"][0]["graph"]["nodes"]]
            + [{
                "name": "Orphan Value", "type": "VALUE", "bl_idname": "ShaderNodeValue",
                "mute": False, "label": "Orphan Value", "group_name": "",
                "inputs": [{"name": "Value", "type": "VALUE", "linked": False, "value": 9.0}],
                "outputs": [{"name": "Value", "type": "VALUE", "linked": False}],
                "color_ramp": None,
            }],
            "links": describe_material(fake)["materials"][0]["graph"]["links"],
        }}], "shared_groups": {}},
        name,
    )
    result = trace.trace_from_output(material.graph)
    assert "Orphan Value" not in {item.name for item in result.nodes}


def test_trace_marks_depth_and_downstream() -> None:
    fake, name = build_full_graph()
    _material, graph = _graph(fake, name)
    result = trace.trace_from_output(graph)
    index = result.index()
    assert index["Value"].depth > index["RGB"].depth
    assert "Mix" in index["ColorRamp"].downstream
    assert result.max_depth >= 4


def test_trace_keeps_all_influence_paths() -> None:
    """同一节点被多条路径访问时去重，但保留全部影响路径。"""
    fake, name = build_full_graph()
    _material, graph = _graph(fake, name)
    result = trace.trace_from_output(graph)
    index = result.index()
    # RGB 只经 Mix → HueSat 一条路
    assert len(index["RGB"].influence_paths) == 1
    # 影响路径是「输出端在前」的节点链，末端是 RGB
    assert index["RGB"].influence_paths[0][-1] == "RGB"
    assert index["RGB"].influence_paths[0][0] == "Principled BSDF"


def test_trace_records_used_outputs() -> None:
    """多输出节点要记录实际被取用的输出插座。"""
    fake, name = build_shader_graph()
    _material, graph = _graph(fake, name)
    result = trace.trace_from_output(graph)
    index = result.index()
    assert index["Diffuse BSDF"].used_outputs == ["BSDF"]
    assert index["Emission"].used_outputs == ["Emission"]


def test_trace_cycle_degrades() -> None:
    fake = FakeBpy()
    a = FakeNode("A", "VALUE", {"Value": 0.0}, bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"})
    b = FakeNode("B", "VALUE", {"Value": 0.0}, bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"})
    out = FakeNode("Material Output", "OUTPUT_MATERIAL", {"Surface": None},
                   bl_idname="ShaderNodeOutputMaterial")
    links = [
        FakeLink(a, _out(a, "Value"), b, _in(b, "Value")),
        FakeLink(b, _out(b, "Value"), a, _in(a, "Value")),
        FakeLink(a, _out(a, "Value"), out, _in(out, "Surface")),
    ]
    graph = build_graph("m", {
        "nodes": [_node_raw(n) for n in (a, b, out)],
        "links": [_link_raw(l) for l in links],
    })
    result = trace.trace_from_output(graph)
    assert not result.ok
    assert result.degraded_reason == semantics.REASON_CYCLE
    assert result.cycle


def test_trace_no_output_is_not_found() -> None:
    fake = FakeBpy()
    value = FakeNode("Value", "VALUE", {"Value": 0.0}, bl_idname="ShaderNodeValue",
                     outputs={"Value": "VALUE"})
    graph = build_graph("m", {"nodes": [_node_raw(value)], "links": []})
    result = trace.trace_from_output(graph)
    assert not result.ok
    assert result.degraded_reason == semantics.REASON_NOT_FOUND


def test_trace_too_deep_degrades() -> None:
    fake = FakeBpy()
    nodes = []
    links = []
    prev = None
    for i in range(12):
        node = FakeNode(f"V{i}", "VALUE", {"Value": float(i)}, bl_idname="ShaderNodeValue",
                        outputs={"Value": "VALUE"})
        nodes.append(node)
        if prev is not None:
            links.append(FakeLink(prev, _out(prev, "Value"), node, _in(node, "Value")))
        prev = node
    out = FakeNode("Material Output", "OUTPUT_MATERIAL", {"Surface": None},
                   bl_idname="ShaderNodeOutputMaterial")
    assert prev is not None
    links.append(FakeLink(prev, _out(prev, "Value"), out, _in(out, "Surface")))
    nodes.append(out)
    graph = build_graph("m", {"nodes": [_node_raw(n) for n in nodes],
                              "links": [_link_raw(l) for l in links]})
    result = trace.trace_from_output(graph, max_depth=4)
    assert not result.ok
    assert result.degraded_reason == semantics.REASON_TOO_DEEP


def test_ambiguous_path_detected() -> None:
    fake = FakeBpy()
    value = FakeNode("Value", "VALUE", {"Value": 0.5}, bl_idname="ShaderNodeValue",
                     outputs={"Value": "VALUE"})
    m1 = FakeNode("Math1", "MATH", {"Value": 0.0}, bl_idname="ShaderNodeMath", outputs={"Value": "VALUE"})
    m2 = FakeNode("Math2", "MATH", {"Value": 0.0}, bl_idname="ShaderNodeMath", outputs={"Value": "VALUE"})
    links = [
        FakeLink(value, _out(value, "Value"), m1, _in(m1, "Value")),
        FakeLink(value, _out(value, "Value"), m2, _in(m2, "Value")),
    ]
    graph = build_graph("m", {
        "nodes": [_node_raw(n) for n in (value, m1, m2)],
        "links": [_link_raw(l) for l in links],
    })
    assert trace.detect_ambiguous(graph, "Value") is True
    assert trace.detect_ambiguous(graph, "Math1") is False


# -- 语义与置信度 ----------------------------------------------------------


def test_semantics_recognises_all_categories() -> None:
    fake, name = build_full_graph()
    _material, graph = _graph(fake, name)
    result = trace.trace_from_output(graph)
    by_type = {item.node.type: item.recognition.semantic for item in result.nodes}
    assert by_type["VALUE"] == semantics.SEM_VALUE
    assert by_type["RGB"] == semantics.SEM_COLOR
    assert by_type["VALTORGB"] == semantics.SEM_RAMP
    assert by_type["MATH"] == semantics.SEM_MATH
    assert by_type["MAP_RANGE"] == semantics.SEM_MAP_RANGE
    assert by_type["HUE_SAT"] == semantics.SEM_HUE_SAT
    assert by_type["MIX_RGB"] == semantics.SEM_MIX


def test_semantics_shader_chain() -> None:
    fake, name = build_shader_graph()
    _material, graph = _graph(fake, name)
    result = trace.trace_from_output(graph)
    by_type = {item.node.type: item.recognition for item in result.nodes}
    assert by_type["BSDF_DIFFUSE"].semantic == semantics.SEM_SHADER
    assert by_type["EMISSION"].semantic == semantics.SEM_SHADER
    assert by_type["ADD_SHADER"].semantic == semantics.SEM_MIX_SHADER


def test_semantics_supported_node_list() -> None:
    """规格列出的首批支持节点类型都必须被识别（至少不是 other）。"""
    supported = [
        "BSDF_PRINCIPLED", "BSDF_DIFFUSE", "BSDF_TOON", "EMISSION",
        "MIX_SHADER", "ADD_SHADER", "MIX_RGB", "MIX", "VALUE", "RGB",
        "VALTORGB", "MATH", "MAP_RANGE", "HUE_SAT", "BRIGHTCONTRAST",
        "GAMMA", "INVERT", "REROUTE",
    ]
    for node_type in supported:
        semantic = semantics.semantic_of(node_type)
        assert semantic != semantics.SEM_OTHER, f"{node_type} 应被识别"


def test_semantics_levels() -> None:
    fake, name = build_full_graph()
    _material, graph = _graph(fake, name)
    result = trace.trace_from_output(graph)
    by_type = {item.node.type: item.recognition for item in result.nodes}
    assert by_type["VALUE"].level == semantics.LEVEL_CONFIRMED
    assert by_type["RGB"].level == semantics.LEVEL_CONFIRMED
    assert by_type["VALTORGB"].level == semantics.LEVEL_CONFIRMED
    assert by_type["MATH"].level == semantics.LEVEL_CONFIRMED
    assert by_type["MAP_RANGE"].level == semantics.LEVEL_CONFIRMED
    # 混合 / 色相：推断（随模式变化，需真机确认）
    assert by_type["MIX_RGB"].level == semantics.LEVEL_INFERRED
    assert by_type["HUE_SAT"].level == semantics.LEVEL_INFERRED


def test_semantics_never_invents_user_confirmed() -> None:
    """没有用户确认存储时，探测流程**从不**产生 user_confirmed。"""
    fake, name = build_full_graph()
    _material, graph = _graph(fake, name)
    result = trace.trace_from_output(graph)
    assert all(
        item.recognition.level != semantics.LEVEL_USER_CONFIRMED for item in result.nodes
    )


def test_semantics_unknown_type_is_unsupported() -> None:
    node = FakeNode("Weird", "TOTALLY_UNKNOWN", {"Foo": 1.0}, bl_idname="ShaderNodeWeird")
    recognition = semantics.classify(node)
    assert recognition.semantic == semantics.SEM_OTHER
    assert recognition.level == semantics.LEVEL_UNSUPPORTED
    assert not recognition.writable


def test_semantics_linked_socket_not_editable() -> None:
    node = FakeNode("Value", "VALUE", {"Value": 0.5}, bl_idname="ShaderNodeValue",
                    outputs={"Value": "VALUE"})
    node.inputs["Value"].is_linked = True
    assert semantics.editable_sockets(node, semantics.SEM_VALUE) == []


# -- 节点组穿透 ------------------------------------------------------------


def _group_graph(depth: int = 1) -> tuple[FakeBpy, str]:
    """构造 ``depth`` 层嵌套节点组。

    每个组实例的输入插座由 ``<前缀>::Group Output`` 喂入，而其来源是
    ``<前缀>::Group Input``（被更内层喂）。组内节点以扁平化命名并入材质节点表，
    因此反向追踪沿入边即可从组实例穿到组内、再到更内层。
    """
    fake = FakeBpy()
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None
    out = FakeNode("Material Output", "OUTPUT_MATERIAL", {"Surface": None},
                   bl_idname="ShaderNodeOutputMaterial")
    bsdf = FakeNode("Principled BSDF", "BSDF_PRINCIPLED", {"Base Color": [1.0, 1.0, 1.0, 1.0]},
                    bl_idname="ShaderNodeBsdfPrincipled", outputs={"BSDF": "SHADER"})

    nodes: list[FakeNode] = [out, bsdf]
    links: list[FakeLink] = [FakeLink(bsdf, _out(bsdf, "BSDF"), out, _in(out, "Surface"))]

    # 内层来源：一个 Value 节点（位于 G0 组内）
    inner_value = FakeNode("G0::Value", "VALUE", {"Value": 0.3},
                           bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"})
    nodes.append(inner_value)

    # 每层的内部「值」节点：G0::Value 是最内层；更外层各有一个中间节点。
    # 结构（自内向外）：G0::Value →(G0::Group Output)→ G0 实例 →(G1::Group Input)
    #                   → G1::Mid →(G1::Group Output)→ G1 实例 → bsdf
    source = inner_value  # 有输出插座
    outer_instance = None
    for level in range(depth):
        prefix = "::".join(f"G{i}" for i in range(level + 1))
        group_in = FakeNode(f"{prefix}::Group Input", "GROUP_INPUT", {"Value": 0.0},
                            bl_idname="NodeGroupInput", outputs={"Value": "VALUE"})
        group_out = FakeNode(f"{prefix}::Group Output", "GROUP_OUTPUT", {"Value": 0.0},
                             bl_idname="NodeGroupOutput")
        mid = FakeNode(f"{prefix}::Mid", "VALUE", {"Value": 0.0},
                       bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"})
        instance = FakeNode(prefix, "GROUP", {"Value": 0.0}, bl_idname="ShaderNodeGroup",
                            outputs={"Color": "RGBA", "Value": "VALUE"})
        instance.node_tree = type("NG", (), {"name": prefix})()
        nodes.extend([group_in, group_out, mid, instance])
        # Group Input → 内部中间节点（接口输入映射）
        links.append(FakeLink(group_in, _out(group_in, "Value"), mid, _in(mid, "Value")))
        # 中间节点 → Group Output（接口输出映射，供 group_interface_map 使用）
        links.append(FakeLink(mid, _out(mid, "Value"), group_out, _in(group_out, "Value")))
        # 上一层来源 → 本层 Group Input
        links.append(FakeLink(source, _out(source, "Value"),
                              group_in, _in(group_in, "Value")))
        # 组内输出 → 实例输入插座（穿透：实例的输入由组内节点喂）
        links.append(FakeLink(mid, _out(mid, "Value"),
                              instance, _in(instance, "Value")))
        source = instance
        outer_instance = instance

    # 最外层实例 → bsdf.Base Color
    assert outer_instance is not None
    links.append(FakeLink(outer_instance, _out(outer_instance, "Color"),
                          bsdf, _in(bsdf, "Base Color")))
    fake.add_material_graph(MATERIAL, nodes=nodes, links=links, users=[mesh])
    return fake, MATERIAL


def test_group_penetration_single_level() -> None:
    fake, name = _group_graph(depth=1)
    _material, graph = _graph(fake, name)
    result = trace.trace_from_output(graph)
    assert result.ok
    names = {item.name for item in result.nodes}
    assert "G0::Value" in names
    assert "G0::Mid" in names
    index = result.index()
    assert index["G0::Value"].in_group == "G0"


def test_group_penetration_nested() -> None:
    fake, name = _group_graph(depth=2)
    _material, graph = _graph(fake, name)
    result = trace.trace_from_output(graph)
    names = {item.name for item in result.nodes}
    assert any("G1::G0::" in item_name or "G1::" in item_name for item_name in names)


def test_group_interface_map() -> None:
    fake, name = _group_graph(depth=1)
    _material, graph = _graph(fake, name)
    mapping = trace.group_interface_map(graph)
    assert "G0" in mapping
    assert mapping["G0"]["internal"]
    assert mapping["G0"]["outputs"]


# -- 递归参数 schema -------------------------------------------------------


def test_schema_is_recursive_and_has_spec_fields() -> None:
    fake, name = build_full_graph()
    material, _graph_ = _graph(fake, name)
    payload = mg_schema.build_parameter_schema(material)
    assert payload["available"] is True
    assert payload["structure_hash"].startswith("sha256:")
    params = payload["parameters"]
    assert params
    for spec in params:
        # 规格 §2 的字段一个都不能少
        for key in (
            "id", "label", "kind", "confidence", "semantic", "evidence",
            "influence_paths", "affected_materials", "shared", "editable", "category",
        ):
            assert key in spec, f"参数缺少字段 {key}"
        assert spec["confidence"] in semantics.LEVELS


def test_schema_categories_are_valid() -> None:
    fake, name = build_full_graph()
    material, _graph_ = _graph(fake, name)
    payload = mg_schema.build_parameter_schema(material)
    for spec in payload["parameters"]:
        assert spec["category"] in semantics.CATEGORIES
    # inferred 分区应包含混合/色相这类推测项
    assert payload["categories"]["inferred"]


def test_schema_only_confirmed_writable() -> None:
    fake, name = build_full_graph()
    material, _graph_ = _graph(fake, name)
    payload = mg_schema.build_parameter_schema(material)
    for spec in payload["parameters"]:
        if spec["confidence"] == semantics.LEVEL_INFERRED:
            assert spec["editable"] is False
            assert spec["readonly_reason"]
        if spec["editable"]:
            assert spec["confidence"] in semantics.WRITABLE_LEVELS


def test_schema_inferred_not_marked_confirmed() -> None:
    """推测结果不得被标为确定（规格硬约束）。"""
    fake, name = build_full_graph()
    material, _graph_ = _graph(fake, name)
    payload = mg_schema.build_parameter_schema(material)
    mix_params = [s for s in payload["parameters"] if s["semantic"] == semantics.SEM_MIX]
    assert mix_params
    assert all(s["confidence"] != semantics.LEVEL_CONFIRMED for s in mix_params)


def test_schema_linked_socket_marked_readonly() -> None:
    fake, name = build_full_graph()
    material, _graph_ = _graph(fake, name)
    payload = mg_schema.build_parameter_schema(material)
    linked = [
        spec for spec in payload["parameters"]
        if "Base Color" in str(spec.get("label") or "") and spec["editable"] is False
    ]
    assert linked, "已连线的输入插座应生成只读占位"
    assert linked[0]["readonly_reason"]


def test_schema_ramp_is_single_composite_node() -> None:
    fake, name = build_full_graph()
    material, _graph_ = _graph(fake, name)
    payload = mg_schema.build_parameter_schema(material)
    ramps = [spec for spec in payload["parameters"] if spec["kind"] == "ramp"]
    assert len(ramps) == 1
    assert ramps[0]["elements"]
    assert ramps[0]["element_count"]["editable"] is False


def test_schema_has_influence_paths_and_materials() -> None:
    fake, name = build_full_graph()
    material, _graph_ = _graph(fake, name)
    payload = mg_schema.build_parameter_schema(material)
    value_params = [s for s in payload["parameters"] if s["semantic"] == semantics.SEM_VALUE]
    assert value_params
    assert value_params[0]["influence_paths"]
    assert value_params[0]["affected_materials"]


def test_schema_degraded_material_is_readonly() -> None:
    value = FakeNode("Value", "VALUE", {"Value": 0.0}, bl_idname="ShaderNodeValue",
                     outputs={"Value": "VALUE"})
    material = build_material_graph(
        {"materials": [{"name": "m", "users": ["obj"], "graph": {
            "nodes": [_node_raw(value)], "links": [],
        }}], "shared_groups": {}},
        "m",
    )
    payload = mg_schema.build_parameter_schema(material)
    assert payload["available"] is True
    assert payload["degraded_reason"] == semantics.REASON_NOT_FOUND
    assert all(spec["editable"] is False for spec in payload["parameters"])


def test_schema_unavailable_material() -> None:
    material = build_material_graph({"materials": [], "shared_groups": {}}, "不存在")
    payload = mg_schema.build_parameter_schema(material)
    assert payload["available"] is False
    assert payload["parameters"] == []


def test_schema_structure_hash_ignores_values() -> None:
    """结构哈希不受数值变化影响（规格硬约束）。"""
    fake, name = build_full_graph()
    material, _graph_ = _graph(fake, name)
    h1 = material.structure_hash

    raw = describe_material(fake)["materials"][0]["graph"]
    for node in raw["nodes"]:
        for sock in node["inputs"]:
            sock["value"] = 0.123456
    material2 = build_material_graph(
        {"materials": [{"name": name, "users": ["model_mesh"], "graph": raw}],
         "shared_groups": {}},
        name,
    )
    assert material2.structure_hash == h1


def test_build_schemas_aggregates_levels() -> None:
    fake, name = build_full_graph()
    material, _graph_ = _graph(fake, name)
    payload = mg_schema.build_schemas([material])
    assert payload["material_count"] == 1
    assert payload["parameter_count"] > 0
    assert payload["levels"].get(semantics.LEVEL_CONFIRMED, 0) > 0


def test_schema_bindings_all_whitelisted() -> None:
    """schema 里出现的每个 binding 都必须过白名单（启动期自检同一条防线）。"""
    from src.server.surface.binding import assert_allowed
    from src.server.surface.schema import GroupNode, collect_bindings, flatten

    fake, name = build_full_graph()
    material, _graph_ = _graph(fake, name)
    graph = material.graph
    result = trace.trace_from_output(graph)
    nodes = []
    for item in result.nodes:
        nodes.extend(mg_schema._node_children(item, material, graph=graph))
    group = GroupNode(id="g", kind="group", group=mg_schema.GROUP, cost="L1", children=nodes)
    flatten([group])  # id 唯一性校验
    for binding in collect_bindings([group]):
        assert_allowed(binding)


# -- 共享节点组 ------------------------------------------------------------


def test_shared_group_marks_shared_and_affected_materials() -> None:
    """共享节点组：参数标 shared=true，影响材质 = 组的使用者。

    共享组内参数**不**在 schema 阶段一律只读 —— 规格要求「修改前必须明确确认
    影响范围」，而确认发生在提交/执行时（``SHARED_PARAMETER_CONFIRM_REQUIRED``）。
    若这里就置为只读，执行层的确认闸门将永远不可达。
    """
    from src.server.material_graph.probe import build_material_graph as bmg

    fake, name = _group_graph(depth=1)
    describe = describe_material(fake)
    # 注入共享信息：G0 被两个材质引用
    describe["shared_groups"] = {"G0": ["皮肤_脸", "衣服"]}
    material = bmg(describe, name)
    payload = mg_schema.build_parameter_schema(material)
    in_group = [s for s in payload["parameters"] if s["in_group"]]
    assert in_group, "应含组内参数"
    for spec in in_group:
        assert spec["shared"] is True
        assert set(spec["affected_materials"]) == {"皮肤_脸", "衣服"}
    # 组**实例**节点本身仍只读（没有可写 binding）
    instance = [s for s in payload["parameters"] if s["semantic"] == semantics.SEM_GROUP]
    for spec in instance:
        assert spec["editable"] is False
        assert spec["readonly_reason"]


# -- 桩工具 ----------------------------------------------------------------


def _node_raw(node: FakeNode) -> dict[str, Any]:
    return {
        "name": node.name,
        "type": node.type,
        "bl_idname": node.bl_idname,
        "mute": node.mute,
        "label": node.label,
        "group_name": getattr(getattr(node, "node_tree", None), "name", "") or "",
        "inputs": [
            {"name": s.name, "type": s.type, "linked": s.is_linked, "value": s.default_value}
            for s in node.inputs
        ],
        "outputs": [{"name": s.name, "type": s.type, "linked": s.is_linked} for s in node.outputs],
        "color_ramp": None,
    }


def _link_raw(link: FakeLink) -> dict[str, str]:
    return {
        "from_node": link.from_node.name,
        "from_socket": link.from_socket.name,
        "to_node": link.to_node.name,
        "to_socket": link.to_socket.name,
    }
