"""稳定参数身份与结构哈希（规格 §5）。

参数不能只依赖节点名称 —— 用户重命名后不能整体失效。身份按优先级选择来源：

1. 工具写入的自定义 UUID（``node["uuid"]``；
2. Blender datablock 稳定信息 + 节点身份组合（``bl_idname`` + 插座 + 字段）；
3. 节点类型 + 拓扑位置 + 上下游结构形成的**结构身份**；
4. 节点名称**只作显示与辅助迁移**，不作唯一主键。

结构哈希只包含：节点增删、连接变化、插座结构变化、节点组接口变化。
**参数数值不进入结构哈希** —— 否则用户在 Blender 里拖一下滑块就会让整份草稿失效。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .graph import Graph


@dataclass(frozen=True)
class ParamIdentity:
    """一个可编辑参数的稳定身份。"""

    material_id: str
    node_tree_id: str
    node_id: str
    socket: str
    field: str
    path_hash: str

    def param_id(self) -> str:
        """对外参数 ID：opaque、不含真实节点名（避免前端依赖内部命名）。"""
        key = "|".join(
            [self.material_id, self.node_tree_id, self.node_id, self.socket, self.field]
        )
        return "mg-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    def to_public(self) -> dict[str, str]:
        return {
            "param_id": self.param_id(),
            "material_id": self.material_id,
            "node_tree_id": self.node_tree_id,
            "node_id": self.node_id,
            "socket": self.socket,
            "field": self.field,
            "path_hash": self.path_hash,
        }


def _h(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def node_identity(node: Any, *, depth: int = 0) -> str:
    """节点的稳定身份。

    优先级：自定义 UUID > 结构身份（类型 + 上下游拓扑 + 插座 + 深度）。
    节点名称**不参与**主键计算，只作为最后的结构位置提示。
    """
    uuid = ""
    try:
        uuid = str(getattr(node, "uuid", "") or "")
    except Exception:  # noqa: BLE001
        uuid = ""
    if uuid:
        return f"uuid:{uuid}"

    # 结构身份：类型 + bl_idname + 输入/输出插座名 + 深度。
    inputs = ",".join(sorted(getattr(node, "inputs", {}).keys()))
    outputs = ",".join(sorted(getattr(node, "outputs", ())))
    structural = _h(
        str(getattr(node, "type", "")),
        str(getattr(node, "bl_idname", "")),
        inputs,
        outputs,
        str(depth),
    )
    return f"struct:{structural}"


def path_hash(graph: Graph, node_name: str) -> str:
    """从节点到材质输出的一条「结构路径」哈希。

    只包含结构（节点类型 + 经过的插座名），**不含数值** —— 因此调值不会让身份漂移。
    """
    chain: list[str] = []
    current = node_name
    guard = 0
    while current:
        guard += 1
        if guard > 32:  # 防御环
            break
        node = graph.get(current)
        if node is None:
            break
        chain.append(f"{node.type}:{node.bl_idname}:{current}")
        if node.type == "OUTPUT_MATERIAL":
            break
        # 沿「第一条入边」继续向上游（结构路径，不关心具体插座名顺序）
        nxt = None
        for _sock, (src_node, _src_socket) in node.incoming.items():
            nxt = src_node
            break
        current = nxt or ""
    return "sha256:" + _h(*chain)


#: 结构哈希参与字段（顺序固定，便于稳定复现）。
_STRUCTURE_NODE_FIELDS = ("type", "bl_idname", "mute", "group_name")


def structure_hash(graph: Graph) -> str:
    """整个材质图的结构哈希。

    只包含：节点增删（节点集合）、连接变化（连线端点）、插座结构（输入/输出插座名）、
    节点组接口（``group_name``）。**任意参数值都不进入。**
    """
    nodes: list[str] = []
    for name in sorted(graph.nodes):
        node = graph.nodes[name]
        nodes.append(
            "|".join(
                [
                    str(getattr(node, field, ""))
                    for field in _STRUCTURE_NODE_FIELDS
                ]
                + [
                    ",".join(sorted(node.inputs.keys())),
                    ",".join(sorted(node.outputs)),
                    ",".join(sorted(node.incoming.keys())),
                ]
            )
        )
    links = sorted(
        "{}:{}->{}:{}".format(*link) for link in graph.links
    )
    blob = json.dumps({"nodes": nodes, "links": links}, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def identity_for(graph: Graph, node_name: str, socket: str, field: str, *, material_id: str) -> ParamIdentity | None:
    """构造某节点某插座的参数身份；节点不存在返回 None。"""
    node = graph.get(node_name)
    if node is None:
        return None
    return ParamIdentity(
        material_id=material_id,
        node_tree_id=f"NODE_TREE:{graph.material_name}",
        node_id=node_identity(node),
        socket=socket,
        field=field,
        path_hash=path_hash(graph, node_name),
    )


def material_id_of(name: str) -> str:
    """材质身份：``MATERIAL:<name>``（与规格 §5 的示例一致）。"""
    return f"MATERIAL:{name}"


def collect_param_identities(identities: Iterable[ParamIdentity]) -> dict[str, ParamIdentity]:
    """param_id -> 身份 的索引；重复即视为结构冲突（调用方决定如何处理）。"""
    out: dict[str, ParamIdentity] = {}
    for identity in identities:
        out[identity.param_id()] = identity
    return out
