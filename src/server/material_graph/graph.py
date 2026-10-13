"""只读节点图模型与循环检测。

图完全由 ``surface_probe`` 的探测结果构造：节点/插座/连线都以**名称**为标识，
且名称只来自服务端探测，客户端永远无法提交节点路径。

为什么需要单独的图模型
----------------------
反向追踪需要频繁做「某个输入插座的来源节点是谁」这类查询。把探测结果整理成
邻接表后，追踪器只依赖这个窄接口，不直接翻原始 dict —— 也便于对循环依赖做检测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

#: 追踪允许的最大递归深度（规格 §3：建议 8）。超过即判为异常结构，降级只读。
MAX_DEPTH = 8


@dataclass(frozen=True)
class Socket:
    name: str
    type: str = ""
    linked: bool = False
    value: Any = None


@dataclass
class GraphNode:
    name: str
    type: str
    bl_idname: str = ""
    label: str = ""
    mute: bool = False
    group_name: str = ""
    inputs: dict[str, Socket] = field(default_factory=dict)
    outputs: tuple[str, ...] = ()
    color_ramp: dict[str, Any] | None = None
    #: 输入插座名 -> 来源 ``(node_name, socket_name)``
    incoming: dict[str, tuple[str, str]] = field(default_factory=dict)

    def is_linked_input(self, socket: str) -> bool:
        return socket in self.incoming


@dataclass
class Graph:
    material_name: str
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    #: 相连的节点集合（无论方向）；用于影响范围与共享判定
    links: list[tuple[str, str, str, str]] = field(default_factory=list)

    # -- 查询 ------------------------------------------------------------
    def get(self, name: str) -> GraphNode | None:
        return self.nodes.get(name)

    def source_of(self, node_name: str, socket: str) -> tuple[str, str] | None:
        node = self.nodes.get(node_name)
        if node is None:
            return None
        return node.incoming.get(socket)

    def output_nodes(self) -> list[GraphNode]:
        return [n for n in self.nodes.values() if n.type == "OUTPUT_MATERIAL"]


def build_graph(material_name: str, raw: dict[str, Any] | None) -> Graph:
    """从探测载荷构造图。``raw`` 形如 ``{"nodes": [...], "links": [...]}``。"""
    graph = Graph(material_name=material_name)
    if not isinstance(raw, dict):
        return graph

    for item in raw.get("nodes") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        inputs: dict[str, Socket] = {}
        for sock in item.get("inputs") or []:
            if not isinstance(sock, dict):
                continue
            sname = str(sock.get("name") or "")
            if not sname:
                continue
            inputs[sname] = Socket(
                name=sname,
                type=str(sock.get("type") or ""),
                linked=bool(sock.get("linked")),
                value=sock.get("value"),
            )
        outputs = tuple(
            str(sock.get("name") or "")
            for sock in (item.get("outputs") or [])
            if isinstance(sock, dict) and sock.get("name")
        )
        graph.nodes[name] = GraphNode(
            name=name,
            type=str(item.get("type") or ""),
            bl_idname=str(item.get("bl_idname") or ""),
            label=str(item.get("label") or ""),
            mute=bool(item.get("mute")),
            group_name=str(item.get("group_name") or ""),
            inputs=inputs,
            outputs=outputs,
            color_ramp=item.get("color_ramp") if isinstance(item.get("color_ramp"), dict) else None,
        )

    for link in raw.get("links") or []:
        if not isinstance(link, dict):
            continue
        from_node = str(link.get("from_node") or "")
        from_socket = str(link.get("from_socket") or "")
        to_node = str(link.get("to_node") or "")
        to_socket = str(link.get("to_socket") or "")
        if not (from_node and to_node):
            continue
        target = graph.nodes.get(to_node)
        if target is None:
            continue
        # 一个输入插座只可能有一条入边（Blender 语义）；后写覆盖不会发生，但仍显式记录。
        target.incoming[to_socket] = (from_node, from_socket)
        target.inputs.setdefault(to_socket, Socket(name=to_socket, linked=True))
        entry = target.inputs[to_socket]
        if not entry.linked:
            target.inputs[to_socket] = Socket(
                name=entry.name, type=entry.type, linked=True, value=entry.value
            )
        graph.links.append((from_node, from_socket, to_node, to_socket))

    return graph


def entry_source(graph: Graph) -> tuple[GraphNode, str] | None:
    """材质的着色器入口：输出节点唯一 + 其 ``Surface`` 输入恰好一条连线。

    与 ``surface.material`` 的 ``_surface_shader`` 同一条规矩：**不唯一就不返回**，
    由调用方整体降级只读，绝不猜。
    """
    outputs = graph.output_nodes()
    if len(outputs) != 1:
        return None
    out = outputs[0]
    source = out.incoming.get("Surface")
    if source is None:
        return None
    node = graph.get(source[0])
    if node is None:
        return None
    return node, source[1]


def find_cycle(graph: Graph, start: str) -> list[str] | None:
    """从 ``start`` 沿入边做 DFS，返回发现的环（若有）。用于循环依赖检测。"""
    #: 组内节点也是同一张图里的节点（探测时已把组内节点打平）；环检测仍按入边方向。
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    seen: set[str] = set()
    while stack:
        current, path = stack.pop()
        node = graph.get(current)
        if node is None:
            continue
        if len(path) > MAX_DEPTH * 4:
            return path  # 过深本身按异常处理
        for _socket, (src_node, _src_socket) in node.incoming.items():
            if src_node in path:
                return path[path.index(src_node) :] + [src_node]
            if src_node in seen:
                continue
            seen.add(src_node)
            stack.append((src_node, path + [src_node]))
    return None


def reachable_from(graph: Graph, start: str, *, max_depth: int = MAX_DEPTH) -> set[str]:
    """从 ``start`` 沿入边可达的节点集合（受深度限制）。"""
    result: set[str] = set()
    frontier: list[tuple[str, int]] = [(start, 0)]
    while frontier:
        name, depth = frontier.pop()
        if depth >= max_depth:
            continue
        node = graph.get(name)
        if node is None:
            continue
        for _socket, (src_node, _src_socket) in node.incoming.items():
            if src_node in result:
                continue
            result.add(src_node)
            frontier.append((src_node, depth + 1))
    return result


def impact_scope(graph: Graph, node_names: Iterable[str]) -> dict[str, Any]:
    """给定一组节点，返回它们影响到的「终点」（输出节点 + 组输入）摘要。"""
    endpoints: list[str] = []
    for name in graph.nodes:
        node = graph.nodes[name]
        if node.type == "OUTPUT_MATERIAL":
            endpoints.append(node.name)
    return {"endpoints": sorted(endpoints), "nodes": sorted(set(node_names))}
