"""从 ``Material Output`` 反向追踪实际生效链路（规格 §2「节点链追踪 / 节点组穿透」）。

追踪规则
--------
1. **只跟踪真正影响输出的节点**：从唯一有效的 ``Material Output.Surface`` 连线开始
   反向遍历；孤立节点（既不在上游也不被引用）不进入默认参数面；
2. 入口唯一：材质输出节点唯一 + ``Surface`` 输入恰好一条连线，否则整体降级（不猜）；
3. 最大深度 ``MAX_DEPTH=8``，超深降级；
4. 检测循环引用：发现环即整段降级只读（``cycle_detected``），不写；
5. 多输出节点记录**实际连接的输出插座**（谁被下游取用）；
6. 同一节点被多条路径访问时**去重**，但保留**全部影响路径**；
7. 路径歧义（同一输出插座被多个下游引用）时标记 ``ambiguous_path``，降级不猜。

节点组穿透
----------
组内节点不在材质自身的 ``nodes`` 里。探针上报组**实例**与 ``group_name``；
调用方负责把组内节点以「扁平化」命名 ``<组实例名>::<组内节点名>`` 并入同一张图，
并补上 ``Group Input`` / ``Group Output`` 到内部节点的连线。穿透即为「沿组接口继续走」。
递归嵌套节点组同样表现为更长的 ``::`` 前缀链；深度与循环保护复用同一套上限。

本模块**只读**：不做任何写入，也不接受客户端输入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .graph import (
    MAX_DEPTH,
    Graph,
    GraphNode,
    entry_source,
    find_cycle,
)
from .identity import node_identity, path_hash
from .semantics import (
    REASON_CYCLE,
    REASON_NOT_FOUND,
    REASON_TOO_DEEP,
    Recognition,
    classify,
)

#: 节点组内节点的名字分隔符（由调用方在扁平化时拼出，客户端无法提交）。
GROUP_SEP = "::"

#: 组接口节点的类型常量。
GROUP_INPUT_TYPES = frozenset({"GROUP_INPUT"})
GROUP_OUTPUT_TYPES = frozenset({"GROUP_OUTPUT"})


@dataclass
class TracedNode:
    """追踪到的单个生效节点。"""

    name: str
    node: GraphNode
    depth: int
    identity: str
    path_hash: str
    recognition: Recognition
    #: 该节点直接的上游节点名（生效链路的下一跳）
    upstream: list[str] = field(default_factory=list)
    #: 该节点是否位于节点组内部（穿透进来的）
    in_group: str = ""
    #: 该节点影响到的下游终点（用于「影响路径」展示）
    downstream: list[str] = field(default_factory=list)
    #: 本节点被下游实际取用的**输出插座**名（规格：多输出节点要记录）
    used_outputs: list[str] = field(default_factory=list)
    #: 从材质输出到本节点的**全部**影响路径（每条为节点名列表，输出端在前）
    influence_paths: list[list[str]] = field(default_factory=list)
    #: 每个已连接输入插座 -> 来源 ``节点::插座``（证据用）
    incoming_edges: list[dict[str, str]] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.node.type,
            "bl_idname": self.node.bl_idname,
            "label": self.node.label,
            "mute": bool(self.node.mute),
            "in_group": self.in_group,
            "depth": self.depth,
            "node_id": self.identity,
            "path_hash": self.path_hash,
            **self.recognition.to_public(),
            "upstream": list(self.upstream),
            "downstream": list(self.downstream),
            "used_outputs": list(self.used_outputs),
            "influence_paths": [list(p) for p in self.influence_paths],
            "incoming_edges": [dict(e) for e in self.incoming_edges],
        }


@dataclass
class TraceResult:
    """一次反向追踪的完整结果。"""

    material_name: str
    nodes: list[TracedNode] = field(default_factory=list)
    #: 整段链路的降级原因；非空即整份只读
    degraded_reason: str | None = None
    cycle: list[str] = field(default_factory=list)
    max_depth: int = 0
    entry: str = ""
    #: 入口着色器被取用的输出插座名（如 ``BSDF``）
    entry_output: str = ""

    @property
    def ok(self) -> bool:
        return self.degraded_reason is None and bool(self.nodes)

    def index(self) -> dict[str, TracedNode]:
        return {item.name: item for item in self.nodes}


def _group_chain(name: str) -> list[str]:
    """``A::B::node`` -> ``["A", "B"]``；``node`` -> ``[]``。"""
    parts = name.split(GROUP_SEP)
    return parts[:-1] if len(parts) > 1 else []


def _group_of(name: str) -> str:
    chain = _group_chain(name)
    return GROUP_SEP.join(chain) if chain else ""


def trace_from_output(graph: Graph, *, max_depth: int = MAX_DEPTH) -> TraceResult:
    """从材质输出反向追踪生效链路。

    降级（返回 ``degraded_reason`` 非空）的三种情形，**都不猜**：
    * 入口不唯一 / Surface 未连线 → ``not_found``；
    * 存在循环引用 → ``cycle_detected``；
    * 链路深度超过 ``max_depth`` → ``too_deep``。
    """
    result = TraceResult(material_name=graph.material_name)
    entry = entry_source(graph)
    if entry is None:
        result.degraded_reason = REASON_NOT_FOUND
        return result
    entry_node, entry_socket = entry
    result.entry = entry_node.name
    result.entry_output = entry_socket

    cycle = find_cycle(graph, entry_node.name)
    if cycle:
        result.cycle = cycle
        result.degraded_reason = REASON_CYCLE
        return result

    # 反向 BFS：沿入边向上游走，同时记录「下游 -> 上游」的边与全部路径。
    frontier: list[tuple[str, list[str]]] = [(entry_node.name, [entry_node.name])]
    visited: dict[str, int] = {}
    paths: dict[str, list[list[str]]] = {}
    #: 下游节点 -> 取用到的 (上游节点, 输出插座)
    used: dict[str, set[tuple[str, str]]] = {}
    edges: dict[str, list[tuple[str, str, str]]] = {}
    while frontier:
        name, path = frontier.pop()
        depth = len(path) - 1
        if depth > max_depth:
            result.degraded_reason = REASON_TOO_DEEP
            return result
        # 记录全部路径（去重：节点集合相同即视为同一路径）
        bucket = paths.setdefault(name, [])
        if path not in bucket:
            bucket.append(path)
        if name in visited and visited[name] <= depth:
            continue
        visited[name] = depth
        result.max_depth = max(result.max_depth, depth)
        node = graph.get(name)
        if node is None:
            continue
        for socket_name, (src_node, src_socket) in node.incoming.items():
            used.setdefault(src_node, set()).add((name, src_socket))
            edges.setdefault(src_node, []).append((socket_name, name, src_socket))
            frontier.append((src_node, [*path, src_node]))

    for name, depth in sorted(visited.items(), key=lambda item: (item[1], item[0])):
        node = graph.get(name)
        if node is None:
            continue
        group = _group_of(name)
        incoming_edges = [
            {
                "to_socket": to_socket,
                "from_node": src_node,
                "from_socket": src_socket,
            }
            for to_socket, (src_node, src_socket) in sorted(node.incoming.items())
        ]
        used_outputs = sorted(
            {src_socket for _down, src_socket in used.get(name, set())}
        )
        result.nodes.append(
            TracedNode(
                name=name,
                node=node,
                depth=depth,
                identity=node_identity(node, depth=depth),
                path_hash=path_hash(graph, name),
                recognition=classify(node),
                upstream=sorted({src for src, _ in node.incoming.values()}),
                in_group=group,
                downstream=sorted({down for down, _s in used.get(name, set())}),
                used_outputs=used_outputs,
                influence_paths=[list(p) for p in paths.get(name, [])],
                incoming_edges=incoming_edges,
            )
        )
    return result


def detect_ambiguous(graph: Graph, name: str) -> bool:
    """该节点的路径是否**不唯一**（同一输出插座被多个下游取用 ⇒ 无法确定哪条生效）。"""
    node = graph.get(name)
    if node is None:
        return True
    per_socket: dict[str, int] = {}
    for src_node, src_socket, _to_node, _to_socket in graph.links:
        if src_node != name:
            continue
        per_socket[src_socket] = per_socket.get(src_socket, 0) + 1
    return any(count > 1 for count in per_socket.values())


def group_users(graph: Graph) -> list[str]:
    """返回本图中出现的节点组名。"""
    names: set[str] = set()
    for node in graph.nodes.values():
        if node.group_name:
            names.add(node.group_name)
    return sorted(names)


def group_interface_map(graph: Graph) -> dict[str, dict[str, Any]]:
    """节点组接口映射：``组名 -> {inputs, outputs, internal}``。

    * ``inputs``：组实例输入插座 -> 组内被喂到的 ``节点::插座``（经 ``Group Input``）；
    * ``outputs``：``Group Output`` 的来源 ``节点::插座`` -> 组实例输出插座名；
    * ``internal``：组内（扁平化命名）节点列表。

    映射一律来自**探测结果**（组内节点以 ``<实例>::<内部>`` 命名并入图）。
    无法确定映射时**不猜**：此处只报告图里真实存在的边。
    """
    out: dict[str, dict[str, Any]] = {}
    for group in group_users(graph):
        prefix = f"{group}{GROUP_SEP}"
        internal = sorted(name for name in graph.nodes if name.startswith(prefix))
        inputs: dict[str, list[str]] = {}
        outputs: dict[str, str] = {}
        for name in internal:
            node = graph.get(name)
            if node is None:
                continue
            simple = name[len(prefix):]
            if node.type in GROUP_INPUT_TYPES:
                # Group Input 无入边；其输出被组内节点取用
                for link in graph.links:
                    if link[0] == name:
                        inputs.setdefault(simple, []).append(f"{link[2]}::{link[3]}")
            elif node.type in GROUP_OUTPUT_TYPES:
                for _sock, (src_node, src_socket) in node.incoming.items():
                    outputs[f"{src_node}::{src_socket}"] = simple
        out[group] = {"inputs": inputs, "outputs": outputs, "internal": internal}
    return out


__all__ = [
    "GROUP_SEP",
    "GROUP_INPUT_TYPES",
    "GROUP_OUTPUT_TYPES",
    "TracedNode",
    "TraceResult",
    "trace_from_output",
    "detect_ambiguous",
    "group_users",
    "group_interface_map",
]
