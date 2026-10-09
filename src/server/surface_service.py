"""v4 参数面状态：身份记录 + 结构指纹 + 递归 schema + 完整基线值。

它把「只读拓扑探针」（``surface_probe``）与「递归 schema / 适配器 / 执行器」
（``surface`` 包）缝在一起，成为 v4 预览与保存的**唯一权威状态**。

为什么单独一层
--------------
``surface`` 包里的模块是**纯函数式**的：给一份探测描述，产出节点树、计划或校验结论。
它们不持有状态，也不碰 Blender。会话状态（这次基线的身份记录、结构指纹、基线值）
集中在 ``SurfaceBaseline``，由本模块管理 —— 这样 ``session.PreviewService`` 只需在
合适的时机调用它，而不必自己拼装指纹与计划。

三层身份怎么落地（技术方案决策 1、2）
-------------------------------------
* **身份**：``object_type + source + name``，**不做指纹匹配**（两个对象可能结构完全相同）；
* **结构**：节点增删 / 连线数 / 色标数量 / 材质槽 —— 变化即 ``STRUCTURE_CHANGED``，
  草稿与保存确认令牌一并作废；
* **值**：普通取值（位置、颜色、强度）—— 只进 ``external_changes`` 报告，**不作废草稿**。
  这条是可用性的底线：用户在 Blender 里拖一下色标就让整份草稿失效，工具就没法用了。

未确认能力一律只读
------------------
Emission 强度这类「候选名 + 结构验证」命中的能力，在拿到真机
``GET /api/diagnostics/describe`` 输出之前不进写入路径（见 ``surface/cel.py``）。
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import errors, surface
from .surface import cel as cel_module
from .surface.identity import (
    ObjectRecord,
    Verdict,
    compare,
    records_from_describe,
    structure_hash,
    value_snapshot,
)

logger = logging.getLogger("toon_tuner")

__all__ = ["SurfaceBaseline", "SurfaceService", "plan_values", "verify_ops", "param_values"]


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class SurfaceBaseline:
    """一次基线采集的全部 v4 状态。"""

    baseline_id: str
    captured_at: str
    blender: str
    structure_hash: str
    records: dict[str, ObjectRecord]
    nodes: list[Any]
    values: dict[str, Any]
    values_snapshot: dict[str, Any]
    baseline_ops: list[Any]
    degraded: list[str] = field(default_factory=list)
    found_groups: int = 0
    declared_groups: int = 0
    compositor_group: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "available": True,
            "schema_version": surface.SCHEMA_VERSION,
            "baseline_id": self.baseline_id,
            "captured_at": self.captured_at,
            "blender": self.blender,
            "structure_hash": self.structure_hash,
            "compositor_group": self.compositor_group,
            "found_groups": self.found_groups,
            "declared_groups": self.declared_groups,
            # 声明了但探不到 ⇒ 明确列出，而不是假装支持
            "degraded": list(self.degraded),
            "identities": [record.identity.to_public() for record in self.records.values()],
            "values": dict(self.values),
        }


class SurfaceService:
    """v4 参数面会话状态（不持有 Blender 连接，调用方负责串行化）。"""

    def __init__(self) -> None:
        self._baseline: SurfaceBaseline | None = None
        #: 探针失败的原因（保留给响应，不静默降级成「没有 Cel 组」）
        self._error: dict[str, Any] | None = None

    # -- 状态 -------------------------------------------------------------
    @property
    def baseline(self) -> SurfaceBaseline | None:
        return self._baseline

    @property
    def error(self) -> dict[str, Any] | None:
        return self._error

    def reset_error(self) -> None:
        self._error = None

    def record_error(self, error: dict[str, Any]) -> None:
        """探针失败：清掉旧基线并留下原因。

        刻意**不**退化成「探不到所以全都 supported:false」：那会把一次真实的
        Blender 故障伪装成「你的工程里没有 Cel 组」，是最难排查的那种假象。
        """
        self._baseline = None
        self._error = error

    def require(self) -> SurfaceBaseline:
        if self._baseline is None:
            raise errors.ToonTunerError(
                errors.NO_BASELINE,
                "尚未建立 v4 参数面基线。请先建立基线（会顺带采集只读拓扑）。",
                details={"probe_error": self._error},
            )
        return self._baseline

    # -- 采集 -------------------------------------------------------------
    def capture(self, describe: dict[str, Any]) -> SurfaceBaseline:
        """从 ``surface_probe.describe_groups`` 的输出建立基线。"""
        records = records_from_describe(describe)
        nodes = cel_module.build_cel_groups(describe)
        values = surface.baseline_values(nodes)
        baseline = SurfaceBaseline(
            baseline_id=uuid.uuid4().hex[:12],
            captured_at=_now_iso(),
            blender=str(describe.get("blender_version") or ""),
            structure_hash=structure_hash(records),
            records=records,
            nodes=nodes,
            values=values,
            values_snapshot=value_snapshot(describe),
            # 「恢复 Cel 基线」与「应用完整草稿」用同一套编译路径，
            # 因此基线写入计划在采集时算一次即可（也保证两者形状一致）。
            baseline_ops=surface.validate_draft({}, nodes, baseline_values=values),
            degraded=list(describe.get("degraded") or []),
            found_groups=int(describe.get("found_groups") or 0),
            declared_groups=int(describe.get("declared_groups") or 0),
            compositor_group=str(describe.get("compositor_group") or ""),
        )
        self._baseline = baseline
        self._error = None
        return baseline

    # -- 对外形状 ---------------------------------------------------------
    def public(self) -> dict[str, Any]:
        if self._baseline is None:
            return {
                "available": False,
                "schema_version": surface.SCHEMA_VERSION,
                "error": self._error,
            }
        return self._baseline.to_public()

    def schema_public(self) -> dict[str, Any]:
        """递归 schema 的对外形状（值 / 基线 / 生效值三值并存，状态四件齐全）。"""
        baseline = self.require()
        tree = surface.public_tree(baseline.nodes)
        tree.update(
            {
                "surface_baseline_id": baseline.baseline_id,
                "structure_hash": baseline.structure_hash,
                "compositor_group": baseline.compositor_group,
                "degraded": list(baseline.degraded),
            }
        )
        return tree

    # -- 计划 -------------------------------------------------------------
    def plan(self, draft: Any) -> list[Any]:
        """把一份 v4 草稿编译成写入计划（完整草稿 = 基线 + 草稿）。"""
        baseline = self.require()
        return surface.validate_draft(draft, baseline.nodes, baseline_values=baseline.values)

    def baseline_ops(self) -> list[Any]:
        return list(self.require().baseline_ops)

    # -- 三层比对 ---------------------------------------------------------
    def check(self, describe: dict[str, Any]) -> Verdict:
        """把当前探测结果与基线做三层比对（身份 → 结构 → 值）。"""
        baseline = self.require()
        current_records = records_from_describe(describe)
        return compare(
            baseline.records,
            current_records,
            baseline_values=baseline.values_snapshot,
            current_values=value_snapshot(describe),
        )


# -- 计划级的回读校验（与 ``surface.executor`` 同键）-------------------------


def plan_values(ops: Any) -> dict[str, Any]:
    return surface.plan_values(ops)


def param_values(ops: Any) -> dict[str, Any]:
    return surface.param_values(ops)


def verify_ops(
    ops: Any, readback: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]]]:
    return surface.verify_ops(ops, readback)
