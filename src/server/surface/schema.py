"""递归 schema：参数树的数据模型（技术方案 §4）。

设计要点
--------
* **每个节点自带状态**：``supported`` / ``editable`` / ``active`` / ``readonly_reason``。
  这四件是「探不到就降级」与「L3 未通过门禁前只读」的唯一表达方式 ——
  不再用「加一个 flag 让前端自己判断」这种散落各处的做法。
* **三值并存**：``value``（当前/草稿）、``baseline``、``effective``（真正生效）。
  三者分开是为了让「配置值 / 生效值 / 显示标签」这类老问题不再复发。
* **复合节点不展开**：``ramp`` 是单一节点，内部含 ``elements``。
  理由见技术方案 §4.3：ColorRamp 是单个 datablock，整体写入才原子。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import errors
from .binding import Binding, assert_allowed, is_readonly

KIND_FLOAT = "float"
KIND_INT = "int"
KIND_BOOL = "bool"
KIND_ENUM = "enum"
KIND_COLOR = "color"
KIND_VECTOR = "vector"
KIND_RAMP = "ramp"
KIND_GROUP = "group"

SCHEMA_VERSION = "toon-surface/2"

COST_L0 = "L0"
COST_L1 = "L1"
COST_L2 = "L2"
COST_L3 = "L3"
COSTS: tuple[str, ...] = (COST_L0, COST_L1, COST_L2, COST_L3)

SOURCE_SCENE = "scene"
SOURCE_DEFAULT = "default"
SOURCE_DRAFT = "draft"
SOURCE_UNSUPPORTED = "unsupported"

#: 只读原因（稳定枚举，前端据此出文案）
REASON_NOT_FOUND = "not_found"
REASON_ROLLBACK_UNAVAILABLE = "rollback_unavailable"
REASON_KEYFRAMED_OR_CONSTRAINED = "keyframed_or_constrained"
REASON_REFERENCE_ONLY = "reference_only"
REASON_STRUCTURAL = "structural"
#: 探到了，但**真实 Blender 的插座名尚未确认**，因此不敢写。
#: 用在 Emission 强度这类「候选名 + 结构验证」命中、但还没拿到真机拓扑的能力上：
#: 探测结果照实报告（``supported: true``），写入一律拒绝。
REASON_UNCONFIRMED_CAPABILITY = "unconfirmed_capability"


@dataclass
class Node:
    """所有节点的公共部分。"""

    id: str
    kind: str
    group: str
    cost: str
    label: str = ""
    binding: Binding | None = None
    supported: bool = True
    editable: bool = True
    active: bool = True
    readonly_reason: str | None = None
    value_source: str = SOURCE_SCENE
    note: str = ""
    unit: str = ""
    structural: bool = False
    impact: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    #: 可选：值域（标量用）
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None

    def __post_init__(self) -> None:
        if self.binding is not None:
            assert_allowed(self.binding)
            # 只读字段永不可编辑（白名单层面已声明），避免漏标
            if is_readonly(self.binding):
                self.editable = False
                self.readonly_reason = self.readonly_reason or REASON_STRUCTURAL
        if not self.editable and self.readonly_reason is None:
            self.readonly_reason = REASON_ROLLBACK_UNAVAILABLE

    # -- 序列化 -----------------------------------------------------------
    def base_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "group": self.group,
            "cost": self.cost,
            "label": self.label or self.id,
            "supported": bool(self.supported),
            "editable": bool(self.editable),
            "active": bool(self.active),
            "readonly_reason": self.readonly_reason,
            "value_source": self.value_source,
            "structural": bool(self.structural),
        }
        if self.binding is not None:
            payload["binding"] = self.binding.to_public()
        if self.note:
            payload["note"] = self.note
        if self.unit:
            payload["unit"] = self.unit
        if self.impact:
            payload["impact"] = dict(self.impact)
        if self.reason:
            payload["reason"] = self.reason
        return payload

    def to_public(self) -> dict[str, Any]:  # pragma: no cover - 由子类实现
        raise NotImplementedError


@dataclass
class ScalarNode(Node):
    """float / int / bool。"""

    value: Any = None
    baseline: Any = None
    effective: Any = None

    def to_public(self) -> dict[str, Any]:
        payload = self.base_payload()
        payload.update(
            {
                "value": self.value,
                "baseline": self.baseline,
                "effective": self.effective,
                "minimum": self.minimum,
                "maximum": self.maximum,
                "step": self.step,
            }
        )
        return payload


@dataclass
class EnumNode(Node):
    value: str | None = None
    baseline: str | None = None
    effective: str | None = None
    options: list[dict[str, str]] = field(default_factory=list)
    depends_on: str | None = None
    options_dynamic: bool = False

    @property
    def allowed_values(self) -> list[str]:
        return [str(option.get("value")) for option in self.options]

    def to_public(self) -> dict[str, Any]:
        payload = self.base_payload()
        payload.update(
            {
                "value": self.value,
                "baseline": self.baseline,
                "effective": self.effective,
                "options": [dict(option) for option in self.options],
                "depends_on": self.depends_on,
                "options_dynamic": self.options_dynamic,
            }
        )
        return payload


@dataclass
class ColorNode(Node):
    """RGBA。长度恒为 4，校验在这里收口。"""

    value: list[float] | None = None
    baseline: list[float] | None = None
    effective: list[float] | None = None

    def to_public(self) -> dict[str, Any]:
        payload = self.base_payload()
        payload.update(
            {
                "value": list(self.value) if self.value else None,
                "baseline": list(self.baseline) if self.baseline else None,
                "effective": list(self.effective) if self.effective else None,
            }
        )
        return payload


@dataclass
class VectorNode(Node):
    """分轴向量；``space`` 明确空间（决策 4：灯光用世界空间）。"""

    value: list[float] | None = None
    baseline: list[float] | None = None
    effective: list[float] | None = None
    axes: list[str] = field(default_factory=lambda: ["x", "y", "z"])
    space: str = "world"
    locked_ratio: bool = False

    def to_public(self) -> dict[str, Any]:
        payload = self.base_payload()
        payload.update(
            {
                "value": list(self.value) if self.value else None,
                "baseline": list(self.baseline) if self.baseline else None,
                "effective": list(self.effective) if self.effective else None,
                "axes": list(self.axes),
                "space": self.space,
                "locked_ratio": bool(self.locked_ratio),
            }
        )
        return payload


@dataclass
class RampElement:
    index: int
    position: float
    color: list[float]
    position_baseline: float | None = None
    color_baseline: list[float] | None = None
    minimum: float = 0.0
    maximum: float = 1.0

    def to_public(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "position": {
                "value": self.position,
                "baseline": self.position_baseline,
                "minimum": self.minimum,
                "maximum": self.maximum,
                "step": 0.001,
            },
            "color": {
                "value": list(self.color),
                "baseline": list(self.color_baseline) if self.color_baseline else None,
            },
        }


@dataclass
class RampNode(Node):
    """复合色带：单一节点，内部逐色标可编辑，写入走**整体替换**。

    ``element_count`` 是子节点而不是同级兄弟：它属于**结构**信息，
    且决策 3 要求它变化时使草稿与确认令牌失效。
    """

    elements: list[RampElement] = field(default_factory=list)
    interpolation: str = "LINEAR"
    interpolation_baseline: str | None = None
    interpolation_options: list[dict[str, str]] = field(default_factory=list)
    element_count_baseline: int | None = None
    max_elements: int = 32
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def element_count(self) -> int:
        return len(self.elements)

    def to_public(self) -> dict[str, Any]:
        payload = self.base_payload()
        payload.update(
            {
                "elements": [element.to_public() for element in self.elements],
                "interpolation": {
                    "value": self.interpolation,
                    "baseline": self.interpolation_baseline,
                    "options": [dict(option) for option in self.interpolation_options],
                },
                "element_count": {
                    "value": self.element_count,
                    "baseline": self.element_count_baseline,
                    "structural": True,
                    "cost": COST_L3,
                    # 决策 3 + L3 门禁：结构性增删在检查点机制验收前不可编辑
                    "supported": True,
                    "editable": False,
                    "readonly_reason": REASON_ROLLBACK_UNAVAILABLE,
                    "minimum": 2,
                    "maximum": self.max_elements,
                },
                "checks": [dict(check) for check in self.checks],
            }
        )
        return payload

    # -- 便捷取值 ---------------------------------------------------------
    def element_values(self) -> list[dict[str, Any]]:
        return [
            {"position": element.position, "color": list(element.color)}
            for element in self.elements
        ]


@dataclass
class GroupNode(Node):
    """分组容器：递归 carry 子节点。"""

    children: list[Node] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        payload = self.base_payload()
        payload["children"] = [child.to_public() for child in self.children]
        return payload


# -- 树操作 ----------------------------------------------------------------


def walk(nodes: Iterable[Node]) -> Iterable[Node]:
    """深度优先遍历（含分组自身）。"""
    for node in nodes:
        yield node
        if isinstance(node, GroupNode):
            yield from walk(node.children)


def flatten(nodes: Iterable[Node]) -> dict[str, Node]:
    """``id -> 节点`` 索引。**id 唯一性在这里校验**：重复直接抛。"""
    index: dict[str, Node] = {}
    duplicates: list[str] = []
    for node in walk(nodes):
        if node.id in index:
            duplicates.append(node.id)
        index[node.id] = node
    if duplicates:
        raise errors.ToonTunerError(
            errors.SCHEMA_INVALID,
            "参数 schema 存在重复 id：" + "、".join(sorted(set(duplicates))[:8]),
            details={"duplicates": sorted(set(duplicates))},
        )
    return index


def collect_bindings(nodes: Iterable[Node]) -> list[Binding]:
    """收集全部声明出来的 binding（供启动期白名单自检与双向覆盖断言使用）。"""
    return [node.binding for node in walk(nodes) if node.binding is not None]


def max_cost(nodes: Iterable[Node]) -> str:
    """整棵树的最高重算层级（技术方案 §10：混合修改按最高成本层级调度）。"""
    order = {cost: index for index, cost in enumerate(COSTS)}
    highest = 0
    for node in walk(nodes):
        highest = max(highest, order.get(node.cost, 0))
    return COSTS[highest]


def public_tree(nodes: list[Node]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "groups": [node.to_public() for node in nodes],
        "highest_cost": max_cost(nodes),
    }
