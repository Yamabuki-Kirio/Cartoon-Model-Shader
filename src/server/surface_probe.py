"""只读拓扑探针：把「本工具关心的场景结构」导出成**脱敏**描述。

用途（提交 1 的第一步）
----------------------
Cel 色带的真实节点/插座名称无法凭空确定，也不能靠猜。因此先交付一个**只读**探针：
在真实 Blender 里跑一次，把受管节点组的拓扑原样 dump 出来。schema 生成器**以探测结果为准**：
探到什么就生成什么可编辑节点，探不到就生成 ``supported: false`` 的只读节点。

安全约束（与既有模块一致）
--------------------------
* 生成的代码是后端内置常量拼接，不接受任何客户端输入（本模块没有入参）；
* **只读**：不写任何 ``bpy`` 数据，不渲染、不保存、不改活动对象与选择状态；
* **脱敏**：输出只保留 datablock 名称 / 类型 / 结构 / 能力 / 必要指纹；
  工程的绝对路径、贴图绝对路径、令牌、用户目录一律不出现 ——
  由本模块的 ``redact_describe()`` 在服务端**再做一遍**兜底（不依赖 Blender 侧自觉）。
"""

from __future__ import annotations

import json
from typing import Any

from . import errors
from .redact import redact, redact_value

DESCRIBE_MARKER = "__TOON_SURFACE_DESCRIBE__"
DESCRIBE_SCHEMA = "toon-surface-describe/1"

#: 本工具**声明管理**的节点组名单。不在此名单内的一律只读展示，
#: 绝不做「按名称前缀猜测」——见技术方案 §1 决策 5。
MANAGED_NODE_GROUPS: tuple[str, ...] = (
    "Cel_Skin",
    "Cel_Hair",
    "Cel_Cloth",
    "Cel_Dark",
    "Cel_Eyes",
    "RayToon_Face_Soft",
    "RayToon_Eyes_Unlit",
)

#: 只读探测 / 回退策略对象：存在即报告，但不视为可编辑。
REFERENCE_NODE_GROUPS: tuple[str, ...] = ("Sakura_Hair_Reference",)

#: 合成器节点组宿主（Blender 5.x 是场景级节点组）
COMPOSITOR_GROUP_NAME = "AI_Compositor"

#: 结构签名的组成字段（**不含值**）：只有这些变化才算结构变化。
STRUCTURE_FIELDS: tuple[str, ...] = ("nodes", "color_ramp_elements", "material_slots")

#: 材质「安全参数」对应的着色器插座**候选**名。
#:
#: 与 Cel 的 Emission 同一条规矩：候选名只是线索，**不是事实**。
#: 命中之后还要过结构验证 —— 材质输出唯一、来源节点唯一、插座未被连线 ——
#: 才算「已确认能力」，才允许写入；任何一条不满足就降级只读。
#: 真机确认之前不写死任何一项（Blender 4.x 把 ``Specular`` 改名为
#: ``Specular IOR Level``，两个候选都留着）。
MATERIAL_SOCKET_CANDIDATES: tuple[str, ...] = (
    "Base Color",
    "Metallic",
    "Roughness",
    "Emission Color",
    "Emission Strength",
    "Specular IOR Level",
    "Specular",
)


def build_describe_code() -> str:
    """生成只读拓扑探测代码。

    无入参：不接受任何客户端输入，从结构上排除注入面。
    输出所有字面量均为内置常量，且以 ``repr`` 形式拼入。
    """
    return f'''
import json
import bpy

_PAYLOAD = {{}}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _vec(value):
    try:
        return [round(float(v), 6) for v in value]
    except Exception:
        return None


def _socket_value(value):
    """插座当前值：只保留可安全序列化的标量/元组，字符串原样。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    try:
        return [round(float(v), 6) for v in value]
    except Exception:
        return None


def _socket_report(node):
    out = []
    for sock in _safe(lambda: list(node.inputs), None) or []:
        out.append({{
            "name": str(_safe(lambda: sock.name, "") or ""),
            "type": str(_safe(lambda: sock.type, "") or ""),
            "linked": bool(_safe(lambda: sock.is_linked, False)),
            "value": _socket_value(_safe(lambda: getattr(sock, "default_value", None))),
        }})
    return out


def _ramp_report(node):
    """ColorRamp 结构：元素数量 + 每个色标的位置/颜色。数量属**结构**字段。"""
    ramp = _safe(lambda: node.color_ramp)
    if ramp is None:
        return None
    elements = _safe(lambda: list(ramp.elements), None) or []
    report = {{
        "element_count": len(elements),
        "interpolation": str(_safe(lambda: ramp.interpolation, "") or ""),
        "elements": [],
    }}
    for element in elements:
        report["elements"].append({{
            "position": _safe(lambda: round(float(element.position), 6)),
            "color": _vec(_safe(lambda: element.color)),
        }})
    return report


def _node_report(node):
    """单个节点：类型/名称/是否 mute/插座/色带。**不含任何值语义之外的字段**。"""
    return {{
        "name": str(_safe(lambda: node.name, "") or ""),
        "type": str(_safe(lambda: node.type, "") or ""),
        "bl_idname": str(_safe(lambda: node.bl_idname, "") or ""),
        "mute": bool(_safe(lambda: node.mute, False)),
        "inputs": _socket_report(node),
        "color_ramp": _ramp_report(node),
    }}


def _group_report(name):
    ng = _safe(lambda: bpy.data.node_groups.get(name))
    if ng is None:
        return None
    nodes = _safe(lambda: list(ng.nodes), None) or []
    report = {{
        "name": str(name),
        "exists": True,
        "node_count": len(nodes),
        "nodes": [_node_report(n) for n in nodes],
    }}
    # 结构签名：只含节点名/类型/色标数量，**不含值**
    report["structure_signature"] = sorted(
        "{{}}:{{}}:{{}}".format(
            str(_safe(lambda n=n: n.name, "") or ""),
            str(_safe(lambda n=n: n.type, "") or ""),
            (
                _safe(lambda n=n: len(list(n.color_ramp.elements)), -1)
                if _safe(lambda n=n: n.color_ramp) is not None
                else -1
            ),
        )
        for n in nodes
    )
    return report


def _object_inventory():
    objects = _safe(lambda: list(bpy.data.objects), None) or []
    counters = {{}}
    for obj in objects:
        kind = str(_safe(lambda o=obj: o.type, "UNKNOWN") or "UNKNOWN")
        counters[kind] = counters.get(kind, 0) + 1
    return {{"total": len(objects), "by_type": counters}}


def _material_users():
    """材质名 -> 使用它的对象名。只统计**真的挂在对象材质槽上**的材质。"""
    out = {{}}
    for obj in _safe(lambda: list(bpy.data.objects), None) or []:
        for slot in _safe(lambda o=obj: list(o.material_slots), None) or []:
            mat = _safe(lambda s=slot: s.material)
            if mat is None:
                continue
            name = str(_safe(lambda m=mat: m.name, "") or "")
            if not name:
                continue
            out.setdefault(name, []).append(str(_safe(lambda o=obj: o.name, "") or ""))
    return out


def _shader_socket_report(node, candidates):
    """只报告候选插座：材质节点上其余插座一律不进载荷（收敛暴露面，也收敛体积）。"""
    out = []
    for sock in _safe(lambda: list(node.inputs), None) or []:
        name = str(_safe(lambda s=sock: s.name, "") or "")
        if name not in candidates:
            continue
        out.append({{
            "name": name,
            "type": str(_safe(lambda s=sock: s.type, "") or ""),
            "linked": bool(_safe(lambda s=sock: s.is_linked, False)),
            "value": _socket_value(_safe(lambda s=sock: getattr(s, "default_value", None))),
        }})
    return out


def _surface_shader(tree, candidates):
    """材质输出所连的那个着色器节点；**只有唯一确定**才返回，否则 None。

    唯一确定 = 恰好一个 ``OUTPUT_MATERIAL`` 节点 + 它的 ``Surface`` 输入恰好一条连线。
    不满足就返回 None，由适配器整体降级为只读 —— 与 Cel 侧「绝不猜 socket 名」同一条规矩。
    """
    nodes = _safe(lambda: list(tree.nodes), None) or []
    outputs = [
        n for n in nodes if str(_safe(lambda n=n: n.type, "") or "") == "OUTPUT_MATERIAL"
    ]
    if len(outputs) != 1:
        return None
    sources = []
    for link in _safe(lambda: list(tree.links), None) or []:
        if _safe(lambda l=link: l.to_node) is not outputs[0]:
            continue
        if str(_safe(lambda l=link: l.to_socket.name, "") or "") != "Surface":
            continue
        source = _safe(lambda l=link: l.from_node)
        if source is not None:
            sources.append(source)
    if len(sources) != 1:
        return None
    node = sources[0]
    return {{
        "name": str(_safe(lambda n=node: n.name, "") or ""),
        "type": str(_safe(lambda n=node: n.type, "") or ""),
        "bl_idname": str(_safe(lambda n=node: n.bl_idname, "") or ""),
        "linked_to_output": True,
        "sockets": _shader_socket_report(node, candidates),
    }}


def _material_inventory(candidates):
    """只报告**被对象引用**的材质 —— 没挂到任何对象上的材质不属于本工具的受管范围。

    每项带：使用对象、节点结构签名（不含值）、以及材质输出所连着色器节点的候选插座。
    """
    users = _material_users()
    out = []
    for mat in _safe(lambda: list(bpy.data.materials), None) or []:
        name = str(_safe(lambda m=mat: m.name, "") or "")
        if not name:
            continue
        used_by = sorted(users.get(name) or [])
        if not used_by:
            continue
        use_nodes = bool(_safe(lambda m=mat: m.use_nodes, False))
        tree = _safe(lambda m=mat: m.node_tree) if use_nodes else None
        nodes = _safe(lambda t=tree: list(t.nodes), None) or []
        out.append({{
            "name": name,
            "blend_method": str(_safe(lambda m=mat: getattr(m, "blend_method", "")) or ""),
            "use_nodes": use_nodes,
            "node_count": len(nodes),
            "structure_signature": sorted(
                "{{}}:{{}}".format(
                    str(_safe(lambda n=n: n.name, "") or ""),
                    str(_safe(lambda n=n: n.type, "") or ""),
                )
                for n in nodes
            ),
            "users": used_by,
            "shader": _surface_shader(tree, candidates) if tree is not None else None,
        }})
    return out


def _image_inventory():
    """只报告**名称与色彩空间**，绝不报告文件路径（路径由服务端兜底脱敏）。"""
    images = _safe(lambda: list(bpy.data.images), None) or []
    out = []
    for image in images:
        out.append({{
            "name": str(_safe(lambda i=image: i.name, "") or ""),
            "colorspace": str(
                _safe(lambda i=image: i.colorspace_settings.name, "") or ""
            ),
            "size": _vec(_safe(lambda i=image: i.size)),
        }})
    return out


_PAYLOAD["schema"] = {DESCRIBE_SCHEMA!r}
_PAYLOAD["blender_version"] = str(_safe(lambda: bpy.app.version_string, "unknown"))
_PAYLOAD["compositor_group"] = str({COMPOSITOR_GROUP_NAME!r})
_PAYLOAD["managed_groups"] = []
for _name in {list(MANAGED_NODE_GROUPS)!r}:
    _report = _group_report(_name)
    if _report is None:
        _report = {{"name": _name, "exists": False}}
    _report["role"] = "managed"
    _PAYLOAD["managed_groups"].append(_report)
_PAYLOAD["reference_groups"] = []
for _name in {list(REFERENCE_NODE_GROUPS)!r}:
    _report = _group_report(_name)
    if _report is None:
        _report = {{"name": _name, "exists": False}}
    _report["role"] = "reference"
    _PAYLOAD["reference_groups"].append(_report)
_PAYLOAD["objects"] = _object_inventory()
_PAYLOAD["materials"] = _material_inventory({list(MATERIAL_SOCKET_CANDIDATES)!r})
_PAYLOAD["images"] = _image_inventory()

print({DESCRIBE_MARKER!r} + json.dumps(_PAYLOAD, ensure_ascii=False, default=str))
'''.strip()


def parse_describe(stdout: str) -> dict[str, Any]:
    """从捕获的 stdout 解析描述载荷。"""
    payload: str | None = None
    for line in stdout.splitlines():
        if line.startswith(DESCRIBE_MARKER):
            payload = line[len(DESCRIBE_MARKER):]
    if payload is None:
        raise errors.BlenderUnexpectedResponse(
            "拓扑探针输出中未找到结构化结果行。",
            details={"stdout_tail": redact(stdout[-500:])},
        )
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise errors.BlenderUnexpectedResponse(
            "拓扑探针输出的结构化结果不是合法 JSON。",
            details={"stdout_tail": redact(stdout[-500:])},
        ) from exc
    if not isinstance(parsed, dict):
        raise errors.BlenderUnexpectedResponse("拓扑探针输出的结构不是对象。")
    return parsed


def redact_describe(payload: dict[str, Any]) -> dict[str, Any]:
    """服务端兜底脱敏：即使 Blender 侧漏了一个路径，也不会出到响应里。

    做法：递归去掉键名像路径的字段，再对全部字符串跑 ``redact``
    （与既有 ``redact.py`` 同一套规则：盘符路径 / UNC / ``/Users`` / ``/home``）。
    """
    cleaned = _drop_path_keys(payload)
    result = redact_value(cleaned)
    return result if isinstance(result, dict) else {}


#: 键名命中即整个丢弃（这些字段在任何响应里都不该出现）。
#: 判定用「精确名 + 后缀」，因为真实数据里会出现 ``texture_path``、``output_dir``
#: 这类派生键 —— 只匹配精确名会漏。
_PATH_KEYS = frozenset(
    {"filepath", "file_path", "path", "paths", "abspath", "absolute_path", "directory", "dir"}
)
_PATH_KEY_SUFFIXES = ("path", "paths", "dir", "dirs", "directory", "directories")


def _is_path_key(key: Any) -> bool:
    text = str(key).strip().lower()
    if text in _PATH_KEYS:
        return True
    return text.endswith(_PATH_KEY_SUFFIXES)


def _drop_path_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _drop_path_keys(item)
            for key, item in value.items()
            if not _is_path_key(key)
        }
    if isinstance(value, list):
        return [_drop_path_keys(item) for item in value]
    return value


def describe_groups(payload: dict[str, Any]) -> dict[str, Any]:
    """把描述整理成 schema 生成器直接可用的形状。

    ``supported`` 的判定只依赖「探到了没有」：探不到就是 ``supported: false`` +
    明确 reason，**绝不猜 socket 名**，也绝不假设某个组一定存在。
    """
    groups: list[dict[str, Any]] = []
    for raw in list(payload.get("managed_groups") or []) + list(
        payload.get("reference_groups") or []
    ):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        exists = bool(raw.get("exists"))
        role = str(raw.get("role") or "managed")
        ramps = [
            node
            for node in (raw.get("nodes") or [])
            if isinstance(node, dict) and isinstance(node.get("color_ramp"), dict)
        ]
        groups.append(
            {
                "name": name,
                "exists": exists,
                "role": role,
                # 只读探测对象即使存在也不可编辑（回退策略用）
                "editable": bool(exists and role == "managed"),
                "supported": exists,
                "reason": None if exists else "未在工程中找到该节点组",
                "node_count": int(raw.get("node_count") or 0),
                "ramp_count": len(ramps),
                "ramp_element_counts": [
                    int(node["color_ramp"].get("element_count") or 0) for node in ramps
                ],
                "structure_signature": list(raw.get("structure_signature") or []),
                # 节点级拓扑：Cel 适配器需要它来判断「有没有色带、色带在哪、
                # 有没有 Emission 强度插座」。已经过 redact_describe 脱敏。
                "nodes": [
                    {
                        "name": node.get("name"),
                        "type": node.get("type"),
                        "bl_idname": node.get("bl_idname"),
                        "mute": bool(node.get("mute")),
                        "inputs": list(node.get("inputs") or []),
                        "color_ramp": node.get("color_ramp"),
                    }
                    for node in (raw.get("nodes") or [])
                    if isinstance(node, dict)
                ],
            }
        )
    found = [group for group in groups if group["exists"]]
    return {
        "schema": payload.get("schema") or DESCRIBE_SCHEMA,
        "blender_version": payload.get("blender_version"),
        "compositor_group": payload.get("compositor_group"),
        "groups": groups,
        "found_groups": len(found),
        "declared_groups": len(groups),
        # 声明了但探不到 ⇒ 明确降级，而不是假装支持
        "degraded": sorted(group["name"] for group in groups if not group["exists"]),
        "objects": payload.get("objects") or {},
        "materials": [_normalise_material(item) for item in (payload.get("materials") or [])],
        "images": list(payload.get("images") or []),
    }


def _normalise_material(raw: Any) -> dict[str, Any]:
    """把材质条目收敛成**固定字段**的形状。

    与 ``groups`` 同一条纪律：只保留 schema 生成器真的会读的字段，
    其余一律丢弃 —— 免得 Blender 侧将来多吐一个字段就悄悄进了响应。
    """
    item = raw if isinstance(raw, dict) else {}
    shader = item.get("shader")
    shader_out: dict[str, Any] | None = None
    if isinstance(shader, dict):
        shader_out = {
            "name": str(shader.get("name") or ""),
            "type": str(shader.get("type") or ""),
            "bl_idname": str(shader.get("bl_idname") or ""),
            "linked_to_output": bool(shader.get("linked_to_output")),
            "sockets": [
                {
                    "name": str(socket.get("name") or ""),
                    "type": str(socket.get("type") or ""),
                    "linked": bool(socket.get("linked")),
                    "value": socket.get("value"),
                }
                for socket in (shader.get("sockets") or [])
                if isinstance(socket, dict)
            ],
        }
    return {
        "name": str(item.get("name") or ""),
        "blend_method": str(item.get("blend_method") or ""),
        "use_nodes": bool(item.get("use_nodes")),
        "node_count": int(item.get("node_count") or 0),
        "structure_signature": [str(entry) for entry in (item.get("structure_signature") or [])],
        "users": sorted(str(entry) for entry in (item.get("users") or [])),
        "shader": shader_out,
    }
