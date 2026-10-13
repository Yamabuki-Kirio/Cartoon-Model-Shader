"""材质深度识别（提交 1）：节点图探测 + 图模型 + 稳定身份。

钉住规格 §11 中与「图 / 身份」相关的条目：
* 从 Material Output → Surface 正确取到入口着色器；
* 已连接输入继续向上游可达；
* 循环依赖可检出；
* 结构哈希**不含数值**（调值不让身份漂移）；
* 参数 ID 稳定（改节点显示名不影响 param_id）；
* 节点增删 / 连线变化 会改变结构哈希。

所有断言跑在假 ``bpy`` 桩上：桩**真实执行**服务端生成的探针代码。
"""

from __future__ import annotations

from typing import Any

from src.server import surface_probe
from src.server.material_graph import identity
from src.server.material_graph.graph import (
    build_graph,
    entry_source,
    find_cycle,
    reachable_from,
)
from src.server.material_graph.probe import build_material_graph
from tests.fake_bpy import FakeBpy, FakeLink, FakeNode, run_generated_code

MATERIAL = "皮肤_脸"


# -- 桩工具 ----------------------------------------------------------------


def _in(node: FakeNode, name: str):
    return node.inputs[name]


def _out(node: FakeNode, name: str):
    for sock in node.outputs:
        if sock.name == name:
            return sock
    raise KeyError(f"{node.name} 没有输出插座 {name}")


def build_fake_graph() -> tuple[FakeBpy, str]:
    """Principled BSDF，Base Color ← Mix Color ← (RGB, Value)；Roughness ← Value。"""
    fake = FakeBpy()
    mesh = fake.data.objects.get("model_mesh")
    assert mesh is not None

    output = FakeNode(
        "Material Output",
        "OUTPUT_MATERIAL",
        {"Surface": None},
        bl_idname="ShaderNodeOutputMaterial",
    )
    bsdf = FakeNode(
        "Principled BSDF",
        "BSDF_PRINCIPLED",
        {"Base Color": [1.0, 1.0, 1.0, 1.0], "Roughness": 0.5, "Metallic": 0.0},
        bl_idname="ShaderNodeBsdfPrincipled",
        outputs={"BSDF": "SHADER"},
    )
    mix = FakeNode(
        "Mix Color",
        "MIX_RGB",
        {"Color A": [0.9, 0.4, 0.4, 1.0], "Color B": [0.2, 0.2, 0.3, 1.0], "Factor": 0.5},
        bl_idname="ShaderNodeMixRGB",
        outputs={"Color": "RGBA"},
    )
    rgb = FakeNode(
        "RGB",
        "RGB",
        {"Color": [0.9, 0.4, 0.4, 1.0]},
        bl_idname="ShaderNodeRGB",
        outputs={"Color": "RGBA"},
    )
    value = FakeNode(
        "Value",
        "VALUE",
        {"Value": 0.25},
        bl_idname="ShaderNodeValue",
        outputs={"Value": "VALUE"},
    )

    links = [
        FakeLink(mix, _out(mix, "Color"), bsdf, _in(bsdf, "Base Color")),
        FakeLink(rgb, _out(rgb, "Color"), mix, _in(mix, "Color A")),
        FakeLink(value, _out(value, "Value"), mix, _in(mix, "Factor")),
        FakeLink(value, _out(value, "Value"), bsdf, _in(bsdf, "Roughness")),
        FakeLink(bsdf, _out(bsdf, "BSDF"), output, _in(output, "Surface")),
    ]
    fake.add_material_graph(MATERIAL, nodes=[output, bsdf, mix, rgb, value], links=links, users=[mesh])
    return fake, MATERIAL


def describe_material(fake: FakeBpy) -> dict[str, Any]:
    return surface_probe.describe_groups(
        surface_probe.redact_describe(
            surface_probe.parse_describe(
                run_generated_code(surface_probe.build_describe_code(), fake)
            )
        )
    )


# -- 图与追踪 --------------------------------------------------------------


def test_probe_reports_material_graph() -> None:
    fake, name = build_fake_graph()
    describe = describe_material(fake)
    materials = {item["name"]: item for item in describe["materials"]}
    assert name in materials
    graph = materials[name]["graph"]
    assert graph is not None
    assert {n["name"] for n in graph["nodes"]} >= {"Material Output", "Principled BSDF", "Mix Color", "RGB", "Value"}
    assert graph["links"], "应报告连线"


def test_entry_source_is_unique_shader() -> None:
    fake, name = build_fake_graph()
    graph = build_graph(name, describe_material(fake)["materials"][0]["graph"])
    entry = entry_source(graph)
    assert entry is not None
    node, socket = entry
    assert node.type == "BSDF_PRINCIPLED"
    # 返回的是「来源节点」及其**输出**插座名（BSDF），而不是输出节点的输入插座。
    assert socket == "BSDF"


def test_reachable_from_entry_follows_upstream() -> None:
    fake, name = build_fake_graph()
    graph = build_graph(name, describe_material(fake)["materials"][0]["graph"])
    reachable = reachable_from(graph, "Principled BSDF")
    assert {"Mix Color", "RGB", "Value"} <= reachable


def test_cycle_detected() -> None:
    fake = FakeBpy()
    a = FakeNode("A", "VALUE", {"Value": 0.0}, bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"})
    b = FakeNode("B", "VALUE", {"Value": 0.0}, bl_idname="ShaderNodeValue", outputs={"Value": "VALUE"})
    links = [
        FakeLink(a, _out(a, "Value"), b, _in(b, "Value")),
        FakeLink(b, _out(b, "Value"), a, _in(a, "Value")),
    ]
    graph = build_graph("m", {"nodes": [_node_raw(a), _node_raw(b)], "links": [_link_raw(l) for l in links]})
    assert find_cycle(graph, "A") is not None


def _node_raw(node: FakeNode) -> dict[str, Any]:
    return {
        "name": node.name,
        "type": node.type,
        "bl_idname": node.bl_idname,
        "inputs": [{"name": s.name, "type": s.type, "linked": s.is_linked, "value": None} for s in node.inputs],
        "outputs": [{"name": s.name, "type": s.type, "linked": s.is_linked} for s in node.outputs],
    }


def _link_raw(link: FakeLink) -> dict[str, str]:
    return {
        "from_node": link.from_node.name,
        "from_socket": link.from_socket.name,
        "to_node": link.to_node.name,
        "to_socket": link.to_socket.name,
    }


# -- 身份与结构哈希 --------------------------------------------------------


def test_structure_hash_ignores_values() -> None:
    """改数值绝不改变结构哈希；改连线才改变。"""
    fake, name = build_fake_graph()
    graph = build_graph(name, describe_material(fake)["materials"][0]["graph"])
    h1 = identity.structure_hash(graph)

    # 构造同一拓扑但**不同数值**的图
    fake2, name2 = build_fake_graph()
    raw = describe_material(fake2)["materials"][0]["graph"]
    for node in raw["nodes"]:
        if node["name"] == "Value":
            for sock in node["inputs"]:
                sock["value"] = 0.99
        if node["name"] == "Principled BSDF":
            for sock in node["inputs"]:
                if sock["name"] == "Roughness":
                    sock["value"] = 0.87
    graph2 = build_graph(name2, raw)
    assert identity.structure_hash(graph2) == h1, "数值不应进入结构哈希"


def test_structure_hash_changes_on_link_change() -> None:
    fake, name = build_fake_graph()
    raw = describe_material(fake)["materials"][0]["graph"]
    h1 = identity.structure_hash(build_graph(name, raw))
    # 删掉一条连线
    raw2 = {**raw, "links": raw["links"][:-1]}
    h2 = identity.structure_hash(build_graph(name, raw2))
    assert h2 != h1


def test_param_id_stable_across_rename() -> None:
    """节点显示名变化不影响参数 ID（结构身份不依赖名称）。"""
    fake, name = build_fake_graph()
    raw = describe_material(fake)["materials"][0]["graph"]
    graph = build_graph(name, raw)
    ident = identity.identity_for(graph, "Value", "Value", "default_value", material_id="MATERIAL:m")
    assert ident is not None
    pid1 = ident.param_id()
    # 模拟重命名：改节点名（结构身份基于类型/插座，不基于名称）
    raw_renamed = {
        "nodes": [dict(n, name=("Value_renamed" if n["name"] == "Value" else n["name"])) for n in raw["nodes"]],
        "links": [
            dict(l, from_node=("Value_renamed" if l["from_node"] == "Value" else l["from_node"]))
            for l in raw["links"]
        ],
    }
    graph2 = build_graph(name, raw_renamed)
    ident2 = identity.identity_for(
        graph2, "Value_renamed", "Value", "default_value", material_id="MATERIAL:m"
    )
    assert ident2 is not None
    assert ident2.param_id() == pid1, "重命名不应改变参数 ID"


def test_probe_missing_material_is_unavailable() -> None:
    """找不到材质时明确不可用并给出原因，绝不伪造图。"""
    fake, _name = build_fake_graph()
    describe = describe_material(fake)
    mg = build_material_graph(describe, "不存在的材质")
    assert not mg.available
    assert mg.error


def test_probe_builds_material_graph() -> None:
    """能从 describe 为真实材质构造图并算出结构哈希。"""
    fake, name = build_fake_graph()
    describe = describe_material(fake)
    mg = build_material_graph(describe, name)
    assert mg.available
    assert mg.structure_hash.startswith("sha256:")
    assert mg.users, "应带使用对象"
