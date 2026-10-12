"""逐材质适配器：把材质描述转成可编辑参数（逐材质编辑竖切）。

与 ``cel`` 完全同一条规矩
------------------------
**探到什么就生成什么；无法唯一确定就降级只读，绝不猜。**

具体到材质：

* 只有「材质输出节点唯一 + 它的 ``Surface`` 恰好连到唯一一个来源节点」时，
  才认为找到了**这个材质的着色器**；否则整个材质降级为只读，并说明原因。
* 着色器上只有**候选插座名**（``surface_probe.MATERIAL_SOCKET_CANDIDATES``）
  里、且探针**真的探到**的插座才会生成控件；探不到就生成 ``supported: false``
  的只读占位，**绝不凭空造一个写下去没反应的控件**。
* 插座已被上游连线 ⇒ 写 ``default_value`` 不影响画面 ⇒ 只读并说明原因。

受管范围
--------
只有**被至少一个对象引用**的材质才进入参数面（``surface_probe`` 已经过滤）。
没挂到任何对象上的材质不属于本工具的受管范围，不做「按名字猜」的迁移。

分类与影响范围
--------------
分类走仓库内**唯一规则源**（``render_pipeline/material_rules.json`` +
``material_classifier.py``），本模块不复制任何规则。规则文件不可用时分类降级为
「未分类」，但**不影响**其余参数 —— 分类只是展示信息，不能拖垮基线。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from .binding import Binding
from .schema import (
    COST_L1,
    ColorNode,
    EnumNode,
    GroupNode,
    REASON_NOT_FOUND,
    REASON_STRUCTURAL,
    SOURCE_SCENE,
    SOURCE_UNSUPPORTED,
    ScalarNode,
    max_cost,
)

logger = logging.getLogger("toon_tuner")

GROUP = "material"

#: 插座名 -> ``(binding.field, 种类)``。
#: 字段必须命中 ``BINDING_WHITELIST["MATERIAL"]``；两个 Specular 候选映射到同一个字段，
#: 因此同一材质只会出现一个 ``specular`` 节点（谁先命中用谁）。
SOCKET_FIELDS: dict[str, tuple[str, str]] = {
    "Base Color": ("base_color", "color"),
    "Metallic": ("metallic", "float"),
    "Roughness": ("roughness", "float"),
    "Emission Color": ("emission_color", "color"),
    "Emission Strength": ("emission_strength", "float"),
    "Specular IOR Level": ("specular", "float"),
    "Specular": ("specular", "float"),
}

#: 字段的固定展示顺序与元信息（``field -> (kind, label, min, max, step)``）。
#: 顺序固定，是为了让「探到」与「探不到」两种情况下生成的节点集合一致 ——
#: 前端不需要为「这次有没有这个参数」写分支。
FIELD_META: tuple[tuple[str, str, str, float | None, float | None, float | None], ...] = (
    ("base_color", "color", "基础色", None, None, None),
    ("metallic", "float", "金属度", 0.0, 1.0, 0.01),
    ("roughness", "float", "粗糙度", 0.0, 1.0, 0.01),
    ("emission_color", "color", "自发光颜色", None, None, None),
    ("emission_strength", "float", "自发光强度", 0.0, 100.0, 0.05),
    ("specular", "float", "高光强度", 0.0, 1.0, 0.01),
)

#: 材质名里出现这些字符就无法安全地拼进 ``object_id``（分段靠 ``/``），
#: 该材质整体降级只读 —— 与其拼出一个会被解析错的身份，不如不做。
_FORBIDDEN_IN_NAME = ("/", "\\", "'", '"')


def _is_bindable(name: str) -> bool:
    return bool(name) and not any(char in name for char in _FORBIDDEN_IN_NAME)


def _prefix(name: str) -> str:
    return f"material.{name}"


def _placeholder(
    prefix: str,
    field: str,
    kind: str,
    label: str,
    *,
    reason: str,
    supported: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
    step: float | None = None,
) -> Any:
    """生成一个只读占位节点（探不到 / 无法唯一确定时用它，而不是猜一个可写控件）。"""
    common: dict[str, Any] = {
        "id": f"{prefix}.{field}",
        "kind": kind,
        "group": GROUP,
        "cost": COST_L1,
        "label": label,
        "supported": supported,
        "editable": False,
        "active": False,
        "readonly_reason": REASON_NOT_FOUND,
        "value_source": SOURCE_UNSUPPORTED,
        "reason": reason,
    }
    if kind == "color":
        return ColorNode(**common)
    return ScalarNode(**common, minimum=minimum, maximum=maximum, step=step)


def build_material_group(
    material: dict[str, Any], *, classification: dict[str, Any] | None = None
) -> GroupNode:
    """把单个材质描述转成 ``GroupNode``（分类 / 影响范围 + 已确认的安全参数）。"""
    name = str(material.get("name") or "")
    users = [str(item) for item in (material.get("users") or [])]
    shader = material.get("shader")
    if not isinstance(shader, dict):
        shader = None
    prefix = _prefix(name)
    children: list[Any] = []

    bindable = _is_bindable(name)
    shader_name = str((shader or {}).get("name") or "")
    if shader is not None and not _is_bindable(shader_name):
        shader = None

    # -- 分类（只读）------------------------------------------------------
    klass = (classification or {}).get("class")
    label = (classification or {}).get("label") or klass
    children.append(
        EnumNode(
            id=f"{prefix}.classification",
            kind="enum",
            group=GROUP,
            cost=COST_L1,
            label="分类",
            value=klass,
            baseline=klass,
            effective=klass,
            options=[{"value": str(klass), "label": str(label)}] if klass else [],
            editable=False,
            readonly_reason=REASON_STRUCTURAL,
            supported=klass is not None,
            value_source=SOURCE_SCENE if klass is not None else SOURCE_UNSUPPORTED,
            note="只读：由仓库内唯一规则源（material_rules.json）判定，本工具不复制规则。",
        )
    )

    # -- 目标 Cel 组（只读；这就是「影响范围」的那一半）--------------------
    target = (classification or {}).get("group")
    children.append(
        EnumNode(
            id=f"{prefix}.target_group",
            kind="enum",
            group=GROUP,
            cost=COST_L1,
            label="目标 Cel 组",
            value=target,
            baseline=target,
            effective=target,
            options=[{"value": str(target), "label": str(target)}] if target else [],
            editable=False,
            readonly_reason=REASON_STRUCTURAL,
            supported=target is not None,
            value_source=SOURCE_SCENE if target is not None else SOURCE_UNSUPPORTED,
            note="只读：该材质按分类规则落到的 Cel 节点组。",
        )
    )

    # -- 使用对象（只读）--------------------------------------------------
    children.append(
        ScalarNode(
            id=f"{prefix}.users.count",
            kind="int",
            group=GROUP,
            cost=COST_L1,
            label="使用对象数",
            value=len(users),
            baseline=len(users),
            effective=len(users),
            minimum=0,
            editable=False,
            readonly_reason=REASON_STRUCTURAL,
            value_source=SOURCE_SCENE,
            note="只读：有多少个对象把这个材质挂在材质槽上。",
        )
    )

    # -- 安全参数 ---------------------------------------------------------
    if not bindable:
        for field, kind, field_label, minimum, maximum, step in FIELD_META:
            children.append(
                _placeholder(
                    prefix,
                    field,
                    kind,
                    field_label,
                    reason=(
                        "材质名含路径分隔符或引号，无法安全地拼进参数身份；"
                        "该材质整体降级为只读。"
                    ),
                    minimum=minimum,
                    maximum=maximum,
                    step=step,
                )
            )
    elif shader is None:
        for field, kind, field_label, minimum, maximum, step in FIELD_META:
            children.append(
                _placeholder(
                    prefix,
                    field,
                    kind,
                    field_label,
                    reason=(
                        "材质输出无法唯一确定（输出节点不唯一，或 Surface 未连线），"
                        "无法确认该写哪个节点；该材质整体降级为只读。"
                    ),
                    minimum=minimum,
                    maximum=maximum,
                    step=step,
                )
            )
    else:
        sockets = {
            str(socket.get("name") or ""): socket
            for socket in (shader.get("sockets") or [])
            if isinstance(socket, dict)
        }
        emitted: set[str] = set()
        #: ``field -> 已生成节点``，用来按 FIELD_META 的固定顺序收尾
        for socket_name, (field, kind) in SOCKET_FIELDS.items():
            if field in emitted:
                continue
            socket = sockets.get(socket_name)
            if socket is None:
                continue
            emitted.add(field)
            meta = next(item for item in FIELD_META if item[0] == field)
            _f, _k, field_label, minimum, maximum, step = meta
            node_id = f"{prefix}.{field}"
            binding = Binding(
                "MATERIAL", f"{name}/{shader_name}/{socket_name}", field
            )
            value = socket.get("value")
            if kind == "color":
                rgba = _as_rgba(value)
                if bool(socket.get("linked")):
                    children.append(
                        ColorNode(
                            id=node_id,
                            kind="color",
                            group=GROUP,
                            cost=COST_L1,
                            label=field_label,
                            value=rgba,
                            baseline=rgba,
                            effective=rgba,
                            supported=True,
                            editable=False,
                            readonly_reason=REASON_STRUCTURAL,
                            value_source=SOURCE_SCENE,
                            note=(
                                f"该材质的 {socket_name} 插座已被上游连线，"
                                "写 default_value 不会影响画面，因此不可编辑。"
                            ),
                        )
                    )
                else:
                    children.append(
                        ColorNode(
                            id=node_id,
                            kind="color",
                            group=GROUP,
                            cost=COST_L1,
                            label=field_label,
                            binding=binding,
                            value=rgba,
                            baseline=rgba,
                            effective=rgba,
                            value_source=SOURCE_SCENE,
                            note=f"写入 {shader_name} 节点的 {socket_name} 插座。",
                        )
                    )
            else:
                scalar = _as_float(value)
                if bool(socket.get("linked")):
                    children.append(
                        ScalarNode(
                            id=node_id,
                            kind="float",
                            group=GROUP,
                            cost=COST_L1,
                            label=field_label,
                            value=scalar,
                            baseline=scalar,
                            effective=scalar,
                            minimum=minimum,
                            maximum=maximum,
                            step=step,
                            supported=True,
                            editable=False,
                            readonly_reason=REASON_STRUCTURAL,
                            value_source=SOURCE_SCENE,
                            note=(
                                f"该材质的 {socket_name} 插座已被上游连线，"
                                "写 default_value 不会影响画面，因此不可编辑。"
                            ),
                        )
                    )
                else:
                    children.append(
                        ScalarNode(
                            id=node_id,
                            kind="float",
                            group=GROUP,
                            cost=COST_L1,
                            label=field_label,
                            binding=binding,
                            value=scalar,
                            baseline=scalar,
                            effective=scalar,
                            minimum=minimum,
                            maximum=maximum,
                            step=step,
                            value_source=SOURCE_SCENE,
                            note=f"写入 {shader_name} 节点的 {socket_name} 插座。",
                        )
                    )
        # 探不到的候选参数：生成只读占位，保持节点集合稳定
        for field, kind, field_label, minimum, maximum, step in FIELD_META:
            if field in emitted:
                continue
            children.append(
                _placeholder(
                    prefix,
                    field,
                    kind,
                    field_label,
                    reason=f"该材质的着色器（{shader_name}）上没有找到对应的插座。",
                    minimum=minimum,
                    maximum=maximum,
                    step=step,
                )
            )

    return GroupNode(
        id=prefix,
        kind="group",
        group=GROUP,
        cost=max_cost(children) if children else COST_L1,
        label=name,
        supported=True,
        editable=True,
        active=True,
        value_source=SOURCE_SCENE,
        impact={
            "users": users,
            "slot_count": len(users),
            "classification": klass,
            "target_group": target,
            "node_count": int(material.get("node_count") or 0),
            "blend_method": str(material.get("blend_method") or ""),
        },
        children=children,
    )


def build_material_groups(
    describe: dict[str, Any], *, classifier: Callable[[str], dict[str, Any]] | None = None
) -> list[GroupNode]:
    """把整份材质描述转成材质分组节点列表。"""
    classify = classifier if classifier is not None else load_default_classifier()
    groups: list[GroupNode] = []
    for material in describe.get("materials") or []:
        if not isinstance(material, dict):
            continue
        name = str(material.get("name") or "")
        if not name:
            continue
        classification = None
        if classify is not None:
            try:
                classification = classify(name)
            except Exception:  # pragma: no cover - 分类是展示信息，绝不能拖垮基线
                logger.warning("材质分类失败：%s", name, exc_info=True)
                classification = None
        groups.append(build_material_group(material, classification=classification))
    return groups


# -- 分类器加载（跨目录复用仓库内唯一规则源）-------------------------------

_CLASSIFIER_LOADED = False
_CLASSIFIER: Callable[[str], dict[str, Any]] | None = None


def _classifier_path() -> Path | None:
    from ..config import REPO_ROOT

    path = Path(REPO_ROOT) / "render_pipeline" / "material_classifier.py"
    return path if path.is_file() else None


def load_default_classifier() -> Callable[[str], dict[str, Any]] | None:
    """惰性加载 ``render_pipeline/material_classifier.py``，返回 ``classify(name)``。

    为什么按**文件路径**加载而不是 ``import`` 包：``render_pipeline`` 是随仓库分发的
    独立组件（没有 ``__init__.py``），与 ``src/server`` 保持边界。分类规则在仓库里
    只有一份（``material_rules.json``），项目明确禁止复制规则，所以这里跨目录复用，
    但把它当作**可选**能力：拿不到就返回 ``None``，分类降级为「未分类」。
    """
    global _CLASSIFIER_LOADED, _CLASSIFIER
    if _CLASSIFIER_LOADED:
        return _CLASSIFIER
    _CLASSIFIER_LOADED = True
    try:
        path = _classifier_path()
        if path is None:
            return None
        spec = importlib.util.spec_from_file_location("toon_material_classifier", path)
        if spec is None or spec.loader is None:  # pragma: no cover - 环境异常
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("toon_material_classifier", module)
        spec.loader.exec_module(module)
        engine = module.MaterialClassifier()
        labels = dict(engine.rules.class_labels)

        def classify(name: str) -> dict[str, Any]:
            result = engine.classify(name)
            payload = result.as_dict()
            payload["label"] = labels.get(payload.get("class"))
            return payload

        _CLASSIFIER = classify
    except Exception:  # pragma: no cover - 规则文件缺失/损坏时不拖垮基线
        logger.warning("材质分类器不可用，分类将显示为「未分类」。", exc_info=True)
        _CLASSIFIER = None
    return _CLASSIFIER


def reset_classifier_cache() -> None:
    """测试用：清掉加载缓存。"""
    global _CLASSIFIER_LOADED, _CLASSIFIER
    _CLASSIFIER_LOADED = False
    _CLASSIFIER = None


def _as_rgba(raw: Any) -> list[float]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        return [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        return [float(raw[0]), float(raw[1]), float(raw[2]), 1.0]
    return [0.0, 0.0, 0.0, 1.0]


def _as_float(raw: Any) -> float:
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    return 0.0
