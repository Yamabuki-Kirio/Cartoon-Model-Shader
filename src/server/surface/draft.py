"""完整草稿校验：**唯一入口**，预设 / 预览 / 保存三处共用。

为什么必须唯一
--------------
旧实现里预览有自己的 ``_validate_draft``，预设与保存复用它 —— 这是对的。
v4 参数面变大之后，如果每个参数族再各写一次校验，就会出现
「预览能过、保存却写坏工程」的两套判定。因此这里只做一件事：

    ``draft``（客户端提交的稀疏草稿） + ``schema``（基线测出的树） → ``PlanOp`` 列表

校验分层（顺序固定，先给可读错误，再谈能不能写）：

1. **白名单**：id 必须在这棵树的 ``flatten()`` 里，否则 ``PARAM_INVALID``；
2. **状态**：``supported=false`` → ``UNSUPPORTED_PARAM``；``editable=false`` → ``NOT_EDITABLE``；
3. **类型与值域**：按 ``kind`` 分派；复合参数整体校验；
4. **依赖**：``depends_on`` 的参数（如 look 依赖 view_transform）交给既有依赖枚举逻辑，
   这里只负责把 ``depends_on`` 带进 ``PlanOp``，让执行器排序。
"""

from __future__ import annotations

import math
from typing import Any

from .. import errors
from .binding import assert_no_client_path, is_asset_binding
from .executor import PlanOp, normalise_ramp_elements
from .schema import (
    COST_L1,
    KIND_BOOL,
    KIND_ENUM,
    KIND_FLOAT,
    KIND_INT,
    ColorNode,
    EnumNode,
    GroupNode,
    Node,
    RampNode,
    ScalarNode,
    VectorNode,
    flatten,
)


class DraftError(errors.ToonTunerError):
    """草稿校验失败（保留为独立类型，便于测试精确断言）。"""


def _fail(code: str, message: str, **details: Any) -> DraftError:
    return DraftError(code, message, details=details or None)


def validate_draft(
    draft: Any, nodes: list[Node], *, baseline_values: dict[str, Any] | None = None
) -> list[PlanOp]:
    """校验草稿并编译成写入计划。

    ``baseline_values`` 给出「未在草稿中出现的参数」的基线值，用于把**完整草稿**
    （基线 + 草稿）送进计划 —— 这是既有不变量：每次应用都是整份草稿，
    绝不在上一次结果上叠加。
    """
    if draft is None:
        draft = {}
    if not isinstance(draft, dict):
        raise _fail(errors.PARAM_INVALID, "draft 必须是对象。")

    index = flatten(nodes)
    baseline = baseline_values or {}

    unknown = sorted(set(draft) - set(index))
    if unknown:
        raise _fail(
            errors.PARAM_INVALID,
            f"存在不在当前基线 schema 内的参数：{unknown}",
            unknown=unknown,
        )

    # 完整草稿 = 基线 + 草稿（草稿覆盖基线）
    merged: dict[str, Any] = {param_id: baseline.get(param_id) for param_id in index}
    merged.update(draft)

    ops: list[PlanOp] = []
    for param_id, node in index.items():
        if isinstance(node, GroupNode):
            continue  # 分组自身不是可写参数
        if node.binding is None:
            # 只读展示节点（如 managed_mode）：草稿里出现即拒绝
            if param_id in draft:
                raise _fail(
                    errors.NOT_EDITABLE,
                    f"{param_id} 是只读展示项，不接受写入。",
                    parameter=param_id,
                    readonly_reason=node.readonly_reason,
                )
            continue

        if param_id not in draft:
            # 未出现在草稿里：若基线也没有值，就跳过（不伪造）
            if merged.get(param_id) is None:
                continue
        _assert_writable(node, param_id)

        raw = merged.get(param_id)
        if node.binding is not None and is_asset_binding(node.binding):
            # asset 参数：只接受服务端生成的资源 id，路径类输入在这里就被挡下
            assert_no_client_path(raw)
        value = _coerce(node, raw, param_id)
        ops.append(
            PlanOp(
                node.binding,
                value,
                cost=node.cost or COST_L1,
                param_id=param_id,
                depends_on=_dependencies(node, index),
            )
        )
    return ops


def _dependencies(node: Node, index: dict[str, Node]) -> list[str]:
    """显式依赖边。目前只有依赖枚举（look → view_transform），未来可加。"""
    depends_on = getattr(node, "depends_on", None)
    if isinstance(depends_on, str) and depends_on in index:
        return [depends_on]
    return []


def _assert_writable(node: Node, param_id: str) -> None:
    if not node.supported:
        raise _fail(
            errors.UNSUPPORTED_PARAM,
            f"{param_id} 在当前工程中探测不到（supported=false）：{node.reason or '无进一步说明'}",
            parameter=param_id,
            reason=node.reason,
        )
    if not node.editable:
        raise _fail(
            errors.NOT_EDITABLE,
            f"{param_id} 当前不可编辑（readonly_reason={node.readonly_reason}）。",
            parameter=param_id,
            readonly_reason=node.readonly_reason,
        )


def _coerce(node: Node, raw: Any, param_id: str) -> Any:
    if isinstance(node, RampNode):
        return _coerce_ramp(node, raw, param_id)
    if isinstance(node, ColorNode):
        return _coerce_color(raw, param_id, components=4)
    if isinstance(node, VectorNode):
        return _coerce_vector(node, raw, param_id)
    if isinstance(node, EnumNode):
        return _coerce_enum(node, raw, param_id)
    if isinstance(node, ScalarNode):
        return _coerce_scalar(node, raw, param_id)
    raise _fail(errors.PARAM_INVALID, f"{param_id} 的节点类型无法写入。")


def _coerce_scalar(node: ScalarNode, raw: Any, param_id: str) -> Any:
    if node.kind == KIND_BOOL:
        if not isinstance(raw, bool):
            raise _fail(errors.PARAM_INVALID, f"{param_id} 必须是布尔值。", parameter=param_id)
        return bool(raw)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _fail(errors.PARAM_INVALID, f"{param_id} 必须是数字。", parameter=param_id)
    value = float(raw)
    if not math.isfinite(value):
        raise _fail(errors.PARAM_INVALID, f"{param_id} 必须是有限数值。", parameter=param_id)
    if node.minimum is not None and value < node.minimum:
        raise _fail(
            errors.PARAM_INVALID,
            f"{param_id} = {value} 低于下限 {node.minimum}。",
            parameter=param_id,
        )
    if node.maximum is not None and value > node.maximum:
        raise _fail(
            errors.PARAM_INVALID,
            f"{param_id} = {value} 高于上限 {node.maximum}。",
            parameter=param_id,
        )
    return int(value) if node.kind == KIND_INT else value


def _coerce_enum(node: EnumNode, raw: Any, param_id: str) -> str:
    if not isinstance(raw, str):
        raise _fail(errors.PARAM_INVALID, f"{param_id} 必须是字符串。", parameter=param_id)
    if node.depends_on:
        # 依赖枚举（look）：合法集合由依赖参数决定，交给既有依赖校验逻辑
        return raw
    allowed = node.allowed_values
    if allowed and raw not in allowed:
        raise _fail(
            errors.PARAM_INVALID,
            f"{param_id} = {raw!r} 不在允许取值内：{allowed}",
            parameter=param_id,
            allowed=allowed,
        )
    return raw


def _coerce_color(raw: Any, param_id: str, *, components: int) -> list[float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != components:
        raise _fail(
            errors.PARAM_INVALID,
            f"{param_id} 必须是含 {components} 个分量的颜色。",
            parameter=param_id,
        )
    out: list[float] = []
    for component in raw:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise _fail(
                errors.PARAM_INVALID, f"{param_id} 的颜色分量必须是数字。", parameter=param_id
            )
        value = float(component)
        if not math.isfinite(value):
            raise _fail(
                errors.PARAM_INVALID, f"{param_id} 的颜色分量必须是有限数值。", parameter=param_id
            )
        out.append(value)
    return out


def _coerce_vector(node: VectorNode, raw: Any, param_id: str) -> list[float]:
    return _coerce_color(raw, param_id, components=len(node.axes))


def _coerce_ramp(node: RampNode, raw: Any, param_id: str) -> list[dict[str, Any]]:
    """复合色带：**整体校验**（长度、逐项位置与颜色），任一不合法即整份拒绝。"""
    if isinstance(raw, dict):
        # 兼容 {"elements": [...]} 形式：只取 elements，interpolation 是独立参数
        raw = raw.get("elements")
    elements = normalise_ramp_elements(raw)
    if node.element_count_baseline is not None and len(elements) != node.element_count_baseline:
        # 决策 3：数量变化属结构改动，本执行器不做结构改写
        raise _fail(
            errors.STRUCTURE_CHANGED,
            f"{param_id} 的色标数量与基线不一致（{len(elements)} != "
            f"{node.element_count_baseline}）。色标增删属于结构变化，"
            "请刷新基线后再调整。",
            parameter=param_id,
            received=len(elements),
            baseline=node.element_count_baseline,
        )
    return elements


def baseline_values(nodes: list[Node]) -> dict[str, Any]:
    """从 schema 节点提取基线值（供调用方构造完整草稿）。"""
    out: dict[str, Any] = {}
    for node in flatten(nodes).values():
        if isinstance(node, RampNode):
            out[node.id] = node.element_values()
        elif isinstance(node, EnumNode):
            out[node.id] = node.baseline
        elif isinstance(node, ColorNode):
            out[node.id] = list(node.baseline) if node.baseline else None
        elif isinstance(node, VectorNode):
            out[node.id] = list(node.baseline) if node.baseline else None
        elif isinstance(node, ScalarNode):
            out[node.id] = node.baseline
    return {key: value for key, value in out.items() if value is not None}


__all__ = ["DraftError", "baseline_values", "validate_draft"]
