"""Cel 适配器与通用执行器测试（v4 提交 2）。

两条主线：

**适配器 —— 能力驱动，不猜名字。**
探到 ColorRamp 才生成可编辑色带；Emission 插座要「候选名 + 结构验证」同时成立才算命中；
命中 0 个或 >1 个一律降级只读。这里刻意用一个**假 socket 名**验证它不会泄漏成生产硬编码。

**执行器 —— 整体写入、整体回滚。**
把生成的代码真的跑在假 ``bpy`` 上：色带整体替换、数量不符拒绝、色标越界拒绝、
中途失败回滚到快照，且 ``applied`` 必须为假。
"""

from __future__ import annotations

import json

import pytest

from src.server import errors, surface
from src.server.surface import cel as celmod
from src.server.surface import executor as execmod
from src.server.surface import schema as schemamod
from tests.fake_bpy import FakeBpy, run_generated_code


# -- 拓扑描述构造 ----------------------------------------------------------


def describe_one(
    name: str = "Cel_Skin",
    *,
    elements: int = 3,
    interpolation: str = "CONSTANT",
    emission_socket: str | None = "Strength",
    emission_node_type: str = "EMISSION",
    emission_node_name: str = "Emission",
    emission_linked: bool = False,
    emission_value: float = 1.5,
    extra_ramps: int = 0,
    role: str = "managed",
    exists: bool = True,
) -> dict:
    """构造一份 ``describe_groups`` 形态的拓扑描述。"""
    nodes: list[dict] = [
        {
            "name": "ColorRamp",
            "type": "VALTORGB",
            "bl_idname": "CompositorNodeValToRGB",
            "mute": False,
            "inputs": [],
            "color_ramp": {
                "element_count": elements,
                "interpolation": interpolation,
                "elements": [
                    {"position": round(index / max(1, elements - 1), 4),
                     "color": [0.1 * index, 0.2, 0.3, 1.0]}
                    for index in range(elements)
                ],
            },
        }
    ]
    for index in range(extra_ramps):
        nodes.append(
            {
                "name": f"ColorRamp{index + 2}",
                "type": "VALTORGB",
                "bl_idname": "CompositorNodeValToRGB",
                "mute": False,
                "inputs": [],
                "color_ramp": {
                    "element_count": 2,
                    "interpolation": "LINEAR",
                    "elements": [
                        {"position": 0.0, "color": [0.0, 0.0, 0.0, 1.0]},
                        {"position": 1.0, "color": [1.0, 1.0, 1.0, 1.0]},
                    ],
                },
            }
        )
    if emission_socket is not None:
        nodes.append(
            {
                "name": emission_node_name,
                "type": emission_node_type,
                "bl_idname": f"ShaderNode{emission_node_type.title()}",
                "mute": False,
                "inputs": [
                    {
                        "name": emission_socket,
                        "type": "VALUE",
                        "linked": emission_linked,
                        "value": emission_value,
                    }
                ],
                "color_ramp": None,
            }
        )
    return {
        "schema": "toon-surface-describe/1",
        "blender_version": "5.2.1",
        "compositor_group": "AI_Compositor",
        "groups": [
            {
                "name": name,
                "exists": exists,
                "role": role,
                "supported": exists,
                "editable": exists and role == "managed",
                "node_count": len(nodes),
                "ramp_count": (1 + extra_ramps) if exists else 0,
                "ramp_element_counts": [elements] if exists else [],
                "structure_signature": [],
                # 与 surface_probe.describe_groups 的输出保持一致：适配器需要节点级拓扑。
                # 组不存在时**不能**带节点，否则等于宣称「探到了」。
                "nodes": nodes if exists else [],
            }
        ],
        "found_groups": 1 if exists else 0,
        "declared_groups": 1,
        "degraded": [] if exists else [name],
        "objects": {"total": 1, "by_type": {"MESH": 1}},
        "materials": [],
        "images": [],
    }


def build_nodes(**kwargs) -> list:
    return celmod.build_cel_groups(describe_one(**kwargs))


def ramp_node(nodes: list) -> schemamod.RampNode:
    found = [
        node
        for node in surface.walk(nodes)
        if isinstance(node, schemamod.RampNode) and node.id.endswith(".ramp")
    ][0]
    return found


def find(nodes: list, param_id: str):
    return surface.flatten(nodes)[param_id]


# -- 适配器：探到就生成可编辑 ---------------------------------------------


def test_ramp_is_editable_when_detected() -> None:
    nodes = build_nodes(elements=4)
    ramp = ramp_node(nodes)
    assert ramp.supported is True
    assert ramp.editable is True
    assert ramp.element_count == 4
    assert ramp.interpolation == "CONSTANT"
    # 逐色标的位置与 RGBA 都具备（UI 每个色标一个控件靠它）
    payload = ramp.to_public()
    assert len(payload["elements"]) == 4
    assert len(payload["elements"][0]["color"]["value"]) == 4
    assert payload["elements"][0]["position"]["minimum"] == 0.0


def test_element_count_is_structural_and_not_editable() -> None:
    """决策 3 + L3 门禁：色标数量是结构信息，且当前不可编辑。"""
    payload = ramp_node(build_nodes()).to_public()["element_count"]
    assert payload["structural"] is True
    assert payload["editable"] is False
    assert payload["readonly_reason"] == schemamod.REASON_ROLLBACK_UNAVAILABLE
    assert payload["cost"] == schemamod.COST_L3


def test_binding_of_ramp_points_at_color_ramp() -> None:
    ramp = ramp_node(build_nodes())
    assert ramp.binding is not None
    assert ramp.binding.object_type == "COLOR_RAMP"
    assert ramp.binding.field == "elements"
    # node 路径由服务端从探测结果拼出，客户端无从指定
    assert ramp.binding.object_id == "Cel_Skin/ColorRamp"


# -- 适配器：探不到就降级 --------------------------------------------------


def test_missing_group_degrades_to_readonly() -> None:
    nodes = build_nodes(exists=False)
    group = nodes[0]
    assert group.supported is False
    assert group.editable is False
    assert group.readonly_reason == schemamod.REASON_NOT_FOUND
    ramp = ramp_node(nodes)
    assert ramp.supported is False
    assert ramp.editable is False
    assert "未找到" in (ramp.reason or "") or "无法唯一" in (ramp.reason or "")


def test_no_ramp_node_degrades_with_reason() -> None:
    """组存在但没有 ColorRamp：降级，且给出原因（不猜节点名）。"""
    describe = describe_one()
    describe["groups"][0]["nodes"] = []
    describe["groups"][0]["ramp_count"] = 0
    describe["groups"][0]["ramp_element_counts"] = []
    ramp = ramp_node(celmod.build_cel_groups(describe))
    assert ramp.supported is False
    assert ramp.editable is False
    assert ramp.readonly_reason == schemamod.REASON_NOT_FOUND


def test_multiple_ramps_are_ambiguous_and_degrade() -> None:
    """多个色带 ⇒ 无法唯一确定 ⇒ 降级，绝不随便挑一个。"""
    ramp = ramp_node(build_nodes(extra_ramps=1))
    assert ramp.supported is False
    assert ramp.editable is False
    assert "无法唯一确定" in (ramp.reason or "")


# -- 适配器：Emission 用候选 + 结构验证 ------------------------------------


def test_emission_socket_detected_and_now_writable() -> None:
    """真机确认后开放写入：候选名 + 结构验证命中 ⇒ 生成**可写**的 Emission 强度节点。

    真机事实（2026-10-11，Blender 5.2.1 LTS，受管 Cel 组全部命中）：
    ``EMISSION``（``bl_idname=ShaderNodeEmission``）+ ``Strength`` 插座且未连线。
    """
    nodes = build_nodes(emission_socket="Strength", emission_node_type="EMISSION")
    node = find(nodes, "cel.Cel_Skin.emission_strength")
    assert node.supported is True
    assert node.value == pytest.approx(1.5)
    assert node.editable is True
    assert node.readonly_reason is None
    assert node.binding is not None


def test_emission_binding_targets_node_socket_not_node_mute() -> None:
    """绑定必须落在 ``NODE_SOCKET.default_value``，且 object_id 由**探测结果**拼出。

    提交 2 里这里曾绑定到 ``NODE_GROUP.mute``（构造上白名单通过、语义完全错误）：
    探测到 Emission 强度后写下去会去 mute 整个节点。这条用例钉住那个回归。
    """
    node = find(build_nodes(emission_socket="Strength"), "cel.Cel_Skin.emission_strength")
    assert node.binding is not None
    assert node.binding.object_type == "NODE_SOCKET"
    assert node.binding.field == "default_value"
    assert node.binding.object_type != "NODE_GROUP"
    # 组名/节点名/插座名三段都来自探测描述，客户端无从提交
    assert node.binding.object_id == "Cel_Skin/Emission/Strength"


def test_emission_object_id_follows_real_node_name() -> None:
    """真机节点名是「自发光」时 object_id 必须随之变化 —— 不写死任何语言的名字。"""
    node = find(
        build_nodes(emission_node_name="自发光", emission_socket="Strength"),
        "cel.Cel_Skin.emission_strength",
    )
    assert node.binding is not None
    assert node.binding.object_id == "Cel_Skin/自发光/Strength"


def test_emission_draft_compiles_to_plan() -> None:
    nodes = build_nodes(emission_socket="Strength")
    ops = surface.validate_draft({"cel.Cel_Skin.emission_strength": 2.0}, nodes)
    emission_ops = [op for op in ops if op.param_id == "cel.Cel_Skin.emission_strength"]
    assert len(emission_ops) == 1
    assert emission_ops[0].binding.object_type == "NODE_SOCKET"
    assert emission_ops[0].cost == schemamod.COST_L1
    assert emission_ops[0].value == pytest.approx(2.0)


def test_emission_draft_rejects_out_of_range() -> None:
    """值域在服务端收口：越界值在生成代码**之前**就被拒。"""
    nodes = build_nodes(emission_socket="Strength")
    for bad in (100.5, -0.5):
        with pytest.raises(errors.ToonTunerError) as excinfo:
            surface.validate_draft({"cel.Cel_Skin.emission_strength": bad}, nodes)
        assert excinfo.value.code == errors.PARAM_INVALID


def test_emission_linked_socket_is_readonly() -> None:
    """插座被上游连线 ⇒ 写 default_value 不生效 ⇒ 明确降级只读，不假装可写。"""
    nodes = build_nodes(emission_socket="Strength", emission_linked=True)
    node = find(nodes, "cel.Cel_Skin.emission_strength")
    assert node.supported is True
    assert node.editable is False
    assert node.readonly_reason == schemamod.REASON_STRUCTURAL
    assert node.binding is None
    with pytest.raises(errors.ToonTunerError) as excinfo:
        surface.validate_draft({"cel.Cel_Skin.emission_strength": 2.0}, nodes)
    assert excinfo.value.code == errors.NOT_EDITABLE


def test_emission_on_reference_group_stays_readonly() -> None:
    """参考组（回退策略）恒只读 —— 即便同样探到了 Emission 强度。"""
    nodes = build_nodes(role="reference", emission_socket="Strength")
    node = find(nodes, "cel.Cel_Skin.emission_strength")
    assert node.supported is True
    assert node.editable is False
    assert node.readonly_reason == schemamod.REASON_REFERENCE_ONLY


def test_emission_accepts_alternative_candidate_name() -> None:
    nodes = build_nodes(emission_socket="Emission Strength")
    assert find(nodes, "cel.Cel_Skin.emission_strength").supported is True


def test_fake_socket_name_does_not_become_editable() -> None:
    """**假 socket 名不得泄漏为生产硬编码**：不在候选表里的名字一律判为探不到。"""
    nodes = build_nodes(emission_socket="TotallyMadeUpSocketName")
    node = find(nodes, "cel.Cel_Skin.emission_strength")
    assert node.supported is False, "随便一个名字不该被当成 Emission 强度"
    assert node.editable is False


def test_emission_socket_on_wrong_node_type_is_rejected() -> None:
    """候选名对、但节点语义不对 ⇒ 结构验证不过 ⇒ 降级。**

    这一条是「候选名 + 结构验证」两个条件缺一不可的证据。
    """
    nodes = build_nodes(emission_socket="Strength", emission_node_type="MIX_RGB")
    node = find(nodes, "cel.Cel_Skin.emission_strength")
    assert node.supported is False
    assert node.editable is False


def test_multiple_emission_sockets_are_ambiguous() -> None:
    describe = describe_one()
    describe["groups"][0]["nodes"].append(
        {
            "name": "Emission2",
            "type": "EMISSION",
            "bl_idname": "ShaderNodeEmission",
            "mute": False,
            "inputs": [{"name": "Strength", "type": "VALUE", "linked": False, "value": 2.0}],
            "color_ramp": None,
        }
    )
    node = find(celmod.build_cel_groups(describe), "cel.Cel_Skin.emission_strength")
    assert node.supported is False
    assert "无法唯一确定" in (node.reason or "")


# -- 适配器：参考组恒只读 --------------------------------------------------


def test_reference_group_is_detected_but_never_editable() -> None:
    nodes = build_nodes(role="reference")
    group = nodes[0]
    assert group.supported is True
    assert group.editable is False
    assert group.readonly_reason == schemamod.REASON_REFERENCE_ONLY
    assert ramp_node(nodes).editable is False


def test_readonly_display_nodes_are_not_writable() -> None:
    nodes = build_nodes()
    count = find(nodes, "cel.Cel_Skin.impact.material_count")
    mode = find(nodes, "cel.Cel_Skin.managed_mode")
    for node in (count, mode):
        assert node.binding is None, "只读展示项不得带可写 binding"
        assert node.editable is False
    assert mode.value in {"editable", "reference", "fallback"}


# -- 草稿校验 --------------------------------------------------------------


def _elements(count: int = 3) -> list[dict]:
    return [
        {"position": round(index / max(1, count - 1), 4), "color": [0.1, 0.2, 0.3, 1.0]}
        for index in range(count)
    ]


def test_draft_accepts_valid_ramp_and_writes_whole_ramp() -> None:
    nodes = build_nodes(elements=3)
    draft = {"cel.Cel_Skin.ramp": _elements(3)}
    ops = surface.validate_draft(draft, nodes)
    ramp_ops = [op for op in ops if op.param_id == "cel.Cel_Skin.ramp"]
    assert len(ramp_ops) == 1
    assert ramp_ops[0].binding.field == "elements"
    assert ramp_ops[0].cost == schemamod.COST_L1
    assert ops[0].cost in schemamod.COSTS


def test_draft_rejects_unknown_id() -> None:
    nodes = build_nodes()
    with pytest.raises(errors.ToonTunerError) as excinfo:
        surface.validate_draft({"cel.Nope.ramp": _elements()}, nodes)
    assert excinfo.value.code == errors.PARAM_INVALID


def test_draft_rejects_element_count_change_as_structural() -> None:
    """决策 3：数量与基线不符 ⇒ STRUCTURE_CHANGED（而不是悄悄写坏）。"""
    nodes = build_nodes(elements=3)
    with pytest.raises(errors.ToonTunerError) as excinfo:
        surface.validate_draft({"cel.Cel_Skin.ramp": _elements(4)}, nodes)
    assert excinfo.value.code == errors.STRUCTURE_CHANGED


@pytest.mark.parametrize(
    "bad",
    [
        [{"position": -0.1, "color": [0, 0, 0, 1]}, {"position": 1.0, "color": [0, 0, 0, 1]}],
        [{"position": 1.2, "color": [0, 0, 0, 1]}, {"position": 1.0, "color": [0, 0, 0, 1]}],
        [{"position": 0.0, "color": [0, 0, 0]}, {"position": 1.0, "color": [0, 0, 0, 1]}],
        [{"position": "x", "color": [0, 0, 0, 1]}, {"position": 1.0, "color": [0, 0, 0, 1]}],
        [{"position": 0.0, "color": [0, 0, 0, 1]}],
    ],
)
def test_draft_rejects_bad_ramp_elements(bad: list) -> None:
    nodes = build_nodes(elements=2)
    with pytest.raises(errors.ToonTunerError) as excinfo:
        surface.validate_draft({"cel.Cel_Skin.ramp": bad}, nodes)
    assert excinfo.value.code in (errors.PARAM_INVALID, errors.STRUCTURE_CHANGED)


def test_draft_rejects_write_to_unsupported_param() -> None:
    nodes = build_nodes(exists=False)
    with pytest.raises(errors.ToonTunerError) as excinfo:
        surface.validate_draft({"cel.Cel_Skin.ramp": _elements(3)}, nodes)
    assert excinfo.value.code == errors.UNSUPPORTED_PARAM


def test_draft_rejects_write_to_readonly_display_node() -> None:
    nodes = build_nodes()
    with pytest.raises(errors.ToonTunerError) as excinfo:
        surface.validate_draft({"cel.Cel_Skin.managed_mode": "editable"}, nodes)
    assert excinfo.value.code == errors.NOT_EDITABLE


# -- 执行器：整体写入 ------------------------------------------------------


def fake_with_cel(
    elements: int = 3, *, name: str = "Cel_Skin", interpolation: str = "LINEAR"
) -> FakeBpy:
    fake = FakeBpy()
    fake.add_cel_group(name, element_count=elements, interpolation=interpolation)
    return fake


def run_apply(ops: list, fake: FakeBpy) -> dict:
    stdout = run_generated_code(execmod.build_apply_code(ops), fake)
    return execmod.extract_json(stdout)


def test_executor_writes_all_ramp_elements_atomically() -> None:
    fake = fake_with_cel(3)
    nodes = build_nodes(elements=3)
    draft = {
        "cel.Cel_Skin.ramp": [
            {"position": 0.0, "color": [0.0, 0.0, 0.0, 1.0]},
            {"position": 0.4, "color": [0.5, 0.5, 0.5, 1.0]},
            {"position": 1.0, "color": [1.0, 1.0, 1.0, 1.0]},
        ]
    }
    ops = surface.validate_draft(draft, nodes)
    payload = run_apply(ops, fake)

    assert payload["applied"] is True
    assert payload["failure"] is None
    ramp = fake.node_groups["Cel_Skin"].nodes.get("ColorRamp").color_ramp
    assert [element.position for element in ramp.elements] == [0.0, 0.4, 1.0]
    assert list(ramp.elements[1].color) == [0.5, 0.5, 0.5, 1.0]


def test_executor_does_not_change_element_count() -> None:
    """执行器不做结构改写：数量必须保持不变。"""
    fake = fake_with_cel(4)
    nodes = build_nodes(elements=4)
    ops = surface.validate_draft({"cel.Cel_Skin.ramp": _elements(4)}, nodes)
    run_apply(ops, fake)
    assert len(fake.node_groups["Cel_Skin"].nodes.get("ColorRamp").color_ramp.elements) == 4


def test_executor_writes_interpolation_as_separate_op() -> None:
    fake = fake_with_cel(3)
    nodes = build_nodes(elements=3)
    interp = surface.flatten(nodes)["cel.Cel_Skin.ramp"].binding
    op = execmod.PlanOp(
        surface.Binding(interp.object_type, interp.object_id, "interpolation"),
        "EASE",
        cost=schemamod.COST_L1,
        param_id="cel.Cel_Skin.ramp.interpolation",
    )
    payload = run_apply([op], fake)
    assert payload["applied"] is True
    assert fake.node_groups["Cel_Skin"].nodes.get("ColorRamp").color_ramp.interpolation == "EASE"


# -- 执行器：Emission 强度（NODE_SOCKET.default_value）----------------------

EMISSION_KEY = "NODE_SOCKET:Cel_Skin/Emission/Strength.default_value"


def test_executor_writes_emission_strength() -> None:
    """端到端：计划 → 假 bpy → 插座值变化，且快照记下写入前的值（恢复的原料）。"""
    fake = FakeBpy()
    fake.add_cel_group("Cel_Skin", element_count=3, emission_strength=0.5)
    nodes = build_nodes(emission_socket="Strength", emission_value=0.5)

    ops = surface.validate_draft({"cel.Cel_Skin.emission_strength": 1.25}, nodes)
    payload = run_apply(ops, fake)

    assert payload["applied"] is True
    assert payload["failure"] is None
    socket = fake.node_groups["Cel_Skin"].nodes.get("Emission").inputs.get("Strength")
    assert socket.default_value == pytest.approx(1.25)
    assert payload["snapshot"][EMISSION_KEY] == pytest.approx(0.5)
    assert payload["values"][EMISSION_KEY] == pytest.approx(1.25)


def test_executor_writes_emission_on_locally_named_node() -> None:
    """真机节点名是中文（「自发光」）时同样可写 —— 定位靠探测结果拼接的 object_id。"""
    fake = FakeBpy()
    fake.add_cel_group("Cel_Skin", element_count=3, emission_strength=0.5, emission_node_name="自发光")
    nodes = build_nodes(emission_node_name="自发光", emission_socket="Strength", emission_value=0.5)

    ops = surface.validate_draft({"cel.Cel_Skin.emission_strength": 0.9}, nodes)
    payload = run_apply(ops, fake)

    assert payload["applied"] is True
    socket = fake.node_groups["Cel_Skin"].nodes.get("自发光").inputs.get("Strength")
    assert socket.default_value == pytest.approx(0.9)


def test_executor_emission_socket_missing_fails_loudly() -> None:
    """插座在写入时不存在（拓扑变了）⇒ 结构化失败，**不静默跳过**。"""
    fake = FakeBpy()
    fake.add_cel_group("Cel_Skin", element_count=3, emission_strength=0.5)
    op = execmod.PlanOp(
        surface.Binding("NODE_SOCKET", "Cel_Skin/Emission/NotThere", "default_value"),
        2.0,
        cost=schemamod.COST_L1,
        param_id="cel.Cel_Skin.emission_strength",
    )
    payload = run_apply([op], fake)
    assert payload["applied"] is False
    assert payload["failure"]["kind"] == "write_failed"
    assert "node socket not found" in payload["failure"]["message"]


def test_executor_emission_node_missing_fails_loudly() -> None:
    fake = FakeBpy()
    fake.add_cel_group("Cel_Skin", element_count=3, emission_strength=0.5)
    op = execmod.PlanOp(
        surface.Binding("NODE_SOCKET", "Cel_Skin/GhostNode/Strength", "default_value"),
        2.0,
        cost=schemamod.COST_L1,
        param_id="cel.Cel_Skin.emission_strength",
    )
    payload = run_apply([op], fake)
    assert payload["applied"] is False
    assert "node not found" in payload["failure"]["message"]


def test_executor_emission_linked_socket_write_is_rejected() -> None:
    """插座被连线时写入也失败（Blender 侧第二道闸门），不写入一个不生效的值。"""
    fake = FakeBpy()
    fake.add_cel_group("Cel_Skin", element_count=3, emission_strength=0.5, emission_linked=True)
    op = execmod.PlanOp(
        surface.Binding("NODE_SOCKET", "Cel_Skin/Emission/Strength", "default_value"),
        2.0,
        cost=schemamod.COST_L1,
        param_id="cel.Cel_Skin.emission_strength",
    )
    payload = run_apply([op], fake)
    assert payload["applied"] is False
    assert "linked" in payload["failure"]["message"]


def test_executor_emission_failure_restores_earlier_writes() -> None:
    """同一份计划里前一条已写入的内容，必须在后一条失败时被还原。"""
    fake = FakeBpy()
    fake.add_cel_group("Cel_Skin", element_count=3, emission_strength=0.5)
    view = fake.view_settings
    view.exposure = 0.25

    ops = [
        execmod.PlanOp(
            surface.Binding("VIEW_SETTINGS", "$scene", "exposure"),
            1.75,
            cost=schemamod.COST_L0,
            param_id="color.exposure",
        ),
        execmod.PlanOp(
            surface.Binding("NODE_SOCKET", "Cel_Skin/Emission/Ghost", "default_value"),
            2.0,
            cost=schemamod.COST_L1,
            param_id="cel.Cel_Skin.emission_strength",
        ),
    ]
    payload = run_apply(ops, fake)
    assert payload["applied"] is False
    assert view.exposure == pytest.approx(0.25), "失败的整份计划必须回滚已写入的部分"


def test_executor_emission_readback_matches_plan_key() -> None:
    """回读键与计划键一致 —— 校验 `verify_ops` 才能逐项比对。"""
    fake = FakeBpy()
    fake.add_cel_group("Cel_Skin", element_count=3, emission_strength=0.5)
    op = execmod.PlanOp(
        surface.Binding("NODE_SOCKET", "Cel_Skin/Emission/Strength", "default_value"),
        0.7,
        cost=schemamod.COST_L1,
        param_id="cel.Cel_Skin.emission_strength",
    )
    stdout = run_generated_code(execmod.build_readback_code([op]), fake)
    payload = execmod.extract_json(stdout)
    assert payload["values"][EMISSION_KEY] == pytest.approx(0.5)
    assert surface.plan_values([op]) == {EMISSION_KEY: 0.7}


# -- 执行器：失败即回滚 ----------------------------------------------------


def test_executor_rejects_out_of_range_ramp_at_server_gate() -> None:
    """第一道闸门在服务端：越界色标在生成代码**之前**就被拒绝。"""
    fake = fake_with_cel(3)
    binding = surface.Binding("COLOR_RAMP", "Cel_Skin/ColorRamp", "elements")
    with pytest.raises(errors.ToonTunerError) as excinfo:
        execmod.build_apply_code(
            [
                execmod.PlanOp(
                    binding,
                    [
                        {"position": 0.0, "color": [0.0, 0.0, 0.0, 1.0]},
                        {"position": 9.0, "color": [1.0, 1.0, 1.0, 1.0]},
                        {"position": 1.0, "color": [1.0, 1.0, 1.0, 1.0]},
                    ],
                    cost=schemamod.COST_L1,
                    param_id="cel.Cel_Skin.ramp",
                )
            ]
        )
    assert excinfo.value.code == errors.PARAM_INVALID
    # 服务端拒绝时不该产生任何 Blender 调用
    assert fake.render_count == 0


def test_executor_rolls_back_when_element_count_mismatches() -> None:
    """第二道闸门在 Blender 侧：数量与现状不符 ⇒ 整份不写并回滚。

    这一条走的是「服务端看起来合法（3 个色标、位置都在 0–1）」但工程里实际是 4 个
    的情形 —— 正是需要 Blender 侧守住的场景。
    """
    fake = fake_with_cel(4)  # 工程里是 4 个
    ramp = fake.node_groups["Cel_Skin"].nodes.get("ColorRamp").color_ramp
    before = [(element.position, list(element.color)) for element in ramp.elements]

    binding = surface.Binding("COLOR_RAMP", "Cel_Skin/ColorRamp", "elements")
    payload = run_apply(
        [
            execmod.PlanOp(
                binding,
                [
                    {"position": 0.0, "color": [0.0, 0.0, 0.0, 1.0]},
                    {"position": 0.5, "color": [0.5, 0.5, 0.5, 1.0]},
                    {"position": 1.0, "color": [1.0, 1.0, 1.0, 1.0]},
                ],
                cost=schemamod.COST_L1,
                param_id="cel.Cel_Skin.ramp",
            )
        ],
        fake,
    )

    assert payload["applied"] is False
    assert payload["failure"]["kind"] == "write_failed"
    assert "element_count mismatch" in payload["failure"]["message"]
    after = [(element.position, list(element.color)) for element in ramp.elements]
    assert after == before, "失败后必须回到快照"


def test_executor_rolls_back_earlier_op_when_later_op_fails() -> None:
    """前一条成功、后一条失败 ⇒ 前一条也必须回滚。"""
    fake = fake_with_cel(4)
    view = fake.view_settings
    before_exposure = view.exposure

    good = execmod.PlanOp(
        surface.Binding("VIEW_SETTINGS", "$scene", "exposure"),
        2.5,
        cost=schemamod.COST_L0,
        param_id="color.exposure",
    )
    bad = execmod.PlanOp(
        surface.Binding("COLOR_RAMP", "Cel_Skin/ColorRamp", "elements"),
        [
            {"position": 0.0, "color": [0.0, 0.0, 0.0, 1.0]},
            {"position": 0.5, "color": [0.5, 0.5, 0.5, 1.0]},
            {"position": 1.0, "color": [1.0, 1.0, 1.0, 1.0]},
        ],
        cost=schemamod.COST_L1,
        param_id="cel.Cel_Skin.ramp",
    )
    payload = run_apply([good, bad], fake)

    assert payload["applied"] is False
    assert view.exposure == pytest.approx(before_exposure), "前一条成功写入也必须被回滚"


def test_executor_clamps_both_ends_range() -> None:
    fake = fake_with_cel(2)
    view = fake.view_settings
    view.exposure = 0.75

    op = execmod.PlanOp(
        surface.Binding("VIEW_SETTINGS", "$scene", "exposure"),
        1.25,
        cost=schemamod.COST_L0,
        param_id="color.exposure",
    )
    payload = run_apply([op], fake)
    assert payload["applied"] is True
    assert view.exposure == pytest.approx(1.25)
    # 快照键由计划推导（不再硬编码四个字段）
    assert "VIEW_SETTINGS:$scene.exposure" in payload["snapshot"]


def test_executor_readback_code_writes_nothing() -> None:
    fake = fake_with_cel(3)
    view = fake.view_settings
    view.exposure = 0.3
    op = execmod.PlanOp(
        surface.Binding("VIEW_SETTINGS", "$scene", "exposure"),
        9.0,
        cost=schemamod.COST_L0,
        param_id="color.exposure",
    )
    stdout = run_generated_code(execmod.build_readback_code([op]), fake)
    payload = execmod.extract_json(stdout)
    assert payload["values"]["VIEW_SETTINGS:$scene.exposure"] == pytest.approx(0.3)
    assert view.exposure == pytest.approx(0.3), "回读不得写入"


def test_executor_rejects_readonly_binding_in_plan() -> None:
    with pytest.raises(errors.ToonTunerError) as excinfo:
        execmod.PlanOp(
            surface.Binding("COLOR_RAMP", "Cel_Skin/ColorRamp", "element_count"),
            4,
            cost=schemamod.COST_L3,
            param_id="cel.Cel_Skin.ramp.element_count",
        )
    assert excinfo.value.code == errors.NOT_EDITABLE


def test_executor_sorts_dependency_before_dependent() -> None:
    """依赖边排序：view_transform 必须先于 look（旧实现靠硬编码阶段表）。"""
    transform = execmod.PlanOp(
        surface.Binding("VIEW_SETTINGS", "$scene", "view_transform"),
        "Standard",
        cost=schemamod.COST_L0,
        param_id="color.view_transform",
    )
    look = execmod.PlanOp(
        surface.Binding("VIEW_SETTINGS", "$scene", "look"),
        "None",
        cost=schemamod.COST_L0,
        param_id="color.look",
        depends_on=["color.view_transform"],
    )
    ordered = execmod.sort_ops([look, transform])
    assert [op.param_id for op in ordered] == ["color.view_transform", "color.look"]


def test_executor_orders_by_cost_layer() -> None:
    low = execmod.PlanOp(
        surface.Binding("VIEW_SETTINGS", "$scene", "exposure"),
        0.5, cost=schemamod.COST_L0, param_id="a")
    high = execmod.PlanOp(
        surface.Binding("RENDER", "$scene", "resolution_percentage"),
        100, cost=schemamod.COST_L2, param_id="b")
    ordered = execmod.sort_ops([high, low])
    assert [op.param_id for op in ordered] == ["a", "b"]


def test_max_cost_reports_highest_layer() -> None:
    nodes = build_nodes()
    assert surface.max_cost(nodes) in schemamod.COSTS


def test_group_cost_follows_children_not_always_l3() -> None:
    """分组层级 = 子树最高层级。

    整组恒标 L3 会让 ``max_cost()`` 永远返回 L3，调度器就会把「调一个色标」当成
    结构性操作。色标**数量**才是结构信息，它已由 ``element_count`` 单独声明为只读。
    """
    nodes = build_nodes()
    assert nodes[0].cost == schemamod.COST_L1
    assert surface.max_cost(nodes) == schemamod.COST_L1


# -- 草稿：降级节点与复合色带 ----------------------------------------------


def test_degraded_nodes_stay_out_of_baseline_and_full_draft() -> None:
    """降级节点不进基线值，也不会因为「不在草稿里」而被当成错误。

    完整草稿 = 基线 + 草稿。若降级节点的空基线被塞进完整草稿，每次预览都会
    在服务端自己抛 ``UNSUPPORTED_PARAM`` —— 一个探不到的组会让整台工具不可用。
    """
    nodes = build_nodes(exists=False)
    assert surface.baseline_values(nodes) == {}
    assert surface.validate_draft({}, nodes) == []


def test_ramp_draft_compiles_interpolation_as_second_op() -> None:
    nodes = build_nodes(elements=3)
    draft = {"cel.Cel_Skin.ramp": {"elements": _elements(3), "interpolation": "EASE"}}
    ops = surface.validate_draft(draft, nodes)
    by_param = {op.param_id: op for op in ops}
    assert set(by_param) == {"cel.Cel_Skin.ramp", "cel.Cel_Skin.ramp.interpolation"}
    interp = by_param["cel.Cel_Skin.ramp.interpolation"]
    assert interp.value == "EASE"
    assert interp.binding.field == "interpolation"
    # 两条操作指向同一个色带 datablock（整体写入、整体回滚）
    assert interp.binding.object_id == by_param["cel.Cel_Skin.ramp"].binding.object_id


def test_ramp_draft_without_interpolation_keeps_baseline() -> None:
    nodes = build_nodes(elements=3, interpolation="CONSTANT")
    ops = surface.validate_draft({"cel.Cel_Skin.ramp": _elements(3)}, nodes)
    interp = [op for op in ops if op.param_id.endswith(".interpolation")][0]
    assert interp.value == "CONSTANT"


def test_ramp_draft_rejects_unknown_interpolation() -> None:
    nodes = build_nodes(elements=3)
    with pytest.raises(errors.ToonTunerError) as excinfo:
        surface.validate_draft(
            {"cel.Cel_Skin.ramp": {"elements": _elements(3), "interpolation": "NOT_A_MODE"}},
            nodes,
        )
    assert excinfo.value.code == errors.PARAM_INVALID


def test_executor_writes_ramp_and_interpolation_in_one_plan() -> None:
    fake = fake_with_cel(3, interpolation="LINEAR")
    nodes = build_nodes(elements=3, interpolation="CONSTANT")
    ops = surface.validate_draft(
        {"cel.Cel_Skin.ramp": {"elements": _elements(3), "interpolation": "CONSTANT"}}, nodes
    )
    payload = run_apply(ops, fake)
    assert payload["applied"] is True, payload.get("failure")
    ramp = fake.node_groups["Cel_Skin"].nodes.get("ColorRamp").color_ramp
    assert ramp.interpolation == "CONSTANT"
    ok, mismatches = execmod.verify_ops(ops, payload["values"])
    assert ok, mismatches


def test_verify_ops_detects_readback_drift() -> None:
    fake = fake_with_cel(3)
    nodes = build_nodes(elements=3)
    ops = surface.validate_draft({"cel.Cel_Skin.ramp": _elements(3)}, nodes)
    payload = run_apply(ops, fake)
    values = dict(payload["values"])

    key = ops[0].binding.key()
    drifted = [dict(item) for item in values[key]]
    drifted[0] = {"position": 0.99, "color": [0.0, 0.0, 0.0, 1.0]}
    values[key] = drifted

    ok, mismatches = execmod.verify_ops([ops[0]], values)
    assert ok is False
    assert mismatches and mismatches[0]["id"] == "cel.Cel_Skin.ramp"


def test_extract_json_rejects_garbage() -> None:
    with pytest.raises(errors.BlenderUnexpectedResponse):
        execmod.extract_json("no marker")
