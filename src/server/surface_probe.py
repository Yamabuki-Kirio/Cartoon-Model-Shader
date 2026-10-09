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


def _material_inventory():
    materials = _safe(lambda: list(bpy.data.materials), None) or []
    out = []
    for mat in materials:
        out.append({{
            "name": str(_safe(lambda m=mat: m.name, "") or ""),
            "blend_method": str(_safe(lambda m=mat: m.blend_method, "") or ""),
            "has_nodes": bool(_safe(lambda m=mat: m.use_nodes, False)),
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
_PAYLOAD["materials"] = _material_inventory()
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
        "materials": list(payload.get("materials") or []),
        "images": list(payload.get("images") or []),
    }
