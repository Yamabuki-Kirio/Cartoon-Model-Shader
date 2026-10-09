"""三层指纹：身份 / 结构 / 值（技术方案决策 1、2、3）。

为什么必须分三层
----------------
只用一把指纹会有两个相反的失败模式：

* 指纹太细（含值）⇒ 用户在 Blender 里拖一下色标，工具就判定「基线失效」，
  草稿全废，工具直接不可用；
* 指纹太粗（只看名字）⇒ 色标从 3 个变 2 个也发现不了，写下去就是错的。

因此：

| 层 | 内容 | 变化后果 |
|---|---|---|
| 身份 | ``对象类型 + 来源 + datablock 名称`` | ``IDENTITY_MISSING``：对象被重命名/删除，草稿与令牌作废，要求刷新基线 |
| 结构 | 节点增删、连线数、色标数量、材质槽 | ``STRUCTURE_CHANGED``：草稿与令牌作废 |
| 值 | 普通取值 | 非致命：记为 ``external_changes`` 报告 |

**身份层不做指纹匹配**：两个对象可能结构完全相同，指纹会误匹配（决策 1）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

#: 进入结构签名的字段。**值不在此列。**
STRUCTURAL_FIELDS: tuple[str, ...] = (
    "node_count",
    "color_ramp_elements",
    "material_slots",
    "link_count",
)


@dataclass(frozen=True)
class ObjectIdentity:
    """会话身份：三段拼成一个稳定主键。

    刻意**不含指纹** —— 指纹只用来检测变化，不参与匹配。
    """

    object_type: str
    name: str
    source: str

    def key(self) -> str:
        return f"{self.object_type}:{self.source}/{self.name}"

    def to_public(self) -> dict[str, str]:
        return {"object_type": self.object_type, "name": self.name, "source": self.source}


@dataclass
class ObjectRecord:
    """基线里记录的单个受管对象：身份 + 结构签名。"""

    identity: ObjectIdentity
    structure: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.identity.key()

    def structure_signature(self) -> str:
        """只取结构字段做稳定序列化（值一律不参与）。"""
        subset = {name: self.structure.get(name) for name in STRUCTURAL_FIELDS}
        return json.dumps(subset, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def structure_hash(records: dict[str, ObjectRecord] | list[ObjectRecord]) -> str:
    """整份结构指纹：身份 → 结构签名 的稳定映射哈希。

    ``identity.key()`` 参与其中，所以重命名**同时**改变结构指纹 —— 这没关系，
    比对时身份层优先，重命名会被优先判为 ``IDENTITY_MISSING``（更精确的结论）。
    """
    items = records.values() if isinstance(records, dict) else records
    payload = {
        record.key: record.structure_signature()
        for record in sorted(items, key=lambda item: item.key)
    }
    return _hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def value_fingerprint(values: dict[str, Any]) -> str:
    """值层指纹（草稿图指纹）。用于确认令牌绑定与「草稿是否被改过」。"""
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash(canonical)


VERDICT_OK = "ok"
VERDICT_IDENTITY_MISSING = "identity_missing"
VERDICT_STRUCTURE_CHANGED = "structure_changed"
VERDICT_VALUE_CHANGED = "value_changed"


@dataclass
class Verdict:
    """三层比对结论。``kind`` 取严重度最高的那一层。"""

    kind: str
    identity_missing: list[str] = field(default_factory=list)
    identity_added: list[str] = field(default_factory=list)
    renamed: list[dict[str, str]] = field(default_factory=list)
    structure_changed: list[str] = field(default_factory=list)
    value_changed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def fatal(self) -> bool:
        """致命 = 草稿与确认令牌都要作废。值层变化不作废。"""
        return self.kind in (VERDICT_IDENTITY_MISSING, VERDICT_STRUCTURE_CHANGED)

    def to_public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "fatal": self.fatal,
            "identity_missing": list(self.identity_missing),
            "identity_added": list(self.identity_added),
            "renamed": list(self.renamed),
            "structure_changed": list(self.structure_changed),
            "value_changed": list(self.value_changed),
        }


def compare(
    baseline: dict[str, ObjectRecord],
    current: dict[str, ObjectRecord],
    *,
    baseline_values: dict[str, Any] | None = None,
    current_values: dict[str, Any] | None = None,
    tolerance: float = 1e-6,
) -> Verdict:
    """三层比对。

    判定优先级：身份缺失 → 结构变化 → 值变化 → 正常。
    身份层用 ``object_type + name + source`` 直接比对，**不做指纹匹配**。
    """
    missing = sorted(key for key in baseline if key not in current)
    added = sorted(key for key in current if key not in baseline)

    structure_changed: list[str] = []
    for key in sorted(set(baseline) & set(current)):
        if baseline[key].structure_signature() != current[key].structure_signature():
            structure_changed.append(key)

    value_changed = _diff_values(
        baseline_values or {}, current_values or {}, tolerance=tolerance
    )

    if missing:
        kind = VERDICT_IDENTITY_MISSING
    elif structure_changed:
        kind = VERDICT_STRUCTURE_CHANGED
    elif value_changed:
        kind = VERDICT_VALUE_CHANGED
    else:
        kind = VERDICT_OK

    return Verdict(
        kind=kind,
        identity_missing=missing,
        identity_added=added,
        structure_changed=structure_changed,
        value_changed=value_changed,
    )


def _diff_values(
    baseline: dict[str, Any], current: dict[str, Any], *, tolerance: float
) -> list[dict[str, Any]]:
    """值层差异。数值按容差比较，避免浮点噪声报成「外部改动」。"""
    out: list[dict[str, Any]] = []
    for key in sorted(set(baseline) | set(current)):
        want = baseline.get(key)
        got = current.get(key)
        if _close(want, got, tolerance=tolerance):
            continue
        out.append({"id": key, "baseline": want, "current": got})
    return out


def _close(left: Any, right: Any, *, tolerance: float) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= tolerance
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(_close(a, b, tolerance=tolerance) for a, b in zip(left, right))
    return left == right


def records_from_describe(describe: dict[str, Any]) -> dict[str, ObjectRecord]:
    """把 ``surface_probe.describe_groups`` 的输出转成受管对象记录。

    ``source`` 用 ``AI_Compositor``（受管节点组的宿主），因此身份三段齐全。
    """
    records: dict[str, ObjectRecord] = {}
    for group in describe.get("groups") or []:
        if not isinstance(group, dict) or not group.get("exists"):
            continue
        name = str(group.get("name") or "")
        if not name:
            continue
        identity = ObjectIdentity(
            object_type="NODE_GROUP", name=name, source=str(describe.get("compositor_group") or "")
        )
        ramp_counts = list(group.get("ramp_element_counts") or [])
        record = ObjectRecord(
            identity=identity,
            structure={
                "node_count": int(group.get("node_count") or 0),
                # 色标数量取第一个色带（当前管线每组一个 ColorRamp）；没有则 None
                "color_ramp_elements": int(ramp_counts[0]) if ramp_counts else None,
                "material_slots": group.get("material_count"),
                "link_count": None,
            },
        )
        records[record.key] = record
    return records


def value_snapshot(describe: dict[str, Any]) -> dict[str, Any]:
    """值层快照：受管对象的**普通取值**，扁平成「可比较、可展示」的键。

    刻意**不含**节点增删与色标数量 —— 那两个属结构层（``STRUCTURAL_FIELDS``）。
    扁平键形如 ``NODE_GROUP:AI_Compositor/Cel_Skin.ramp[0].position``：

    * 数值可以按容差比较（``compare`` 的 ``_close``），不会把浮点噪声报成外部改动；
    * 差异项能直接展示成「哪个对象的哪个值被谁改了」，不需要前端再解析嵌套结构。

    外部改动（用户在 Blender 里拖了色标）**只报告、不作废草稿**：否则工具会变得
    完全不可用 —— 见本模块开头的三层设计说明。
    """
    out: dict[str, Any] = {}
    source = str(describe.get("compositor_group") or "")
    for group in describe.get("groups") or []:
        if not isinstance(group, dict) or not group.get("exists"):
            continue
        name = str(group.get("name") or "")
        if not name:
            continue
        key = ObjectIdentity(object_type="NODE_GROUP", name=name, source=source).key()
        for node in group.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_name = str(node.get("name") or "")
            out[f"{key}.node[{node_name}].mute"] = bool(node.get("mute"))
            ramp = node.get("color_ramp")
            if isinstance(ramp, dict):
                out[f"{key}.ramp.interpolation"] = ramp.get("interpolation")
                for index, element in enumerate(ramp.get("elements") or []):
                    if not isinstance(element, dict):
                        continue
                    out[f"{key}.ramp[{index}].position"] = element.get("position")
                    color = element.get("color")
                    if isinstance(color, (list, tuple)):
                        for channel, component in enumerate(color):
                            out[f"{key}.ramp[{index}].color.{channel}"] = component
            for socket in node.get("inputs") or []:
                if not isinstance(socket, dict) or socket.get("linked"):
                    continue
                prefix = f"{key}.node[{node_name}].input[{socket.get('name')}]"
                value = socket.get("value")
                if isinstance(value, (list, tuple)):
                    for channel, component in enumerate(value):
                        out[f"{prefix}.{channel}"] = component
                else:
                    out[prefix] = value
    return out
