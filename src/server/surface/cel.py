"""Cel 色阶适配器：**能力驱动**地把拓扑描述转成可编辑参数。

核心原则（技术方案 §8.1）
------------------------
**探到什么就生成什么；探不到或无法唯一确定，就降级为只读，绝不猜名字。**

因此：

* ``ramp``：只有在节点上真的存在 ``color_ramp`` 时才生成可编辑色带。
  找不到 ⇒ ``supported: false, editable: false`` + ``readonly_reason: not_found``。
* Emission：用**候选插座名 + 结构验证**判定。候选命中多个、或命中的节点类型
  不是发光语义 ⇒ 判为无法唯一确定，降级只读。**不把候选名当成事实**，
  也不因为某个名字出现在某份参考脚本里就写死。
  真机确认（2026-10-11）：受管 Cel 组全部命中「``EMISSION`` 节点 + ``Strength``
  插座且未连线」，因此可写；插座被上游连线、或该组是 reference 角色时降级只读。
  写入走 ``NODE_SOCKET.default_value``，节点/插座缺失即整体失败并回滚。
* ``Sakura_Hair_Reference``：归入 ``reference`` 角色，恒为只读（决策：回退策略用）。

假 ``bpy`` 的作用仅限于验证**协议与安全逻辑**，不作为真实拓扑的事实来源 ——
真机拓扑回传后只需增删候选名，不改本模块的结构。
"""

from __future__ import annotations

from typing import Any

from .binding import Binding
from .schema import (
    COST_L1,
    EnumNode,
    GroupNode,
    RampElement,
    RampNode,
    REASON_NOT_FOUND,
    REASON_REFERENCE_ONLY,
    REASON_STRUCTURAL,
    SOURCE_SCENE,
    SOURCE_UNSUPPORTED,
    ScalarNode,
    max_cost,
)

GROUP = "cel"
COMPOSITOR_SOURCE = "AI_Compositor"

#: 承载色阶的节点语义。用**结构**判定（节点带 color_ramp），这些类型名只作参考。
RAMP_NODE_TYPES: frozenset[str] = frozenset({"VALTORGB"})

#: Emission 强度插座的**候选**名。命中之后还要过结构验证才算数。
EMISSION_SOCKET_CANDIDATES: tuple[str, ...] = (
    "Strength",
    "Emission Strength",
    "EmissionStrength",
)

#: 发光语义的节点类型。只有这些节点上的候选插座才会被接受。
EMISSION_NODE_TYPES: frozenset[str] = frozenset({"EMISSION", "BSDF_EMISSION"})

#: 插值候选（ColorRamp.interpolation 的真实枚举）
INTERPOLATION_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "LINEAR", "label": "线性"},
    {"value": "CONSTANT", "label": "常量"},
    {"value": "EASE", "label": "缓动"},
    {"value": "B_SPLINE", "label": "B 样条"},
    {"value": "CARDINAL", "label": "Cardinal"},
)

MANAGED_MODE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "editable", "label": "可编辑"},
    {"value": "reference", "label": "只读探测"},
    {"value": "fallback", "label": "回退策略"},
)


def _ramp_candidates(group: dict[str, Any]) -> list[dict[str, Any]]:
    """结构判定：节点上是否真的挂着 ``color_ramp``。"""
    out: list[dict[str, Any]] = []
    for node in group.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        ramp = node.get("color_ramp")
        if isinstance(ramp, dict):
            out.append({"node": node, "ramp": ramp})
    return out


def _emission_sockets(group: dict[str, Any]) -> list[dict[str, Any]]:
    """候选名 + 结构验证：只有发光语义节点上的候选插座才算命中。"""
    out: list[dict[str, Any]] = []
    for node in group.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or "")
        bl_idname = str(node.get("bl_idname") or "")
        is_emission = node_type in EMISSION_NODE_TYPES or "Emission" in bl_idname
        if not is_emission:
            continue
        for socket in node.get("inputs") or []:
            if not isinstance(socket, dict):
                continue
            if str(socket.get("name") or "") in EMISSION_SOCKET_CANDIDATES:
                out.append({"node_name": node.get("name"), "socket": socket})
    return out


def _cel_prefix(name: str) -> str:
    return f"cel.{name}"


def build_group_node(
    group: dict[str, Any], *, material_count: int | None = None
) -> GroupNode:
    """把单个 Cel 组转成 ``GroupNode``（含色带、发光强度、影响面、管理状态）。"""
    name = str(group.get("name") or "")
    role = str(group.get("role") or "managed")
    exists = bool(group.get("exists"))
    readonly_by_role = role == "reference"

    children: list[Any] = []

    # -- 色带 -------------------------------------------------------------
    candidates = _ramp_candidates(group)
    if len(candidates) == 1:
        node, ramp = candidates[0]["node"], candidates[0]["ramp"]
        raw_elements = [item for item in (ramp.get("elements") or []) if isinstance(item, dict)]
        elements = [
            RampElement(
                index=index,
                position=float(item.get("position") or 0.0),
                color=_as_rgba(item.get("color")),
                position_baseline=float(item.get("position") or 0.0),
                color_baseline=_as_rgba(item.get("color")),
            )
            for index, item in enumerate(raw_elements)
        ]
        interpolation = str(ramp.get("interpolation") or "LINEAR")
        children.append(
            RampNode(
                id=f"{_cel_prefix(name)}.ramp",
                kind="ramp",
                group=GROUP,
                cost=COST_L1,
                label=f"{name} 色带",
                binding=Binding(
                    "COLOR_RAMP", f"{name}/{node.get('name')}", "elements"
                ),
                elements=elements,
                interpolation=interpolation,
                interpolation_baseline=interpolation,
                interpolation_options=[dict(option) for option in INTERPOLATION_OPTIONS],
                element_count_baseline=len(elements),
                editable=not readonly_by_role,
                readonly_reason=REASON_REFERENCE_ONLY if readonly_by_role else None,
            )
        )
    else:
        # 0 个候选 ⇒ 没有色带；多个候选 ⇒ 无法唯一确定。两种都不猜，一律降级。
        children.append(
            RampNode(
                id=f"{_cel_prefix(name)}.ramp",
                kind="ramp",
                group=GROUP,
                cost=COST_L1,
                label=f"{name} 色带",
                binding=Binding("COLOR_RAMP", f"{name}/unknown", "elements"),
                supported=False,
                editable=False,
                active=False,
                readonly_reason=REASON_NOT_FOUND,
                value_source=SOURCE_UNSUPPORTED,
                reason=(
                    "该节点组中未找到色带（ColorRamp）节点。"
                    if not candidates
                    else f"该节点组中存在 {len(candidates)} 个色带节点，无法唯一确定，已降级为只读。"
                ),
            )
        )

    # -- 发光强度 ---------------------------------------------------------
    # 真机事实（2026-10-11，Blender 5.2.1 LTS，受管 Cel 组全部命中）：
    #   node name='自发光' type='EMISSION' bl_idname='ShaderNodeEmission'
    #   socket name='Strength' type='VALUE' linked=False value=0.5（各组不同）
    # 因此「候选名 + 结构验证」在真机成立 ⇒ 开放写入（v4 提交 1 承诺的 L1 参数）。
    # 两种情形仍降级只读，且**都不生成**写下去没反应的可编辑控件：
    #   1. 插座已被上游连线（default_value 不生效）；
    #   2. 该组是 reference 角色（回退策略，恒只读）。
    emission = _emission_sockets(group)
    if len(emission) == 1:
        node_name = str(emission[0].get("node_name") or "")
        socket = emission[0]["socket"]
        socket_name = str(socket.get("name") or "")
        value = socket.get("value")
        if bool(socket.get("linked")):
            children.append(
                ScalarNode(
                    id=f"{_cel_prefix(name)}.emission_strength",
                    kind="float",
                    group=GROUP,
                    cost=COST_L1,
                    label=f"{name} Emission 强度",
                    value=value,
                    baseline=value,
                    effective=value,
                    minimum=0.0,
                    maximum=100.0,
                    step=0.05,
                    supported=True,
                    editable=False,
                    readonly_reason=REASON_STRUCTURAL,
                    value_source=SOURCE_SCENE,
                    reason=(
                        "该节点组的 Emission 强度插座已被上游连线，"
                        "写入 default_value 不会影响画面，因此不可编辑。"
                    ),
                )
            )
        else:
            children.append(
                ScalarNode(
                    id=f"{_cel_prefix(name)}.emission_strength",
                    kind="float",
                    group=GROUP,
                    cost=COST_L1,
                    label=f"{name} Emission 强度",
                    value=value,
                    baseline=value,
                    effective=value,
                    minimum=0.0,
                    maximum=100.0,
                    step=0.05,
                    supported=True,
                    editable=not readonly_by_role,
                    readonly_reason=REASON_REFERENCE_ONLY if readonly_by_role else None,
                    # object_id = 组名/节点名/插座名，三段都来自**探测结果**。
                    binding=Binding(
                        "NODE_SOCKET", f"{name}/{node_name}/{socket_name}", "default_value"
                    ),
                    value_source=SOURCE_SCENE,
                    note=(
                        f"写入 {name} 组内 {node_name} 节点的 {socket_name} 插座（default_value）。"
                        f"节点或插座缺失、或插座已被连线时整份计划失败并回滚。"
                    ),
                )
            )
    elif not emission:
        children.append(
            ScalarNode(
                id=f"{_cel_prefix(name)}.emission_strength",
                kind="float",
                group=GROUP,
                cost=COST_L1,
                label=f"{name} Emission 强度",
                supported=False,
                editable=False,
                active=False,
                readonly_reason=REASON_NOT_FOUND,
                value_source=SOURCE_UNSUPPORTED,
                reason="该节点组中未找到发光节点与 Emission 强度插座。",
            )
        )
    else:
        children.append(
            ScalarNode(
                id=f"{_cel_prefix(name)}.emission_strength",
                kind="float",
                group=GROUP,
                cost=COST_L1,
                label=f"{name} Emission 强度",
                supported=False,
                editable=False,
                active=False,
                readonly_reason=REASON_NOT_FOUND,
                value_source=SOURCE_UNSUPPORTED,
                reason=(
                    f"命中 {len(emission)} 个候选 Emission 强度插座，"
                    "无法唯一确定（拒绝猜测），已降级为只读。"
                ),
            )
        )

    # -- 影响面（只读）----------------------------------------------------
    children.append(
        EnumNode(
            id=f"{_cel_prefix(name)}.impact.material_count",
            kind="int",
            group=GROUP,
            cost=COST_L1,
            label="影响材质数",
            supported=material_count is not None,
            editable=False,
            readonly_reason=REASON_STRUCTURAL,
            value_source=SOURCE_SCENE if material_count is not None else SOURCE_UNSUPPORTED,
            options=[],
            note="只读：该 Cel 组当前被多少个材质引用。",
        )
    )

    # -- 管理状态（只读）--------------------------------------------------
    mode = "reference" if readonly_by_role else ("editable" if exists else "fallback")
    children.append(
        EnumNode(
            id=f"{_cel_prefix(name)}.managed_mode",
            kind="enum",
            group=GROUP,
            cost=COST_L1,
            label="管理状态",
            value=mode,
            baseline=mode,
            effective=mode,
            options=[dict(option) for option in MANAGED_MODE_OPTIONS],
            editable=False,
            readonly_reason=REASON_REFERENCE_ONLY if readonly_by_role else REASON_STRUCTURAL,
            supported=exists,
            value_source=SOURCE_SCENE if exists else SOURCE_UNSUPPORTED,
            note="只读：该组在本工具管理范围内的角色。",
        )
    )

    return GroupNode(
        id=_cel_prefix(name),
        kind="group",
        group=GROUP,
        # 分组的层级 = 子树最高层级。**不能**因为「子树里有色带」就整组标 L3：
        # 色带的**色标取值**是 L1（只更新草稿 + 用户触发预览），只有色标**数量**
        # 属于 L3 结构性改动，而它已经由 element_count 单独声明为只读。
        # 整组标 L3 会让 max_cost() 永远返回 L3，调度器会把普通调参当成结构性操作。
        cost=max_cost(children) if children else COST_L1,
        label=name,
        supported=exists,
        editable=bool(exists and not readonly_by_role),
        active=exists,
        readonly_reason=(
            REASON_NOT_FOUND if not exists else (REASON_REFERENCE_ONLY if readonly_by_role else None)
        ),
        value_source=SOURCE_SCENE if exists else SOURCE_UNSUPPORTED,
        reason=None if exists else "未在工程中找到该节点组。",
        children=children,
    )


def _as_rgba(raw: Any) -> list[float]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        return [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        return [float(raw[0]), float(raw[1]), float(raw[2]), 1.0]
    return [0.0, 0.0, 0.0, 1.0]


def build_cel_groups(
    describe: dict[str, Any], *, material_counts: dict[str, int] | None = None
) -> list[GroupNode]:
    """把整份拓扑描述转成 Cel 分组节点列表（受管组在前，参考组在后）。"""
    counts = material_counts or {}
    groups: list[GroupNode] = []
    for group in describe.get("groups") or []:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "")
        groups.append(build_group_node(group, material_count=counts.get(name)))
    return groups
