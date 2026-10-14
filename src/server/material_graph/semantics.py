"""节点语义识别与置信等级（规格 §2「语义与置信度」）。

本模块只做**纯映射**：给定节点类型 / 插座组合，返回：
* 它属于哪一类可编辑语义（数值 / 颜色 / 色带 / 运算 / 色相调整 / 混合 / 着色器…）；
* 它每个可编辑字段的 kind 与 binding 字段名；
* 该参数的**置信等级**与**只读原因**。

置信等级（规格 §2）
--------------------
``confirmed``      —— 类型、目标插座和路径全部明确：可写；
``inferred``       —— 路径明确但语义依赖名称或规则推断：展示、试调，**默认不写**；
``user_confirmed`` —— 用户显式确认过（由上层记录；本模块**从不自行产生**）；
``experimental``   —— 需要微扰验证：展示、试调，写入需确认；
``unsupported``    —— 无法安全执行或回滚：只读展示。

**绝不猜**：这里没有「按名字前缀猜语义」的分支。所有映射都基于 Blender 的节点
``type`` 常量与插座**类型**；候选插座名只用于「命中判定」，命中之后还要结构验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# -- 置信等级 --------------------------------------------------------------

LEVEL_CONFIRMED = "confirmed"
LEVEL_INFERRED = "inferred"
LEVEL_USER_CONFIRMED = "user_confirmed"
LEVEL_EXPERIMENTAL = "experimental"
LEVEL_UNSUPPORTED = "unsupported"

LEVELS: tuple[str, ...] = (
    LEVEL_CONFIRMED,
    LEVEL_INFERRED,
    LEVEL_USER_CONFIRMED,
    LEVEL_EXPERIMENTAL,
    LEVEL_UNSUPPORTED,
)

#: 等级 -> 是否默认可写。只有 ``confirmed`` / ``user_confirmed`` 默认可写；
#: ``inferred`` / ``experimental`` 展示与试调，正式写入需用户确认。
WRITABLE_LEVELS: frozenset[str] = frozenset({LEVEL_CONFIRMED, LEVEL_USER_CONFIRMED})

# -- 语义分类 --------------------------------------------------------------

SEM_VALUE = "value"
SEM_COLOR = "color"
SEM_RAMP = "ramp"
SEM_MATH = "math"
SEM_MAP_RANGE = "map_range"
SEM_HUE_SAT = "hue_saturation"
SEM_BRIGHT_CONTRAST = "bright_contrast"
SEM_GAMMA = "gamma"
SEM_INVERT = "invert"
SEM_MIX = "mix"
SEM_MIX_SHADER = "mix_shader"
SEM_GROUP = "group"
SEM_TEXTURE = "texture"
SEM_REROUTE = "reroute"
SEM_SHADER = "shader"
SEM_OTHER = "other"

SEMANTIC_LABELS: dict[str, str] = {
    SEM_VALUE: "数值",
    SEM_COLOR: "颜色",
    SEM_RAMP: "色带",
    SEM_MATH: "数值运算",
    SEM_MAP_RANGE: "范围映射",
    SEM_HUE_SAT: "色相/饱和度",
    SEM_BRIGHT_CONTRAST: "亮度/对比度",
    SEM_GAMMA: "伽马",
    SEM_INVERT: "反相",
    SEM_MIX: "颜色混合",
    SEM_MIX_SHADER: "着色器混合",
    SEM_GROUP: "节点组",
    SEM_TEXTURE: "纹理控制",
    SEM_REROUTE: "转接",
    SEM_SHADER: "着色器",
    SEM_OTHER: "其它",
}

#: 每个语义归属的 schema 分区（规格 §2「Schema 分类」）。
SEMANTIC_CATEGORY: dict[str, str] = {
    SEM_VALUE: "numeric",
    SEM_COLOR: "color",
    SEM_RAMP: "ramp",
    SEM_MATH: "numeric",
    SEM_MAP_RANGE: "numeric",
    SEM_HUE_SAT: "color",
    SEM_BRIGHT_CONTRAST: "color",
    SEM_GAMMA: "color",
    SEM_INVERT: "color",
    SEM_MIX: "color",
    SEM_MIX_SHADER: "advanced",
    SEM_GROUP: "node_group",
    SEM_TEXTURE: "texture",
    SEM_REROUTE: "advanced",
    SEM_SHADER: "common",
    SEM_OTHER: "advanced",
}

#: 所有合法分区（前端据此渲染固定顺序的折叠面板）。
CATEGORIES: tuple[str, ...] = (
    "common",
    "color",
    "numeric",
    "ramp",
    "node_group",
    "texture",
    "inferred",
    "advanced",
)

# -- 节点类型 -> 语义 ------------------------------------------------------
#: 键是 Blender 的 ``node.type`` 常量（探针上报的就是它），**不是**名称。
#: 同一语义可能对应多个类型常量（Blender 4.x 把 ``MIX_RGB`` 拆成 ``MIX``）。
NODE_SEMANTICS: dict[str, str] = {
    # 着色器入口
    "BSDF_PRINCIPLED": SEM_SHADER,
    "BSDF_DIFFUSE": SEM_SHADER,
    "BSDF_TOON": SEM_SHADER,
    "EMISSION": SEM_SHADER,
    "BSDF_EMISSION": SEM_SHADER,
    "OUTPUT_MATERIAL": SEM_SHADER,
    # 着色器混合（Add Shader 与 Mix Shader）
    "MIX_SHADER": SEM_MIX_SHADER,
    "ADD_SHADER": SEM_MIX_SHADER,
    # 颜色混合
    "MIX_RGB": SEM_MIX,
    "MIX": SEM_MIX,
    # 数值
    "VALUE": SEM_VALUE,
    # 颜色
    "RGB": SEM_COLOR,
    # 色带
    "VALTORGB": SEM_RAMP,
    # 运算
    "MATH": SEM_MATH,
    # 范围映射
    "MAP_RANGE": SEM_MAP_RANGE,
    # 颜色调整
    "HUE_SAT": SEM_HUE_SAT,
    "BRIGHTCONTRAST": SEM_BRIGHT_CONTRAST,
    "GAMMA": SEM_GAMMA,
    "INVERT": SEM_INVERT,
    # 转接
    "REROUTE": SEM_REROUTE,
    # 纹理控制
    "TEX_IMAGE": SEM_TEXTURE,
    "TEX_COORD": SEM_TEXTURE,
    "MAPPING": SEM_TEXTURE,
    # 节点组
    "GROUP": SEM_GROUP,
}

#: 各语义的**候选**可编辑插座名 -> ``(field, kind)``。命中判定后还要结构验证。
#: 字段名必须命中 ``surface.binding.BINDING_WHITELIST``（节点参数统一走 NODE_SOCKET）。
SOCKET_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    SEM_VALUE: {"Value": ("default_value", "float")},
    SEM_COLOR: {"Color": ("default_value", "color")},
    SEM_MAP_RANGE: {
        "Value": ("default_value", "float"),
        "From Min": ("from_min", "float"),
        "From Max": ("from_max", "float"),
        "To Min": ("to_min", "float"),
        "To Max": ("to_max", "float"),
    },
    SEM_MIX: {
        "Fac": ("factor", "float"),
        "Factor": ("factor", "float"),
        "Color A": ("color_a", "color"),
        "Color B": ("color_b", "color"),
    },
    SEM_HUE_SAT: {
        "Hue": ("hue", "float"),
        "Saturation": ("saturation", "float"),
        "Value": ("value", "float"),
        "Fac": ("fac", "float"),
    },
    SEM_BRIGHT_CONTRAST: {
        "Bright": ("bright", "float"),
        "Contrast": ("contrast", "float"),
    },
    SEM_GAMMA: {"Gamma": ("gamma", "float")},
    SEM_INVERT: {"Fac": ("fac", "float")},
    SEM_MATH: {
        "Value": ("default_value", "float"),
        "Value_001": ("value_1", "float"),
        "Value_002": ("value_2", "float"),
    },
    # 着色器的常量候选插座：Base Color 等（与逐材质适配器同一套候选）
    SEM_SHADER: {
        "Base Color": ("base_color", "color"),
        "Metallic": ("metallic", "float"),
        "Roughness": ("roughness", "float"),
        "Emission Color": ("emission_color", "color"),
        "Emission Strength": ("emission_strength", "float"),
        "Specular IOR Level": ("specular", "float"),
    },
    # 节点组公开输入（Group Input 接口）由穿透流程按组接口名映射
    SEM_MIX_SHADER: {},  # 混合着色器的 Fac 常量在着色器侧，此处不下发可写项
}

# -- 只读原因 --------------------------------------------------------------

REASON_NOT_FOUND = "not_found"
REASON_LINKED = "linked_upstream"
REASON_STRUCTURAL = "structural"
REASON_UNCONFIRMED_CAPABILITY = "unconfirmed_capability"
REASON_CYCLE = "cycle_detected"
REASON_TOO_DEEP = "too_deep"
REASON_AMBIGUOUS_PATH = "ambiguous_path"
REASON_SHARED_GROUP = "shared_group_requires_confirmation"
REASON_EXPERIMENTAL = "experimental_semantics"
REASON_INFERRED = "inferred_not_confirmed"
REASON_REROUTE = "reroute_transparent"


@dataclass(frozen=True)
class Recognition:
    """一次语义识别的结果（规格 §2 的「置信等级 + 只读原因」）。"""

    semantic: str
    level: str
    readonly_reason: str | None = None
    evidence: tuple[str, ...] = ()

    @property
    def writable(self) -> bool:
        return self.level in WRITABLE_LEVELS and self.readonly_reason is None

    @property
    def category(self) -> str:
        """schema 分区：``inferred`` 优先于语义自身的分区。"""
        if self.level == LEVEL_INFERRED:
            return "inferred"
        return SEMANTIC_CATEGORY.get(self.semantic, "advanced")

    def to_public(self) -> dict[str, Any]:
        return {
            "semantic": self.semantic,
            "semantic_label": SEMANTIC_LABELS.get(self.semantic, self.semantic),
            "level": self.level,
            "confidence": self.level,
            "category": self.category,
            "writable": self.writable,
            "readonly_reason": self.readonly_reason,
            "evidence": list(self.evidence),
        }


def semantic_of(node_type: str, *, group_name: str = "") -> str:
    """由节点类型常量推断语义；节点组实例优先判为 ``group``。"""
    if group_name:
        return SEM_GROUP
    return NODE_SEMANTICS.get(str(node_type), SEM_OTHER)


def is_ramp(node: Any) -> bool:
    """色带判定走**结构**（节点上挂着 ``color_ramp``），类型常量只作参考。"""
    return getattr(node, "color_ramp", None) is not None


def classify(node: Any) -> Recognition:
    """给出单个节点的置信等级与证据摘要。

    * 探到 ``color_ramp`` ⇒ 色带，``confirmed``（结构确定）；
    * 已知类型的确定性节点 ⇒ ``confirmed``；
    * 着色器常量插座 ⇒ ``confirmed``（与逐材质适配器同一条候选 + 结构验证）；
    * 混合 / 颜色调整 ⇒ ``inferred``（语义随混合模式或规则变化）；
    * 转接节点 ⇒ ``inferred``（透明，不产生可写参数）；
    * 未知类型 ⇒ ``unsupported``；
    * 节点组实例 ⇒ ``experimental``（跨组修改需确认）。
    """
    group_name = str(getattr(node, "group_name", "") or "")
    node_type = str(getattr(node, "type", "") or "")
    if group_name:
        return Recognition(
            SEM_GROUP,
            LEVEL_EXPERIMENTAL,
            REASON_SHARED_GROUP,
            evidence=(f"节点 {node_type} 引用节点组 {group_name}",),
        )
    if is_ramp(node):
        return Recognition(
            SEM_RAMP,
            LEVEL_CONFIRMED,
            evidence=("节点挂载 color_ramp 结构",),
        )
    semantic = NODE_SEMANTICS.get(node_type)
    if semantic is None:
        return Recognition(
            SEM_OTHER,
            LEVEL_UNSUPPORTED,
            REASON_UNCONFIRMED_CAPABILITY,
            evidence=(f"未识别的节点类型 {node_type}",),
        )
    if semantic == SEM_REROUTE:
        return Recognition(SEM_REROUTE, LEVEL_INFERRED, REASON_REROUTE,
                           evidence=("转接节点透传，无可写参数",))
    if semantic in (SEM_MIX, SEM_HUE_SAT, SEM_BRIGHT_CONTRAST, SEM_GAMMA, SEM_INVERT):
        return Recognition(
            semantic,
            LEVEL_INFERRED,
            evidence=(f"节点类型 {node_type} 的语义随模式/规则变化，需真机确认",),
        )
    if semantic == SEM_MIX_SHADER:
        return Recognition(
            SEM_MIX_SHADER,
            LEVEL_EXPERIMENTAL,
            REASON_EXPERIMENTAL,
            evidence=("着色器混合，改动需微扰验证",),
        )
    return Recognition(
        semantic,
        LEVEL_CONFIRMED,
        evidence=(f"节点类型 {node_type} 语义确定",),
    )


def _socket_linked(socket: Any) -> bool:
    """兼容两种插座表示：图模型用 ``linked``，bpy / 桩用 ``is_linked``。"""
    return bool(getattr(socket, "linked", False)) or bool(
        getattr(socket, "is_linked", False)
    )


def editable_sockets(node: Any, semantic: str) -> list[tuple[str, str, str]]:
    """返回该节点上**可编辑的（插座名, 字段, kind）**。

    规则（与 Cel / 材质同一条纪律）：
    * 插座必须真的存在；
    * 插座**不能被上游连线**（写 ``default_value`` 不生效）；
    * 插座名必须在候选表内。
    """
    fields = SOCKET_FIELDS.get(semantic)
    if not fields:
        return []
    out: list[tuple[str, str, str]] = []
    inputs = getattr(node, "inputs", {}) or {}
    for socket_name, (field, kind) in fields.items():
        socket = inputs.get(socket_name)
        if socket is None:
            continue
        if _socket_linked(socket):
            continue
        out.append((socket_name, field, kind))
    return out


def linked_inputs(node: Any) -> list[tuple[str, str]]:
    """返回所有**已连线**的输入插座 ``(插座名, 来源节点::插座 描述)``。"""
    out: list[tuple[str, str]] = []
    incoming = getattr(node, "incoming", {}) or {}
    inputs = getattr(node, "inputs", {}) or {}
    for socket_name, socket in inputs.items():
        if not _socket_linked(socket):
            continue
        src = incoming.get(socket_name)
        desc = f"{src[0]}::{src[1]}" if src else "上游节点"
        out.append((socket_name, desc))
    return out


__all__ = [
    "LEVELS",
    "LEVEL_CONFIRMED",
    "LEVEL_INFERRED",
    "LEVEL_USER_CONFIRMED",
    "LEVEL_EXPERIMENTAL",
    "LEVEL_UNSUPPORTED",
    "WRITABLE_LEVELS",
    "CATEGORIES",
    "SEM_VALUE",
    "SEM_COLOR",
    "SEM_RAMP",
    "SEM_MATH",
    "SEM_MAP_RANGE",
    "SEM_HUE_SAT",
    "SEM_BRIGHT_CONTRAST",
    "SEM_GAMMA",
    "SEM_INVERT",
    "SEM_MIX",
    "SEM_MIX_SHADER",
    "SEM_GROUP",
    "SEM_TEXTURE",
    "SEM_REROUTE",
    "SEM_SHADER",
    "SEM_OTHER",
    "SEMANTIC_LABELS",
    "SEMANTIC_CATEGORY",
    "NODE_SEMANTICS",
    "SOCKET_FIELDS",
    "REASON_NOT_FOUND",
    "REASON_LINKED",
    "REASON_STRUCTURAL",
    "REASON_UNCONFIRMED_CAPABILITY",
    "REASON_CYCLE",
    "REASON_TOO_DEEP",
    "REASON_AMBIGUOUS_PATH",
    "REASON_SHARED_GROUP",
    "REASON_EXPERIMENTAL",
    "REASON_INFERRED",
    "REASON_REROUTE",
    "Recognition",
    "semantic_of",
    "is_ramp",
    "classify",
    "editable_sockets",
    "linked_inputs",
]
