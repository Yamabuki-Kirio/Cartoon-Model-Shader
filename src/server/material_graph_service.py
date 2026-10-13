"""材质图会话状态（PR #7）：由 describe 载荷构造材质图、schema 与可写参数索引。

与 ``surface_service.SurfaceService`` 同一条纪律：本层只持有**状态**，
纯计算都在 ``material_graph`` 包里。它把「只读探针载荷」缝成材质图参数面。

职责：
* ``capture(describe)``：为所有被对象引用的材质构造图 + schema + 参数索引；
* ``schema_public()``：对外递归 schema（含分区 / 影响路径 / 置信等级）；
* ``plan(draft, ...)``：把材质图草稿编译成写入计划（复用 ``execution``）。

**只读优先**：拿不到图 / 图不可用时如实报告 ``available=false``，绝不伪造。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from . import errors
from .material_graph import schema as schema_module
from .material_graph.execution import (
    GraphParamIndex,
    baseline_ops,
    build_param_index,
    validate_graph_draft,
)
from .material_graph.probe import MaterialGraph, build_material_graph

__all__ = ["MaterialGraphBaseline", "MaterialGraphService"]


@dataclass
class MaterialGraphBaseline:
    """一次基线采集的材质图状态。"""

    baseline_id: str
    structure_hash: str
    materials: list[MaterialGraph] = field(default_factory=list)
    schemas: dict[str, Any] = field(default_factory=dict)
    #: 各材质的内部节点树（供参数索引构建；不进对外响应）
    nodes: dict[str, list[Any]] = field(default_factory=dict)
    param_index: GraphParamIndex | None = None
    #: 恢复材质图基线的计划（与「应用完整草稿」同一条编译路径）。
    baseline_ops: list[Any] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        return {
            "available": True,
            "schema_version": schema_module.SCHEMA_VERSION,
            "material_graph_baseline_id": self.baseline_id,
            "structure_hash": self.structure_hash,
            "material_count": len(self.materials),
            "materials": [
                {
                    "material_id": item.material_id,
                    "name": item.name,
                    "users": list(item.users),
                    "available": item.available,
                    "error": item.error,
                    "shared_groups": dict(item.shared_groups),
                }
                for item in self.materials
            ],
        }


def _combine_hash(materials: list[MaterialGraph]) -> str:
    """合并各材质结构哈希（只含结构，不含数值）。"""
    blob = "|".join(f"{item.name}:{item.structure_hash}" for item in materials)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class MaterialGraphService:
    """材质图参数面会话状态（不持有 Blender 连接）。"""

    def __init__(self) -> None:
        self._baseline: MaterialGraphBaseline | None = None

    @property
    def baseline(self) -> MaterialGraphBaseline | None:
        return self._baseline

    def require(self) -> MaterialGraphBaseline:
        if self._baseline is None:
            raise errors.ToonTunerError(
                errors.NO_BASELINE,
                "尚未建立材质图基线。请先建立基线。",
            )
        return self._baseline

    def capture(self, describe: dict[str, Any], *, baseline_id: str = "") -> MaterialGraphBaseline:
        """从 ``surface_probe.describe_groups`` 输出为所有材质建图与 schema。"""
        materials: list[MaterialGraph] = []
        schemas: dict[str, Any] = {}
        nodes: dict[str, list[Any]] = {}
        for item in describe.get("materials") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            material = build_material_graph(describe, name, classification=None)
            materials.append(material)
            payload, internal = schema_module.build_parameter_schema_with_nodes(material)
            schemas[name] = payload
            nodes[name] = internal
        all_nodes = [node for group in nodes.values() for node in group]
        param_index = build_param_index(all_nodes, structure_hash=_combine_hash(materials))
        baseline = MaterialGraphBaseline(
            baseline_id=baseline_id,
            structure_hash=_combine_hash(materials),
            materials=materials,
            schemas=schemas,
            nodes=nodes,
            param_index=param_index,
            baseline_ops=baseline_ops(param_index),
        )
        self._baseline = baseline
        return baseline

    # -- 对外形状 ---------------------------------------------------------
    def schema_public(self) -> dict[str, Any]:
        baseline = self.require()
        return {
            "available": True,
            "schema_version": schema_module.SCHEMA_VERSION,
            "material_graph_baseline_id": baseline.baseline_id,
            "structure_hash": baseline.structure_hash,
            "materials": [baseline.schemas[item.name] for item in baseline.materials],
        }

    def material_schema(self, material_id: str) -> dict[str, Any]:
        baseline = self.require()
        for item in baseline.materials:
            if item.material_id == material_id or item.name == material_id:
                return baseline.schemas[item.name]
        raise errors.ToonTunerError(errors.PARAM_INVALID, f"找不到材质：{material_id}")

    def shared_group_users(self) -> dict[str, list[str]]:
        """共享节点组 -> 使用它的材质名（含自身）的汇总。"""
        out: dict[str, set[str]] = {}
        baseline = self.require()
        for item in baseline.materials:
            for group, users in item.shared_groups.items():
                out.setdefault(group, set()).update(users)
        return {key: sorted(value) for key, value in out.items()}

    # -- 计划 -------------------------------------------------------------
    def plan(
        self,
        draft: Any,
        *,
        expected_structure_hash: str | None = None,
        confirmed_shared: list[str] | None = None,
    ) -> list[Any]:
        baseline = self.require()
        assert baseline.param_index is not None
        return validate_graph_draft(
            draft,
            baseline.param_index,
            expected_structure_hash=expected_structure_hash,
            confirmed_shared=confirmed_shared or [],
        )
