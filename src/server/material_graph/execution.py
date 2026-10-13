"""材质图草稿校验与写入计划编译（规格「提交 3：安全执行、回读与回滚」）。

职责边界
--------
本模块把「客户端提交的材质图草稿」编译成 ``surface.executor.PlanOp`` 列表，
复用既有执行器的**原子写入 / 快照 / 回滚 / 回读**机制。它只做**服务端侧**的
校验与编排，不做任何 Blender 调用。

完整执行顺序（规格原文）
------------------------
    校验会话令牌 → 校验基线 → 校验结构哈希 → 用当前 Schema 解析参数 ID
    → 校验值类型和范围 → 计算依赖顺序 → 快照所有受影响目标 → 应用完整草稿
    → 回读逐项校验 → 渲染 → 恢复快照 → 回读确认恢复结果

本模块承担前三步之后的「服务端可完成部分」：解析参数 ID、校验值、编译计划。
令牌 / 基线 / 结构哈希 / 渲染 / 恢复由 ``session`` 与 ``surface_service`` 承担。

安全约束（不可绕过）
--------------------
* 参数 ID 必须命中**当前基线**的材质图 schema；
* ``field`` 必须命中固定白名单（经 ``PlanOp`` 二次把关）；
* 节点 / 插座 / 节点组 / 材质身份**全部来自服务端探测结果**，绝不来自请求；
* 共享节点组参数在未确认影响范围前**拒绝写入**（``SHARED_PARAMETER_CONFIRM_REQUIRED``）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import errors
from ..surface.binding import Binding
from ..surface.executor import PlanOp
from ..surface.schema import (
    ColorNode,
    EnumNode,
    GroupNode,
    Node,
    RampNode,
    ScalarNode,
    VectorNode,
    flatten,
)
from .identity import ParamIdentity

#: 材质图草稿里每个可写参数的**服务端侧**描述（基线捕获时生成一次）。
@dataclass(frozen=True)
class GraphParam:
    """一个可写材质图参数的稳定描述。"""

    param_id: str
    binding: Binding
    kind: str
    identity: ParamIdentity
    #: 该参数所属的共享组（无则空串）
    shared_group: str = ""
    #: 共享组的使用材质集合（含自身）
    shared_users: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    #: 色带：基线色标数量（数量变化属结构改动，拒绝）
    element_count: int | None = None
    #: 基线取值（恢复基线用）。色带为 ``{"elements": [...], "interpolation": ...}``。
    baseline_value: Any = None

    @property
    def shared(self) -> bool:
        return bool(self.shared_group) and len(self.shared_users) > 1

    @property
    def in_group_shared(self) -> bool:
        """该参数是否位于**任何**节点组内部（不论该组是否共享）。

        共享组参数需要先确认影响范围；非共享组内参数同样默认只读（组内写入
        需要穿透语义确认），但拦截理由不同，因此单独暴露这个判定。
        """
        return bool(self.shared_group)


@dataclass
class GraphParamIndex:
    """一次基线的材质图参数索引。"""

    structure_hash: str
    params: dict[str, GraphParam] = field(default_factory=dict)

    def get(self, param_id: str) -> GraphParam | None:
        return self.params.get(param_id)


def build_param_index(
    material_groups: Iterable[Node], *, structure_hash: str
) -> GraphParamIndex:
    """从材质图 schema 节点树构造可写参数索引。

    * 只收录 ``editable`` 且 ``supported`` 且带 binding 的节点；
    * ``param_id`` 由节点的对外 id 派生（与 schema 一致）；
    * 色带记录基线的色标数量，供「数量变化即结构改动」判定。
    """
    index = GraphParamIndex(structure_hash=structure_hash)
    for node in flatten(list(material_groups)).values():
        if isinstance(node, GroupNode):
            continue
        if node.binding is None or not node.editable or not node.supported:
            continue
        impact = node.impact or {}
        session_id = str(impact.get("session_id") or "")
        identity = ParamIdentity(
            material_id=str(impact.get("material_id") or ""),
            node_tree_id=str(impact.get("node_tree_id") or ""),
            node_id=session_id,
            socket="",
            field=node.binding.field,
            path_hash=str(impact.get("path_hash") or ""),
        )
        element_count = None
        baseline_value: Any = None
        if isinstance(node, RampNode):
            # 色带的基线是**复合值**（元素表 + 插值），与 ScalarNode/ColorNode 不同。
            element_count = node.element_count_baseline
            baseline_value = {
                "elements": node.element_values(),
                "interpolation": node.interpolation_baseline or node.interpolation,
            }
        elif isinstance(node, (ColorNode, VectorNode)):
            baseline_value = list(node.baseline) if node.baseline else None
        elif isinstance(node, ScalarNode):
            baseline_value = node.baseline
        elif isinstance(node, EnumNode):
            baseline_value = node.baseline
        index.params[node.id] = GraphParam(
            param_id=node.id,
            binding=node.binding,
            kind=node.kind,
            identity=identity,
            shared_group=str(impact.get("in_group") or ""),
            shared_users=tuple(impact.get("affected_materials") or ()),
            minimum=node.minimum,
            maximum=node.maximum,
            element_count=element_count,
            baseline_value=baseline_value,
        )
    return index


def baseline_values(index: GraphParamIndex) -> dict[str, Any]:
    """全部可写参数的基线取值（供「恢复基线」编译计划）。

    与既有 ``surface.draft.baseline_values`` 同一条纪律：**只收录可写参数** ——
    探测失败 / 降级只读的参数没有可信的当前取值，放进去只会多出一条来历不明的写入。
    """
    out: dict[str, Any] = {}
    for param_id, param in index.params.items():
        if param.baseline_value is None:
            continue
        # 共享参数恢复时同样需要确认集合：恢复基线是系统行为，不是用户修改，
        # 因此这里对共享组**不要求**用户确认（否则「点恢复基线」会被共享拦截）。
        out[param_id] = param.baseline_value
    return out


def baseline_ops(index: GraphParamIndex) -> list[PlanOp]:
    """恢复材质图基线的写入计划（与「应用完整草稿」走同一条编译路径）。"""
    if not index.params:
        return []
    confirmed = {
        param.shared_group for param in index.params.values() if param.shared_group
    }
    return validate_graph_draft(
        baseline_values(index), index, confirmed_shared=confirmed
    )


def _fail(code: str, message: str, **details: Any) -> errors.ToonTunerError:
    return errors.ToonTunerError(code, message, details=details or None)


def validate_graph_draft(
    draft: Any,
    index: GraphParamIndex,
    *,
    expected_structure_hash: str | None = None,
    confirmed_shared: Iterable[str] = (),
) -> list[PlanOp]:
    """校验材质图草稿并编译成写入计划。

    失败码（稳定）：
    * 未知参数 / 类型或范围错误 → ``PARAM_INVALID``；
    * 不可编辑 / 只读 → ``NOT_EDITABLE``；
    * 结构哈希不符 → ``STRUCTURE_CHANGED``；
    * 共享参数未确认 → ``SHARED_PARAMETER_CONFIRM_REQUIRED``。
    """
    if draft is None:
        draft = {}
    if not isinstance(draft, dict):
        raise _fail(errors.PARAM_INVALID, "material_draft 必须是对象。")

    if expected_structure_hash is not None and expected_structure_hash != index.structure_hash:
        raise _fail(
            errors.STRUCTURE_CHANGED,
            "结构哈希与当前基线不一致：草稿与确认令牌已作废，请刷新基线。",
            expected=expected_structure_hash,
            current=index.structure_hash,
        )

    unknown = sorted(set(draft) - set(index.params))
    if unknown:
        raise _fail(
            errors.PARAM_INVALID,
            f"存在不在当前材质图 schema 内的参数：{unknown}",
            unknown=unknown,
        )

    confirmed = set(confirmed_shared)
    ops: list[PlanOp] = []
    for param_id in sorted(draft):
        param = index.params[param_id]
        if param.shared and param.shared_group not in confirmed:
            raise _fail(
                errors.SHARED_PARAMETER_CONFIRM_REQUIRED,
                (
                    f"参数 {param_id} 属于共享节点组 {param.shared_group}，"
                    "修改前必须确认影响范围（会同时影响："
                    + "、".join(param.shared_users)
                    + "）。"
                ),
                parameter=param_id,
                shared_group=param.shared_group,
                affected_materials=list(param.shared_users),
            )
        ops.extend(_ops_for(param, draft[param_id], param_id))
    return ops


def _ops_for(param: GraphParam, raw: Any, param_id: str) -> list[PlanOp]:
    if param.kind == "ramp":
        return _ramp_ops(param, raw, param_id)
    value = _coerce(param, raw, param_id)
    return [PlanOp(param.binding, value, cost="L1", param_id=param_id)]


def _ramp_ops(param: GraphParam, raw: Any, param_id: str) -> list[PlanOp]:
    """色带：整体替换 + 单独写插值，两段一起进同一个计划（原子、一起回滚）。"""
    interpolation: Any = None
    if isinstance(raw, dict):
        interpolation = raw.get("interpolation")
        raw = raw.get("elements")
    from ..surface.executor import normalise_ramp_elements

    elements = normalise_ramp_elements(raw)
    if param.element_count is not None and len(elements) != param.element_count:
        raise _fail(
            errors.STRUCTURE_CHANGED,
            f"{param_id} 的色标数量与基线不一致（{len(elements)} != {param.element_count}）。"
            "色标增删属于结构变化，请刷新基线后再调整。",
            parameter=param_id,
            received=len(elements),
            baseline=param.element_count,
        )
    ops = [PlanOp(param.binding, elements, cost="L1", param_id=param_id)]
    if interpolation is not None:
        if not isinstance(interpolation, str):
            raise _fail(errors.PARAM_INVALID, f"{param_id} 的插值必须是字符串。")
        ops.append(
            PlanOp(
                Binding(
                    param.binding.object_type, param.binding.object_id, "interpolation"
                ),
                interpolation,
                cost="L1",
                param_id=f"{param_id}.interpolation",
            )
        )
    return ops


def _coerce(param: GraphParam, raw: Any, param_id: str) -> Any:
    if param.kind == "color":
        return _color(raw, param_id)
    if param.kind in ("float", "int"):
        return _scalar(param, raw, param_id)
    raise _fail(errors.PARAM_INVALID, f"{param_id} 的类型无法写入：{param.kind}")


def _color(raw: Any, param_id: str) -> list[float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise _fail(errors.PARAM_INVALID, f"{param_id} 必须是含 4 个分量的颜色。")
    out: list[float] = []
    for component in raw:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise _fail(errors.PARAM_INVALID, f"{param_id} 的颜色分量必须是数字。")
        value = float(component)
        if not math.isfinite(value):
            raise _fail(errors.PARAM_INVALID, f"{param_id} 的颜色分量必须是有限数值。")
        out.append(value)
    return out


def _scalar(param: GraphParam, raw: Any, param_id: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _fail(errors.PARAM_INVALID, f"{param_id} 必须是数字。")
    value = float(raw)
    if not math.isfinite(value):
        raise _fail(errors.PARAM_INVALID, f"{param_id} 必须是有限数值。")
    if param.minimum is not None and value < param.minimum:
        raise _fail(
            errors.PARAM_INVALID, f"{param_id} = {value} 低于下限 {param.minimum}。"
        )
    if param.maximum is not None and value > param.maximum:
        raise _fail(
            errors.PARAM_INVALID, f"{param_id} = {value} 高于上限 {param.maximum}。"
        )
    return value


__all__ = [
    "GraphParam",
    "GraphParamIndex",
    "build_param_index",
    "baseline_values",
    "baseline_ops",
    "validate_graph_draft",
]
