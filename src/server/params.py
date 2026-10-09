"""MVP-02 参数白名单（L0「即时」档）。

设计原则
--------
* **服务端唯一权威**：前端只能提交 ``{param_id: value}``；Python 代码一律由本模块
  按白名单拼装，客户端永远无法注入任意代码。
* 每个参数都声明了**类型、取值范围、绑定位置**；越界/未知 id 一律拒绝，不做静默兜底。
* 绑定位置（``binding``）在服务端映射为固定的访问表达式，见 ``blender_ops``。

MVP-02 开放两组 L0「即时」参数：``color.*``（曝光）与 ``glow.*``（辉光）。
曝光组先单独通过验证，再加入辉光组（见提交历史）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 合成器在 Blender 5.x 是「场景级节点组」，而非旧版的 scene.node_tree
COMPOSITOR_GROUP_NAME = "AI_Compositor"
GLARE_NODE_NAME = "Autocel_Glow"

PARAM_SCHEMA_VERSION = "toon-l0-surface/1"


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
    #: 本参数的合法取值**依赖**另一个参数（例：look 依赖 view_transform）。
    #: 有依赖时必须走依赖枚举校验，不能只比对静态候选列表。
    depends_on: str | None = None
    unit: str = ""
    note: str = ""

    def to_public(self, options: list[Any] | None = None) -> dict[str, Any]:
        """输出给前端的 schema。

        枚举项一律是 ``{"value": <Blender 真实 identifier>, "label": <显示文本>}``：
        **写进 Blender 的只能是 value**（需求 1）。静态枚举的 label 与 value 相同。
        """
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
            raw = list(options) if options is not None else list(self.options)
            payload["options"] = [_as_option(item) for item in raw]
            payload["options_dynamic"] = self.options_dynamic
            payload["depends_on"] = self.depends_on
        return payload


def _as_option(item: Any) -> dict[str, str]:
    """把候选值统一成 ``{value, label}``。"""
    if isinstance(item, dict):
        value = str(item.get("value"))
        label = item.get("label")
        return {"value": value, "label": str(label) if label is not None else value}
    text = str(item)
    return {"value": text, "label": text}


GROUP_EXPOSURE = "曝光"
GROUP_GLOW = "辉光"


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
        #: look 是**依赖 view_transform 的枚举**：同一个字符串在 AgX 下合法，
        #: 换到 Standard 就会直接抛 enum not found。候选一律由 color_looks 的
        #: 能力探针按当前视图给出，这里只留一个**任何视图都合法**的兜底值。
        depends_on="color.view_transform",
        options=("None",),
        note=(
            "对比度档位；降低会让二分色阶变柔，升高会让硬边更硬。"
            "可选档位随「视图变换」变化 —— 切换视图变换后列表会立即刷新，"
            "旧档位若有等价项会自动迁移，否则回退 None。"
        ),
    ),
)

GLOW_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        id="glow.threshold",
        group=GROUP_GLOW,
        label="辉光阈值",
        type="float",
        target="AI_Compositor › Autocel_Glow › Threshold",
        binding="glare.Threshold",
        minimum=0.0,
        maximum=5.0,
        step=0.01,
        note="低于此亮度的像素不发辉光。调低 → 大面积泛白；调高 → 只有高光边缘发亮。",
    ),
    ParamSpec(
        id="glow.strength",
        group=GROUP_GLOW,
        label="辉光强度",
        type="float",
        target="AI_Compositor › Autocel_Glow › Strength",
        binding="glare.Strength",
        minimum=0.0,
        maximum=10.0,
        step=0.01,
        note="辉光整体叠加量，是过曝的主要来源之一。",
    ),
    ParamSpec(
        id="glow.size",
        group=GROUP_GLOW,
        label="辉光尺寸",
        type="float",
        target="AI_Compositor › Autocel_Glow › Size",
        binding="glare.Size",
        minimum=0.0,
        maximum=1.0,
        step=0.01,
        note="辉光扩散半径。调大 → 整幅蒙纱感；0.5 以上取值范围在 5.x 未系统标定。",
    ),
    ParamSpec(
        id="glow.type",
        group=GROUP_GLOW,
        label="辉光类型",
        type="enum",
        target="AI_Compositor › Autocel_Glow › Type",
        binding="glare.Type",
        options=("Bloom", "Ghosts", "Streaks", "FogGlow", "SimpleStar"),
        note="辉光算法。Bloom 为源工程所用，视作风格基线，一般不建议改。",
    ),
    ParamSpec(
        id="glow.quality",
        group=GROUP_GLOW,
        label="辉光质量",
        type="enum",
        target="AI_Compositor › Autocel_Glow › Quality",
        binding="glare.Quality",
        options=("Low", "Medium", "High"),
        note="影响辉光柔度与耗时；High 明显变慢。与渲染采样数无关。",
    ),
    ParamSpec(
        id="glow.smoothness",
        group=GROUP_GLOW,
        label="辉光平滑度",
        type="float",
        target="AI_Compositor › Autocel_Glow › Smoothness",
        binding="glare.Smoothness",
        minimum=0.0,
        maximum=1.0,
        step=0.01,
        note="阈值附近的过渡柔度。",
    ),
)

# 全部参数（MVP-02 = 曝光组 + 辉光组）
ALL_PARAMS: tuple[ParamSpec, ...] = EXPOSURE_PARAMS + GLOW_PARAMS

BY_ID: dict[str, ParamSpec] = {p.id: p for p in ALL_PARAMS}

#: 需要从 Blender 实时读取候选值的绑定
DYNAMIC_OPTION_BINDINGS: dict[str, tuple[str, ...]] = {
    "view.view_transform": (),
    "view.look": (),
}


def get(param_id: str) -> ParamSpec | None:
    return BY_ID.get(param_id)


def public_schema(options_by_binding: dict[str, list[Any]] | None = None) -> dict[str, Any]:
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
