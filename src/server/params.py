"""MVP-02 参数白名单（L0「即时」档）。

设计原则
--------
* **服务端唯一权威**：前端只能提交 ``{param_id: value}``；Python 代码一律由本模块
  按白名单拼装，客户端永远无法注入任意代码。
* 每个参数都声明了**类型、取值范围、绑定位置**；越界/未知 id 一律拒绝，不做静默兜底。
* 绑定位置（``binding``）在服务端映射为固定的访问表达式，见 ``blender_ops``。

MVP-02 首批只开放「曝光」组（``color.*``）；辉光组在基线恢复经重复测试通过后再加入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 合成器在 Blender 5.x 是「场景级节点组」，而非旧版的 scene.node_tree
COMPOSITOR_GROUP_NAME = "AI_Compositor"
GLARE_NODE_NAME = "Autocel_Glow"

PARAM_SCHEMA_VERSION = "toon-exposure-surface/1"


@dataclass(frozen=True)
class ParamSpec:
    """单个可调参数的完整声明。"""

    id: str
    group: str
    label: str
    type: str  # "float" | "enum"
    target: str  # 人类可读的目标位置（用于界面提示与审计）
    binding: str  # 内部绑定键，见 blender_ops.build_set_code
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple[str, ...] = ()
    options_dynamic: bool = False  # 可选值需从 Blender 实时读取
    unit: str = ""
    note: str = ""

    def to_public(self, options: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "group": self.group,
            "label": self.label,
            "type": self.type,
            "target": self.target,
            "unit": self.unit,
            "note": self.note,
        }
        if self.type == "float":
            payload["minimum"] = self.minimum
            payload["maximum"] = self.maximum
            payload["step"] = self.step
        else:
            payload["options"] = list(options if options is not None else self.options)
            payload["options_dynamic"] = self.options_dynamic
        return payload


GROUP_EXPOSURE = "曝光"


EXPOSURE_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        id="color.exposure",
        group=GROUP_EXPOSURE,
        label="曝光 (EV)",
        type="float",
        target="scene.view_settings.exposure",
        binding="view.exposure",
        minimum=-10.0,
        maximum=10.0,
        step=0.02,
        note="整体亮度 EV 补偿，线性乘法；最适合做『整体亮度』滑杆。",
    ),
    ParamSpec(
        id="color.gamma",
        group=GROUP_EXPOSURE,
        label="Gamma",
        type="float",
        target="scene.view_settings.gamma",
        binding="view.gamma",
        minimum=0.1,
        maximum=5.0,
        step=0.01,
        note="中间调非线性调整；1.0 为不改变。",
    ),
    ParamSpec(
        id="color.view_transform",
        group=GROUP_EXPOSURE,
        label="视图变换",
        type="enum",
        target="scene.view_settings.view_transform",
        binding="view.view_transform",
        options_dynamic=True,
        # 回退值（正常情况下由 OCIO 配置实时给出）
        options=(
            "Standard",
            "Khronos PBR Neutral",
            "AgX",
            "Filmic",
            "Filmic Log",
            "False Color",
            "Raw",
        ),
        note="整体影调曲线。换 Standard 会立刻变亮但丢高光滚降，卡通风格会崩。",
    ),
    ParamSpec(
        id="color.look",
        group=GROUP_EXPOSURE,
        label="Look（对比度预设）",
        type="enum",
        target="scene.view_settings.look",
        binding="view.look",
        options_dynamic=True,
        # 回退值（正常情况下由 OCIO 配置实时给出）
        options=(
            "None",
            "AgX - Very High Contrast",
            "AgX - High Contrast",
            "AgX - Medium High Contrast",
            "AgX - Base Contrast",
            "AgX - Medium Low Contrast",
            "AgX - Low Contrast",
            "AgX - Very Low Contrast",
        ),
        note="对比度档位；降低会让二分色阶变柔，升高会让硬边更硬。",
    ),
)

# 全部参数（MVP-02 首批 = 曝光组）
ALL_PARAMS: tuple[ParamSpec, ...] = EXPOSURE_PARAMS

BY_ID: dict[str, ParamSpec] = {p.id: p for p in ALL_PARAMS}

#: 需要从 Blender 实时读取候选值的绑定
DYNAMIC_OPTION_BINDINGS: dict[str, tuple[str, ...]] = {
    "view.view_transform": (),
    "view.look": (),
}


def get(param_id: str) -> ParamSpec | None:
    return BY_ID.get(param_id)


def public_schema(options_by_binding: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """给前端的 schema：按组聚合，附带动态枚举值的当前候选项。"""
    options_by_binding = options_by_binding or {}
    groups: dict[str, list[dict[str, Any]]] = {}
    for spec in ALL_PARAMS:
        opts = None
        if spec.options_dynamic:
            opts = options_by_binding.get(spec.binding)
        groups.setdefault(spec.group, []).append(spec.to_public(opts))
    return {
        "schema": PARAM_SCHEMA_VERSION,
        "groups": [{"name": name, "params": params} for name, params in groups.items()],
    }
