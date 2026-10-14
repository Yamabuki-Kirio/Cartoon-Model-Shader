"""材质图探针入口：把 ``surface_probe`` 的 describe 载荷整理成可追踪的图。

只读：本模块不生成任何写入代码，也不接受客户端输入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .graph import Graph, build_graph
from .identity import structure_hash


@dataclass
class MaterialGraph:
    """单个材质的图 + 影响范围 + 结构哈希。"""

    material_id: str
    name: str
    users: list[str] = field(default_factory=list)
    classification: str | None = None
    graph: Graph | None = None
    structure_hash: str = ""
    #: 该材质引用的节点组名 -> 共享该组的其它材质名（含自身）
    shared_groups: dict[str, list[str]] = field(default_factory=dict)
    #: 探测阶段的降级原因（拿不到图时非空）
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.graph is not None


def materials_index(describe: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """材质名 -> 原始条目。"""
    out: dict[str, dict[str, Any]] = {}
    for item in describe.get("materials") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name:
            out[name] = item
    return out


def build_material_graph(
    describe: dict[str, Any],
    material_name: str,
    *,
    classification: str | None = None,
) -> MaterialGraph:
    """为某个材质构造图。找不到或图不可用时不抛错，返回 ``available=False``。"""
    from .identity import material_id_of

    materials = materials_index(describe)
    raw = materials.get(material_name)
    if raw is None:
        return MaterialGraph(
            material_id=material_id_of(material_name),
            name=material_name,
            error="未在被对象引用的材质清单中找到该材质。",
        )
    graph_raw = raw.get("graph")
    if not isinstance(graph_raw, dict) or not graph_raw.get("nodes"):
        return MaterialGraph(
            material_id=material_id_of(material_name),
            name=material_name,
            users=sorted(str(u) for u in (raw.get("users") or [])),
            error="该材质没有可用的节点图（未启用节点，或探测未返回结构）。",
        )
    graph = build_graph(material_name, graph_raw)
    shared_raw = describe.get("shared_groups") or {}
    referenced_groups: dict[str, list[str]] = {}
    for node in graph.nodes.values():
        if not node.group_name:
            continue
        users = [str(u) for u in (shared_raw.get(node.group_name) or [])]
        referenced_groups[node.group_name] = sorted(set(users) or {material_name})
    return MaterialGraph(
        material_id=material_id_of(material_name),
        name=material_name,
        users=sorted(str(u) for u in (raw.get("users") or [])),
        classification=classification,
        graph=graph,
        structure_hash=structure_hash(graph),
        shared_groups=referenced_groups,
    )
