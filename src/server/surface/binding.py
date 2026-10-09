"""结构化 binding 与**字段白名单**。

为什么要有白名单
----------------
v4 的 binding 是结构化的 ``{object_type, object_id, field}``。前两项来自基线探测结果，
而 ``field`` **永远不来自请求** —— 只能来自 schema 声明。白名单在这里做两件事：

1. **启动期自检**（``assert_whitelist_healthy``）：schema 若声明了一个白名单外的字段，
   在 ``create_app()`` 阶段就炸，而不是等用户点「应用」时才炸在 Blender 里；
2. **运行期把关**（``assert_allowed``）：即使有人绕过 schema 手工拼了一个 binding，
   也会在生成代码之前被拒绝。

这条防线替代了旧实现「按字符串前缀静态分发」的隐式保护：旧代码遇到未知 binding 会抛
``PARAM_INVALID``，但那只是因为 ``_stage_of`` 写死了几个前缀；换成数据驱动之后，
必须有一个**显式**的白名单来承担同样的职责。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .. import errors

#: ``object_id`` 的合法形态。
#:
#: 允许 ``/``：色带/节点组的 object_id 形如 ``Cel_Skin/ColorRamp``（「组内对象」），
#: 这个分隔符是**服务端从探测结果拼出来的**，不是客户端输入。
#: 真正要挡的是另外几类：反斜杠（文件路径）、引号（拼串注入）、控制字符、
#: 以及 ``..`` 这类上跳（避免 pathlib 语义把它当上级目录）。
_ID_ALLOWED = re.compile(r"^[^\x00-\x1f\x7f\\'\"]{1,200}$")
_ID_LEADING_TRAILING_SLASH = re.compile(r"^/|/$")


def _id_is_valid(object_id: str) -> bool:
    if not object_id:
        return False
    if not _ID_ALLOWED.match(object_id):
        return False
    if object_id.startswith("$"):
        return True  # 占位符（$scene / $compositor），无其它约束
    if _ID_LEADING_TRAILING_SLASH.search(object_id):
        return False
    if ".." in object_id:
        return False
    if "//" in object_id:
        return False
    return True


@dataclass(frozen=True)
class Binding:
    """结构化绑定：指明「写到哪个对象的哪个字段」。"""

    object_type: str
    object_id: str
    field: str

    def key(self) -> str:
        return f"{self.object_type}:{self.object_id}.{self.field}"

    def to_public(self) -> dict[str, str]:
        return {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "field": self.field,
        }


#: ``object_type -> 允许的 field``。**唯一权威**：schema 与执行器都只认这张表。
BINDING_WHITELIST: dict[str, frozenset[str]] = {
    "SCENE": frozenset({"frame_current"}),
    "VIEW_SETTINGS": frozenset({"exposure", "gamma", "view_transform", "look"}),
    "DISPLAY_SETTINGS": frozenset({"display_device"}),
    "RENDER": frozenset(
        {
            "engine",
            "resolution_x",
            "resolution_y",
            "resolution_percentage",
            "film_transparent",
        }
    ),
    "IMAGE_SETTINGS": frozenset({"file_format", "color_mode", "color_depth"}),
    "WORLD": frozenset({"color", "use_nodes", "node.strength"}),
    "NODE_GROUP": frozenset({"mute", "topology"}),
    "COLOR_RAMP": frozenset({"elements", "interpolation", "element_count"}),
    "LIGHT": frozenset(
        {
            "energy",
            "color",
            "type",
            "hide_render",
            "use_shadow",
            "matrix_world.translation",
            "matrix_world.rotation",
        }
    ),
    "CAMERA": frozenset(
        {
            "lens",
            "sensor_fit",
            "sensor_width",
            "sensor_height",
            "shift_x",
            "shift_y",
            "type",
            "ortho_scale",
        }
    ),
    "CAMERA_DOF": frozenset({"use_dof", "focus_distance", "aperture_fstop"}),
    "MATERIAL": frozenset(
        {
            "base_color",
            "emission_color",
            "emission_strength",
            "roughness",
            "metallic",
            "specular",
            "blend_method",
            "alpha_threshold",
            "cel_group",
            "cel_override",
        }
    ),
    "IMAGE": frozenset({"colorspace_settings.name"}),
    "ASSET": frozenset({"id"}),
}

#: 只读字段：允许出现在 schema 里（用于展示与结构判定），但不接受写入。
READONLY_FIELDS: frozenset[str] = frozenset(
    {"SCENE.frame_current", "NODE_GROUP.topology", "COLOR_RAMP.element_count"}
)


def binding_ref(binding: Binding) -> str:
    """给错误信息与覆盖断言用的点分引用。"""
    return f"{binding.object_type}.{binding.field}"


def is_readonly(binding: Binding) -> bool:
    return binding_ref(binding) in READONLY_FIELDS


def assert_allowed(binding: Binding) -> None:
    """运行期把关：字段必须在白名单内，``object_id`` 形态必须合法。"""
    fields = BINDING_WHITELIST.get(binding.object_type)
    if fields is None:
        raise errors.ToonTunerError(
            errors.INVALID_BINDING,
            f"未知的 binding.object_type：{binding.object_type!r}",
            details={"object_type": binding.object_type},
        )
    if binding.field not in fields:
        raise errors.ToonTunerError(
            errors.INVALID_BINDING,
            f"{binding.object_type} 不允许绑定字段 {binding.field!r}",
            details={
                "object_type": binding.object_type,
                "field": binding.field,
                "allowed": sorted(fields),
            },
        )
    if not _id_is_valid(binding.object_id):
        raise errors.ToonTunerError(
            errors.INVALID_BINDING,
            "binding.object_id 形态非法（不得含反斜杠、引号、控制字符或 .. 上跳）。",
            details={
                "object_type": binding.object_type,
                "field": binding.field,
                "object_id_tail": binding.object_id[-24:],
            },
        )


def assert_whitelist_healthy(declared: Iterable[Binding]) -> None:
    """启动期自检：所有声明出来的 binding 都必须合法，且只读字段不得被当作可写。

    在 ``create_app()`` 调用。这样「schema 写错字段名」会立刻在应用启动 / 测试收集阶段暴露。
    """
    problems: list[str] = []
    seen: set[str] = set()
    for binding in declared:
        try:
            assert_allowed(binding)
        except errors.ToonTunerError as exc:
            problems.append(f"{binding.key()}: {exc.message}")
            continue
        if binding.key() in seen:
            problems.append(f"{binding.key()}: 重复声明")
        seen.add(binding.key())
    if problems:
        raise errors.ToonTunerError(
            errors.INVALID_BINDING,
            "参数 schema 与 binding 白名单不一致：" + "；".join(problems[:8]),
            details={"problems": problems},
        )


def is_asset_binding(binding: Binding) -> bool:
    """``asset`` 类参数：只允许提交服务端生成的资源 id（技术方案决策 6）。"""
    return binding.object_type == "ASSET" and binding.field == "id"


def assert_no_client_path(value: Any) -> None:
    """``asset`` 的值守卫：拒绝任何看起来像路径的输入。

    前端只应提交服务端生成的 id。这里做**形态**判定（而非白名单查找）：
    真正的 id 校验由基线的资源表负责，这一步只是把明显的路径挡在最外层。
    """
    if not isinstance(value, str):
        raise errors.ToonTunerError(errors.ASSET_UNKNOWN, "asset 取值必须是字符串 id。")
    text = value.strip()
    if not text:
        raise errors.ToonTunerError(errors.ASSET_UNKNOWN, "asset 取值不能为空。")
    if any(sep in text for sep in ("/", "\\", ":")) or text.startswith("~"):
        raise errors.ToonTunerError(
            errors.ASSET_UNKNOWN,
            "asset 参数只接受服务端生成的资源 id，不接受文件路径。",
        )
