"""递归参数 schema 生成（规格 §2「语义与置信度 / Schema 分类」）。

把一次反向追踪的结果转成 ``surface.schema`` 的节点树，**复用既有节点模型**：
前端因此不需要为「材质图参数」写第二套渲染逻辑；第三提交的写入路径也能直接
复用 ``surface.draft.validate_draft``。

对外参数形状（规格 §2）
------------------------
每个参数（扁平化后）都带：

.. code-block:: json

    {
      "id": "mg-opaque-id",
      "label": "基础颜色",
      "kind": "color",
      "confidence": "confirmed",
      "semantic": "base_color",
      "evidence": [],
      "influence_paths": [],
      "affected_materials": [],
      "shared": false,
      "editable": true
    }

节点身份通过 ``identity.ParamIdentity`` 生成 opaque ``param_id``（``mg-<hash24>``），
前端只拿到这个 id 与当前值；**参数不含节点路径**。

本模块**只生成 schema**：不执行任何写入（写入在后续提交接入）。
"""

from __future__ import annotations

from typing import Any

from ..surface.binding import Binding
from ..surface.schema import (
    COST_L1,
    ColorNode,
    EnumNode,
    GroupNode,
    Node,
    RampElement,
    RampNode,
    REASON_STRUCTURAL,
    SOURCE_SCENE,
    SOURCE_UNSUPPORTED,
    ScalarNode,
    max_cost,
    walk,
)
from .graph import Graph
from .identity import ParamIdentity, identity_for, node_identity, path_hash
from .probe import MaterialGraph
from .semantics import (
    CATEGORIES,
    LEVEL_EXPERIMENTAL,
    LEVEL_INFERRED,
    LEVEL_UNSUPPORTED,
    REASON_INFERRED,
    REASON_LINKED,
    REASON_SHARED_GROUP,
    SEM_GROUP,
    SEM_RAMP,
    SEMANTIC_CATEGORY,
    SEMANTIC_LABELS,
    editable_sockets,
    linked_inputs,
)
from .trace import (
    GROUP_SEP,
    TraceResult,
    TracedNode,
    detect_ambiguous,
    group_users,
    trace_from_output,
)

GROUP = "material_graph"
SCHEMA_VERSION = "toon-material-graph/1"

#: 语义 -> 展示分组序号（前端据此排序，固定不变）。
_SEMANTIC_COST = {semantic: COST_L1 for semantic in SEMANTIC_LABELS}


def _binding(material_name: str, node_name: str, socket: str) -> Binding:
    """材质图节点参数走 ``MATERIAL_GRAPH_SOCKET.default_value``。

    ``object_id`` = ``材质名/节点名/插座名``，三段**全部由服务端探测结果拼出**；
    客户端永远不能提交节点路径或插座名。与 Cel 的 ``NODE_SOCKET`` 分开：
    后者定位的是节点组内节点，本工具不做「按名猜组」的迁移。
    """
    return Binding(
        "MATERIAL_GRAPH_SOCKET", f"{material_name}/{node_name}/{socket}", "default_value"
    )


def _display_name(item: TracedNode) -> str:
    return item.node.label or item.name.split(GROUP_SEP, 1)[-1]


def _fallback_identity(
    graph: Graph, material: MaterialGraph, node_name: str, socket: str, field: str
) -> ParamIdentity:
    """``identity_for`` 找不到节点时的兜底身份（节点确实不在图里）。

    仍然走同一套 ``ParamIdentity``，因此 id 依旧是 opaque 且稳定 ——
    宁可给一个基于材质 + 字段的稳定占位 id，也不退回「用节点名当主键」。
    """
    return ParamIdentity(
        material_id=material.material_id,
        node_tree_id=f"NODE_TREE:{material.name}",
        node_id=node_identity(graph.get(node_name) or _MissingNode(node_name)),
        socket=socket,
        field=field,
        path_hash=path_hash(graph, node_name),
    )


class _MissingNode:
    """图里不存在的节点占位：只提供 ``node_identity`` 需要的两个属性。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.type = ""
        self.bl_idname = ""

    inputs: dict[str, Any] = {}
    outputs: tuple[str, ...] = ()
    uuid: str | None = None


def _param_node_id(identity) -> str:
    """参数在 schema 里的 id = **opaque param_id**（规格 §5）。

    刻意**不用节点名**：节点名会跨材质重复（两个材质都可能有 ``Value`` 节点），
    用它做主键会导致 id 冲突；更重要的是规格要求「前端只拿到 opaque id」，
    真实节点名 / 插座名只出现在 ``label`` 与 ``impact``（展示用）里。
    """
    return identity.param_id()


def _shared(item: TracedNode, material: MaterialGraph) -> bool:
    """该节点是否属于**共享**节点组（同一组被多个材质引用）。"""
    group = item.in_group or item.node.group_name
    if not group:
        return False
    users = material.shared_groups.get(group)
    if users is None:
        return False
    return len(users) > 1


def _affected_materials(item: TracedNode, material: MaterialGraph) -> list[str]:
    """受影响材质：组内节点 → 该组的全部使用材质；否则 → 本材质的使用对象。"""
    group = item.in_group or item.node.group_name
    if group and group in material.shared_groups:
        return list(material.shared_groups[group])
    return list(material.users) or [material.name]


def _impact(item: TracedNode, material: MaterialGraph) -> dict[str, Any]:
    """影响路径 + 影响材质（规格 §2：每项都要带）。"""
    paths = item.influence_paths or [[item.name]]
    return {
        "session_id": item.identity,
        "path_hash": item.path_hash,
        "material_id": material.material_id,
        "node_tree_id": f"NODE_TREE:{material.name}",
        "source_node": item.name,
        "source_label": _display_name(item),
        "in_group": item.in_group,
        "depth": item.depth,
        "upstream": list(item.upstream),
        "downstream": list(item.downstream),
        "used_outputs": list(item.used_outputs),
        "influence_paths": [list(p) for p in paths],
        "affected_materials": _affected_materials(item, material),
        "shared": _shared(item, material),
        "evidence": list(item.recognition.evidence),
    }


def _degrade_reason(item: TracedNode, graph: Graph, material: MaterialGraph) -> str | None:
    """该节点当前**不可写**的原因；可写返回 None。

    判定顺序：歧义路径 → 置信等级限制。

    刻意**不在这里**拦截共享节点组：规格要求「共享参数修改前必须明确确认影响范围」，
    而确认发生在**提交/执行**时（``SHARED_PARAMETER_CONFIRM_REQUIRED``）。若在 schema
    阶段就一律只读，执行层的确认闸门将永远不可达，这条安全机制就成了死代码。
    共享信息仍然如实带在 ``shared`` / ``affected_materials`` 上供界面提示。
    """
    if detect_ambiguous(graph, item.name):
        return "ambiguous_path"
    recognition = item.recognition
    if recognition.level == LEVEL_UNSUPPORTED:
        return recognition.readonly_reason or "unsupported"
    if recognition.level == LEVEL_INFERRED:
        return REASON_INFERRED
    if recognition.level == LEVEL_EXPERIMENTAL:
        return recognition.readonly_reason or "experimental_semantics"
    return None


def _base_extra(item: TracedNode, material: MaterialGraph, kind: str) -> dict[str, Any]:
    """spec 形状的公共字段（写进 ``impact``，由 ``to_spec`` 平铺到顶层）。"""
    impact = _impact(item, material)
    return {
        **impact,
        "kind": kind,
        "confidence": item.recognition.level,
        "semantic": item.recognition.semantic,
        "evidence": list(item.recognition.evidence),
    }


def _scalar(
    *,
    node_id: str,
    label: str,
    value: Any,
    impact: dict[str, Any],
    binding: Binding | None,
    level: str,
    semantic: str,
    editable: bool,
    readonly_reason: str | None,
    minimum: float | None = None,
    maximum: float | None = None,
    step: float | None = None,
) -> ScalarNode:
    return ScalarNode(
        id=node_id,
        kind="float",
        group=GROUP,
        cost=_SEMANTIC_COST.get(semantic, COST_L1),
        label=label,
        binding=binding,
        value=value,
        baseline=value,
        effective=value,
        minimum=minimum,
        maximum=maximum,
        step=step,
        supported=True,
        editable=editable,
        active=True,
        readonly_reason=readonly_reason,
        value_source=SOURCE_SCENE,
        impact={**impact, "level": level, "semantic": semantic},
        note=f"语义：{SEMANTIC_LABELS.get(semantic, semantic)}；置信等级：{level}。",
    )


def _color(
    *,
    node_id: str,
    label: str,
    value: list[float],
    impact: dict[str, Any],
    binding: Binding | None,
    level: str,
    semantic: str,
    editable: bool,
    readonly_reason: str | None,
) -> ColorNode:
    return ColorNode(
        id=node_id,
        kind="color",
        group=GROUP,
        cost=_SEMANTIC_COST.get(semantic, COST_L1),
        label=label,
        binding=binding,
        value=value,
        baseline=value,
        effective=value,
        supported=True,
        editable=editable,
        active=True,
        readonly_reason=readonly_reason,
        value_source=SOURCE_SCENE,
        impact={**impact, "level": level, "semantic": semantic},
        note=f"语义：{SEMANTIC_LABELS.get(semantic, semantic)}；置信等级：{level}。",
    )


def _placeholder(
    *,
    node_id: str,
    label: str,
    kind: str,
    impact: dict[str, Any],
    level: str,
    semantic: str,
    reason: str,
) -> Node:
    """无法写入时的只读占位（绝不生成写下去没反应的控件）。"""
    common: dict[str, Any] = {
        "id": node_id,
        "kind": kind,
        "group": GROUP,
        "cost": _SEMANTIC_COST.get(semantic, COST_L1),
        "label": label,
        "supported": False,
        "editable": False,
        "active": False,
        "readonly_reason": reason,
        "value_source": SOURCE_UNSUPPORTED,
        "impact": {**impact, "level": level, "semantic": semantic},
        "reason": f"该项无法安全写入（{reason}），仅作展示。",
    }
    if kind == "color":
        return ColorNode(**common)
    return ScalarNode(**common)


def _as_rgba(raw: Any) -> list[float]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        return [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        return [float(raw[0]), float(raw[1]), float(raw[2]), 1.0]
    return [0.0, 0.0, 0.0, 1.0]


def _as_float(raw: Any) -> float:
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    return 0.0


def _ramp_node(
    item: TracedNode,
    material: MaterialGraph,
    *,
    graph: Graph,
    editable: bool,
    readonly_reason: str | None,
) -> RampNode:
    """色带：单一复合节点，色标取值可编辑（结构数量仍只读）。"""
    ramp = item.node.color_ramp or {}
    raw_elements = [e for e in (ramp.get("elements") or []) if isinstance(e, dict)]
    elements = [
        RampElement(
            index=index,
            position=float(element.get("position") or 0.0),
            color=_as_rgba(element.get("color")),
            position_baseline=float(element.get("position") or 0.0),
            color_baseline=_as_rgba(element.get("color")),
        )
        for index, element in enumerate(raw_elements)
    ]
    interpolation = str(ramp.get("interpolation") or "LINEAR")
    identity = identity_for(
        graph, item.name, "elements", "elements", material_id=material.material_id
    )
    binding = (
        Binding("MATERIAL_GRAPH_RAMP", f"{material.name}/{item.name}", "elements")
        if editable and identity is not None
        else None
    )
    return RampNode(
        id=_param_node_id(identity) if identity is not None else _param_node_id(
            _fallback_identity(graph, material, item.name, "elements", "elements")
        ),
        kind="ramp",
        group=GROUP,
        cost=_SEMANTIC_COST[SEM_RAMP],
        label=f"{_display_name(item)} 色带",
        binding=binding,
        elements=elements,
        interpolation=interpolation,
        interpolation_baseline=interpolation,
        element_count_baseline=len(elements),
        editable=editable,
        active=True,
        readonly_reason=readonly_reason,
        value_source=SOURCE_SCENE,
        impact={**_base_extra(item, material, "ramp"), "level": item.recognition.level},
        note="色带为单一复合节点：色标取值可编辑，色标数量属结构信息（只读）。",
    )


def _node_children(
    item: TracedNode,
    material: MaterialGraph,
    *,
    graph: Graph,
) -> list[Node]:
    """把一个追踪节点转成（可能多个）schema 子节点。"""
    semantic = item.recognition.semantic
    reason = _degrade_reason(item, graph, material)
    writable = reason is None
    level = item.recognition.level

    if semantic == SEM_RAMP:
        return [
            _ramp_node(item, material, graph=graph, editable=writable, readonly_reason=reason)
        ]

    if semantic == SEM_GROUP:
        return [
            EnumNode(
                id=_param_node_id(
                    _fallback_identity(graph, material, item.name, item.node.group_name, "group")
                ),
                kind="enum",
                group=GROUP,
                cost=COST_L1,
                label=f"{_display_name(item)} 节点组",
                value=item.node.group_name,
                baseline=item.node.group_name,
                effective=item.node.group_name,
                options=(
                    [{"value": item.node.group_name, "label": item.node.group_name}]
                    if item.node.group_name
                    else []
                ),
                supported=True,
                editable=False,
                active=True,
                readonly_reason=reason or REASON_SHARED_GROUP,
                value_source=SOURCE_SCENE,
                impact={**_base_extra(item, material, "enum"), "level": level},
                note="节点组实例：内部结构需穿透后才可编辑；跨组修改需先确认影响范围。",
            )
        ]

    children: list[Node] = []
    sockets = editable_sockets(item.node, semantic)
    used_fields: set[str] = set()
    for socket_name, field, kind in sockets:
        if field in used_fields:
            continue
        used_fields.add(field)
        socket = item.node.inputs.get(socket_name)
        raw_value = socket.value if socket is not None else None
        identity = identity_for(
            graph, item.name, socket_name, field, material_id=material.material_id
        ) or _fallback_identity(graph, material, item.name, socket_name, field)
        node_id = _param_node_id(identity)
        label = f"{_display_name(item)} · {socket_name}"
        impact = _base_extra(item, material, kind)
        if not writable or identity is None:
            children.append(
                _placeholder(
                    node_id=node_id,
                    label=label,
                    kind=kind,
                    impact=impact,
                    level=level,
                    semantic=semantic,
                    reason=reason or REASON_LINKED,
                )
            )
            continue
        binding = _binding(material.name, item.name, socket_name)
        if kind == "color":
            children.append(
                _color(
                    node_id=node_id,
                    label=label,
                    value=_as_rgba(raw_value),
                    impact=impact,
                    binding=binding,
                    level=level,
                    semantic=semantic,
                    editable=True,
                    readonly_reason=None,
                )
            )
        else:
            minimum, maximum, step = _bounds(semantic, field)
            children.append(
                _scalar(
                    node_id=node_id,
                    label=label,
                    value=_as_float(raw_value),
                    impact=impact,
                    binding=binding,
                    level=level,
                    semantic=semantic,
                    editable=True,
                    readonly_reason=None,
                    minimum=minimum,
                    maximum=maximum,
                    step=step,
                )
            )

    # 已连接的输入插座：不生成可写控件，只生成明确只读的占位（说明原因）。
    candidate_names = {name for name, _f, _k in sockets}
    for socket_name, source_desc in linked_inputs(item.node):
        if socket_name in candidate_names:
            continue
        node_id = _param_node_id(
                    _fallback_identity(graph, material, item.name, socket_name, "linked_input")
                )
        children.append(
            _placeholder(
                node_id=node_id,
                label=f"{_display_name(item)} · {socket_name}（已连线）",
                kind="float",
                impact=_base_extra(item, material, "float"),
                level=level,
                semantic=semantic,
                reason=REASON_LINKED,
            )
        )

    if not children:
        # 探不到任何可编辑插座 ⇒ 只读占位，保持节点集合稳定。
        children.append(
            _placeholder(
                node_id=_param_node_id(
                    _fallback_identity(graph, material, item.name, "unsupported", "unsupported")
                ),
                label=f"{_display_name(item)}（无可编辑参数）",
                kind="float",
                impact=_base_extra(item, material, "float"),
                level=level,
                semantic=semantic,
                reason=(reason or item.recognition.readonly_reason or "no_editable_socket"),
            )
        )
    return children


def _bounds(semantic: str, field: str) -> tuple[float | None, float | None, float | None]:
    """该语义字段的推荐值域（标量）。探不到事实时给保守范围，**不限制**取值。"""
    if semantic == "mix" and field in ("factor", "fac"):
        return 0.0, 1.0, 0.01
    if semantic == "hue_saturation" and field in ("saturation", "value"):
        return 0.0, 10.0, 0.01
    if semantic == "hue_saturation" and field == "hue":
        return 0.0, 1.0, 0.001
    return None, None, None


# -- 对外（规格形状）------------------------------------------------------


def to_spec(node: Node) -> dict[str, Any]:
    """把内部节点转成规格 §2 的对外参数形状。"""
    payload = node.to_public()
    impact = payload.get("impact") or {}
    spec = {
        "id": payload["id"],
        "label": payload.get("label"),
        "kind": payload.get("kind"),
        "confidence": impact.get("confidence"),
        "semantic": impact.get("semantic"),
        "semantic_label": SEMANTIC_LABELS.get(impact.get("semantic"), impact.get("semantic")),
        "evidence": list(impact.get("evidence") or []),
        "influence_paths": [list(p) for p in (impact.get("influence_paths") or [])],
        "affected_materials": list(impact.get("affected_materials") or []),
        "shared": bool(impact.get("shared")),
        "editable": bool(payload.get("editable")),
        "supported": bool(payload.get("supported")),
        "category": _category_of(node, impact),
        "readonly_reason": payload.get("readonly_reason"),
        "source_node": impact.get("source_node"),
        "source_label": impact.get("source_label"),
        "in_group": impact.get("in_group"),
        "used_outputs": list(impact.get("used_outputs") or []),
        "value": {
            "current": payload.get("value"),
            "baseline": payload.get("baseline"),
            "effective": payload.get("effective"),
        },
        "minimum": payload.get("minimum"),
        "maximum": payload.get("maximum"),
        "step": payload.get("step"),
    }
    if payload.get("kind") == "ramp":
        spec["elements"] = payload.get("elements")
        spec["interpolation"] = payload.get("interpolation")
        spec["element_count"] = payload.get("element_count")
    if "binding" in payload:
        # 只暴露字段名，不暴露任何可被误用为路径的拼接串
        binding = payload["binding"]
        spec["binding"] = {
            "object_type": binding.get("object_type"),
            "field": binding.get("field"),
        }
    return spec


def _category_of(node: Node, impact: dict[str, Any]) -> str:
    semantic = impact.get("semantic")
    level = impact.get("level") or impact.get("confidence")
    if level == LEVEL_INFERRED:
        return "inferred"
    return SEMANTIC_CATEGORY.get(semantic, "advanced")


def _build_nodes(material: MaterialGraph) -> tuple[list[Node], TraceResult | None]:
    """构造材质的内部参数节点树；不可用时返回空列表。"""
    if not material.available or material.graph is None:
        return [], None
    graph = material.graph
    trace = trace_from_output(graph)
    children: list[Node] = []
    if trace.ok:
        for item in trace.nodes:
            children.extend(_node_children(item, material, graph=graph))
    else:
        children.append(
            EnumNode(
                id=f"{GROUP}.{material.name}.degraded",
                kind="enum",
                group=GROUP,
                cost=COST_L1,
                label="追踪降级",
                value=trace.degraded_reason,
                baseline=trace.degraded_reason,
                effective=trace.degraded_reason,
                options=(
                    [{"value": trace.degraded_reason, "label": trace.degraded_reason}]
                    if trace.degraded_reason
                    else []
                ),
                supported=False,
                editable=False,
                active=False,
                readonly_reason=trace.degraded_reason or "unsupported",
                value_source=SOURCE_UNSUPPORTED,
                reason="反向追踪无法唯一确定生效链路，整份参数面降级为只读。",
            )
        )
    return children, trace


def build_parameter_schema_with_nodes(
    material: MaterialGraph,
) -> tuple[dict[str, Any], list[Node]]:
    """生成 schema 的对外形状 + 内部节点树（供参数索引构建）。"""
    if not material.available or material.graph is None:
        return (
            {
                "schema_version": SCHEMA_VERSION,
                "material_id": material.material_id,
                "material": material.name,
                "available": False,
                "error": material.error or "材质图不可用。",
                "groups": [],
                "parameters": [],
                "categories": {name: [] for name in CATEGORIES},
                "highest_cost": COST_L1,
            },
            [],
        )
    children, trace = _build_nodes(material)
    assert trace is not None
    group_node = GroupNode(
        id=f"{GROUP}.{material.name}",
        kind="group",
        group=GROUP,
        cost=max_cost(children) if children else COST_L1,
        label=material.name,
        supported=True,
        editable=True,
        active=True,
        value_source=SOURCE_SCENE,
        impact={
            "users": list(material.users),
            "shared_groups": dict(material.shared_groups),
        },
        children=children,
    )
    parameters = [to_spec(node) for node in children]
    categories: dict[str, list[str]] = {name: [] for name in CATEGORIES}
    for spec in parameters:
        categories.setdefault(spec["category"], []).append(spec["id"])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "material_id": material.material_id,
        "material": material.name,
        "available": True,
        "structure_hash": material.structure_hash,
        "degraded_reason": trace.degraded_reason,
        "trace": {
            "entry": trace.entry,
            "entry_output": trace.entry_output,
            "max_depth": trace.max_depth,
            "cycle": list(trace.cycle),
            "nodes": [item.to_public() for item in trace.nodes],
        },
        "shared_groups": dict(material.shared_groups),
        "groups": [group_node.to_public()],
        "parameters": parameters,
        "categories": categories,
        "highest_cost": max_cost([group_node]),
    }
    return payload, children


def build_parameter_schema(material: MaterialGraph) -> dict[str, Any]:
    """为单个材质生成递归参数 schema（对外形状）。

    返回内部节点树（``groups``）+ 规格形状的扁平参数（``parameters``）+ 分区索引。
    """
    payload, _nodes = build_parameter_schema_with_nodes(material)
    return payload


def build_schemas(materials: list[MaterialGraph]) -> dict[str, Any]:
    """为一批材质生成 schema，并汇总参数总数 / 各置信等级计数。"""
    items = [build_parameter_schema(material) for material in materials]
    levels: dict[str, int] = {}
    total = 0
    for item in items:
        for spec in item.get("parameters") or []:
            total += 1
            level = spec.get("confidence")
            if level:
                levels[level] = levels.get(level, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "materials": items,
        "material_count": len(items),
        "parameter_count": total,
        "levels": levels,
    }


__all__ = [
    "GROUP",
    "SCHEMA_VERSION",
    "to_spec",
    "build_parameter_schema",
    "build_parameter_schema_with_nodes",
    "build_schemas",
]
