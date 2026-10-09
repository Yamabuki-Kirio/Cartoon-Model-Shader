"""通用执行器：把「完整草稿」编译成**固定模板**的 Blender 代码。

与旧 ``blender_ops`` 的区别
---------------------------
旧实现按 binding 的**字符串前缀**静态分发（``_stage_of`` / ``_write_line``），
参数一多就退化成一张巨大的 if-else。这里改成数据驱动：

* ``SETTERS``：``(object_type, field) -> (target_expr, setter_expr)``；
  两个表达式里的 ``{object_id}`` / ``{value}`` 槽位**只接受已经 repr 量化好的字面量**，
  绝不做字符串拼接，也不会二次 repr（这是本文件最容易写错的地方）；
* 阶段顺序固定 ``L0 → L1 → L2 → L3``，阶段内按 ``depends_on`` 拓扑排序
  （旧代码里「view_transform 必须先于 look」的硬规则变成一条显式依赖边）；
* 恢复逻辑由计划**推导**出显式的 if/elif 分支，不用 ``exec``、也不硬编码四个字段。

原子性
------
* 单一 ``ramp`` 参数整体替换：先构造完整目标列表并逐项校验，再一次赋值；
  任一色标不合法或数量不符 ⇒ 整份不写（数量变化属结构改动，本执行器不做结构改写）；
* 任何一步失败 ⇒ 回滚到本次应用前的快照，并在结构化输出里报告 ``failure``。

安全
----
所有进入模板的值都经 ``repr()`` 量化；binding 已过白名单校验（``binding.assert_allowed``）；
客户端**永远**不能提交 node 路径、socket 名或 Python。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .. import errors
from . import binding as binding_module
from .schema import COSTS

JSON_MARKER = "__TOON_JSON__"

#: 色带最小/最大色标数（Blender ColorRamp 最少 2 个）
RAMP_MIN_ELEMENTS = 2
RAMP_MAX_ELEMENTS = 32


class PlanOp:
    """一条写入操作。``binding`` 在构造时即通过白名单校验。"""

    __slots__ = ("binding", "value", "cost", "param_id", "depends_on")

    def __init__(
        self,
        binding: binding_module.Binding,
        value: Any,
        *,
        cost: str,
        param_id: str,
        depends_on: Iterable[str] = (),
    ) -> None:
        binding_module.assert_allowed(binding)
        if binding_module.is_readonly(binding):
            raise errors.ToonTunerError(
                errors.NOT_EDITABLE,
                f"{binding.key()} 是只读绑定，不能进入写入计划。",
                details={"binding": binding.to_public()},
            )
        if cost not in COSTS:
            raise errors.ToonTunerError(
                errors.SCHEMA_INVALID, f"未知重算层级：{cost!r}"
            )
        self.binding = binding
        self.value = value
        self.cost = cost
        self.param_id = param_id
        self.depends_on = tuple(depends_on)


#: ``(object_type, field) -> (target_expr, setter_expr)``。
#: ``{object_id}`` 与 ``{value}`` 都**只接受已量化的字面量**（见 ``_render``）。
SETTERS: dict[tuple[str, str], tuple[str, str]] = {
    ("VIEW_SETTINGS", "exposure"): ("_vs.exposure", "_vs.exposure = {value}"),
    ("VIEW_SETTINGS", "gamma"): ("_vs.gamma", "_vs.gamma = {value}"),
    ("VIEW_SETTINGS", "view_transform"): ("_vs.view_transform", "_vs.view_transform = {value}"),
    ("VIEW_SETTINGS", "look"): ("_vs.look", "_vs.look = {value}"),
    ("DISPLAY_SETTINGS", "display_device"): (
        "_display.display_device",
        "_display.display_device = {value}",
    ),
    ("RENDER", "engine"): ("_render.engine", "_render.engine = {value}"),
    ("RENDER", "resolution_x"): ("_render.resolution_x", "_render.resolution_x = {value}"),
    ("RENDER", "resolution_y"): ("_render.resolution_y", "_render.resolution_y = {value}"),
    ("RENDER", "resolution_percentage"): (
        "_render.resolution_percentage",
        "_render.resolution_percentage = {value}",
    ),
    ("RENDER", "film_transparent"): (
        "_render.film_transparent",
        "_render.film_transparent = {value}",
    ),
    ("IMAGE_SETTINGS", "file_format"): (
        "_image_settings.file_format",
        "_image_settings.file_format = {value}",
    ),
    ("IMAGE_SETTINGS", "color_mode"): (
        "_image_settings.color_mode",
        "_image_settings.color_mode = {value}",
    ),
    ("IMAGE_SETTINGS", "color_depth"): (
        "_image_settings.color_depth",
        "_image_settings.color_depth = {value}",
    ),
    ("NODE_GROUP", "mute"): (
        "_node_group({object_id}).mute",
        "_node_group({object_id}).mute = {value}",
    ),
    ("WORLD", "use_nodes"): ("_world().use_nodes", "_world().use_nodes = {value}"),
    ("WORLD", "color"): ("_world_color()", "_set_world_color({value})"),
    ("WORLD", "node.strength"): ("_world_strength()", "_set_world_strength({value})"),
    ("COLOR_RAMP", "elements"): (
        "_ramp_elements({object_id})",
        "_set_ramp_elements({object_id}, {value})",
    ),
    ("COLOR_RAMP", "interpolation"): (
        "_ramp_interpolation({object_id})",
        "_set_ramp_interpolation({object_id}, {value})",
    ),
    ("LIGHT", "energy"): (
        "_light({object_id}).data.energy",
        "_light({object_id}).data.energy = {value}",
    ),
    ("LIGHT", "color"): (
        "_light_color({object_id})",
        "_set_light_color({object_id}, {value})",
    ),
    ("LIGHT", "hide_render"): (
        "_light({object_id}).hide_render",
        "_light({object_id}).hide_render = {value}",
    ),
    ("LIGHT", "use_shadow"): (
        "_light({object_id}).data.use_shadow",
        "_light({object_id}).data.use_shadow = {value}",
    ),
    ("LIGHT", "matrix_world.translation"): (
        "_light({object_id}).matrix_world.translation",
        "_set_light_world_location({object_id}, {value})",
    ),
    ("CAMERA", "lens"): ("_camera().data.lens", "_camera().data.lens = {value}"),
    ("CAMERA", "sensor_fit"): ("_camera().data.sensor_fit", "_camera().data.sensor_fit = {value}"),
    ("CAMERA", "sensor_width"): ("_camera().data.sensor_width", "_camera().data.sensor_width = {value}"),
    ("CAMERA", "sensor_height"): (
        "_camera().data.sensor_height",
        "_camera().data.sensor_height = {value}",
    ),
    ("CAMERA", "shift_x"): ("_camera().data.shift_x", "_camera().data.shift_x = {value}"),
    ("CAMERA", "shift_y"): ("_camera().data.shift_y", "_camera().data.shift_y = {value}"),
    ("CAMERA", "type"): ("_camera().data.type", "_camera().data.type = {value}"),
    ("CAMERA", "ortho_scale"): ("_camera().data.ortho_scale", "_camera().data.ortho_scale = {value}"),
    ("IMAGE", "colorspace_settings.name"): (
        "_image({object_id}).colorspace_settings.name",
        "_image({object_id}).colorspace_settings.name = {value}",
    ),
}


def _prelude() -> str:
    """所有生成代码共用的工具函数（固定常量，不含任何客户端输入）。"""
    return '''
import bpy
import json

_scene = bpy.context.scene
_render = _scene.render
_vs = _scene.view_settings
_display = _scene.display_settings
_image_settings = _render.image_settings


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _node_group(name):
    ng = bpy.data.node_groups.get(name)
    if ng is None:
        raise RuntimeError("node group not found: " + str(name))
    return ng


def _ramp_ref(object_id):
    group_name, _, node_name = str(object_id).partition("/")
    ng = _node_group(group_name)
    node = ng.nodes.get(node_name)
    if node is None:
        raise RuntimeError("ramp node not found: " + str(node_name))
    ramp = getattr(node, "color_ramp", None)
    if ramp is None:
        raise RuntimeError("node has no color_ramp: " + str(node_name))
    return ng, node, ramp


def _ramp_elements(object_id):
    _ng, _node, ramp = _ramp_ref(object_id)
    out = []
    for element in list(ramp.elements):
        color = list(getattr(element, "color", []))
        out.append({"position": float(element.position),
                    "color": [float(c) for c in color]})
    return out


def _ramp_interpolation(object_id):
    _ng, _node, ramp = _ramp_ref(object_id)
    return str(getattr(ramp, "interpolation", ""))


def _set_ramp_interpolation(object_id, value):
    _ng, _node, ramp = _ramp_ref(object_id)
    ramp.interpolation = str(value)


def _set_ramp_elements(object_id, elements):
    """整体替换：先整理并校验全部目标值，再一次写入。

    任一元素不合法、或数量与现状不符（结构性改动）⇒ 抛出，整份不写。
    """
    _ng, _node, ramp = _ramp_ref(object_id)
    wanted = []
    for item in elements:
        position = float(item["position"])
        color = [float(c) for c in item["color"]]
        if len(color) != 4:
            raise RuntimeError("ramp color must have 4 components")
        if position < 0.0 or position > 1.0:
            raise RuntimeError("ramp position out of range: " + str(position))
        wanted.append((position, color))
    if len(wanted) < 2:
        raise RuntimeError("color ramp needs at least 2 elements")
    current = list(ramp.elements)
    if len(current) != len(wanted):
        raise RuntimeError(
            "color ramp element_count mismatch: %d != %d" % (len(current), len(wanted))
        )
    for element, (position, color) in zip(current, wanted):
        element.position = position
        element.color = color


def _world():
    world = _scene.world
    if world is None:
        raise RuntimeError("scene has no world")
    return world


def _world_color():
    return [float(c) for c in _world().color]


def _set_world_color(value):
    _world().color = [float(c) for c in value]


def _world_strength():
    world = _world()
    tree = getattr(world, "node_tree", None)
    if not world.use_nodes or tree is None:
        return None
    for node in tree.nodes:
        if node.type == "BACKGROUND":
            return float(node.inputs["Strength"].default_value)
    return None


def _set_world_strength(value):
    world = _world()
    tree = getattr(world, "node_tree", None)
    if not world.use_nodes or tree is None:
        raise RuntimeError("world has no nodes")
    for node in tree.nodes:
        if node.type == "BACKGROUND":
            node.inputs["Strength"].default_value = float(value)
            return
    raise RuntimeError("world has no background node")


def _light(name):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise RuntimeError("light not found: " + str(name))
    return obj


def _light_color(name):
    return [float(c) for c in _light(name).data.color]


def _set_light_color(name, value):
    _light(name).data.color = [float(c) for c in value]


def _set_light_world_location(name, value):
    """世界空间定位：只写平移分量前的坐标，不改旋转与父级关系。"""
    obj = _light(name)
    if getattr(obj, "animation_data", None) is not None:
        raise RuntimeError("light is animated; world location is read-only")
    if getattr(obj, "constraints", None):
        raise RuntimeError("light has constraints; world location is read-only")
    if getattr(obj, "parent", None) is not None:
        raise RuntimeError("light has a parent; world location is read-only")
    obj.location = [float(c) for c in value]


def _camera():
    cam = _scene.camera
    if cam is None:
        raise RuntimeError("scene has no active camera")
    return cam


def _image(name):
    img = bpy.data.images.get(name)
    if img is None:
        raise RuntimeError("image not found: " + str(name))
    return img
'''.strip()


# -- 计划编排 --------------------------------------------------------------


def sort_ops(ops: Iterable[PlanOp]) -> list[PlanOp]:
    """按 cost 分层；层内按依赖拓扑排序（稳定）。"""
    ordered: list[PlanOp] = []
    for cost in COSTS:
        ordered.extend(_topological([op for op in ops if op.cost == cost]))
    return ordered


def _topological(ops: list[PlanOp]) -> list[PlanOp]:
    """被依赖的先写。

    旧实现把「view_transform 必须先于 look」硬编码在阶段表里；这里它只是一条依赖边，
    因此将来新增带依赖的参数不需要再改执行顺序的逻辑。
    """
    remaining = list(ops)
    ordered: list[PlanOp] = []
    written: set[str] = set()
    while remaining:
        progressed = False
        remaining_ids = {op.param_id for op in remaining}
        for op in list(remaining):
            unmet = [dep for dep in op.depends_on if dep in remaining_ids]
            if unmet:
                continue
            ordered.append(op)
            written.add(op.param_id)
            remaining.remove(op)
            progressed = True
        if not progressed:
            # 依赖成环：保持原顺序写完，避免死循环（顺序问题会在回读校验里暴露）
            ordered.extend(remaining)
            break
    return ordered


# -- 代码生成 --------------------------------------------------------------


def _render(template: str, binding: binding_module.Binding, value_literal: str) -> str:
    """填充模板。``object_id`` 与 ``value`` 都已是**量化好的字面量**。"""
    return template.format(object_id=repr(str(binding.object_id)), value=value_literal)


def build_apply_code(ops: Iterable[PlanOp]) -> str:
    """生成「原子应用整份计划」的代码。

    * 应用前按计划推导出快照键并拍快照；
    * 任一操作失败 ⇒ ``_restore()`` 并在 ``failure`` 里报告阶段与原因；
    * 输出 ``applied`` / ``failure`` / ``snapshot`` / ``values``（逐项回读）。
    """
    ordered = sort_ops(ops)
    if not ordered:
        raise errors.ToonTunerError(errors.PARAM_INVALID, "写入计划为空。")

    lines: list[str] = [_prelude(), "", "_readers = {"]
    for op in ordered:
        target, _setter = SETTERS[(op.binding.object_type, op.binding.field)]
        lines.append(f"    {op.binding.key()!r}: lambda: {_render(target, op.binding, 'None')},")
    lines.append("}")
    lines.append("")
    # 快照键由计划推导，不再硬编码 view_transform/look/exposure/gamma
    keys = sorted({op.binding.key() for op in ordered})
    lines.append("_snapshot_keys = " + repr(keys))
    lines.append("_snapshot = {_key: _safe(_readers[_key]) for _key in _snapshot_keys}")
    lines.append("")
    lines.append("def _restore():")
    if not ordered:  # pragma: no cover - 上面已拒绝空计划
        lines.append("    return")
    for op in ordered:
        _target, setter = SETTERS[(op.binding.object_type, op.binding.field)]
        # 赋值语句不能放进 lambda，因此这里生成的是**带 try/except 的普通语句**
        lines.append(f"    _snapshot_value = _snapshot.get({op.binding.key()!r})")
        lines.append("    if _snapshot_value is not None:")
        lines.append("        try:")
        lines.append("            " + _render(setter, op.binding, "_snapshot_value"))
        lines.append("        except Exception:")
        lines.append("            pass")
    lines.append("")
    lines.append("_applied = False")
    lines.append("_failure = None")
    lines.append("_stage = 'init'")
    lines.append("try:")
    for op in ordered:
        _target, setter = SETTERS[(op.binding.object_type, op.binding.field)]
        literal = _literal(op.value, op.binding)
        lines.append(f"    _stage = {op.param_id!r}")
        lines.append("    " + _render(setter, op.binding, literal))
    lines.append("    _applied = True")
    lines.extend(
        [
            "except Exception as _exc:",
            "    _restore()",
            "    _failure = {",
            "        'kind': 'write_failed',",
            "        'stage': _stage,",
            "        'type': type(_exc).__name__,",
            "        'message': str(_exc),",
            "    }",
            "",
            "_values = {_key: _safe(_readers[_key]) for _key in _readers}",
            "_out = {",
            "    'applied': _applied,",
            "    'failure': _failure,",
            "    'snapshot': _snapshot,",
            "    'values': _values,",
            "}",
            f"print({JSON_MARKER!r} + json.dumps(_out, ensure_ascii=False, default=str))",
        ]
    )
    return "\n".join(lines)


def build_readback_code(ops: Iterable[PlanOp]) -> str:
    """只回读计划涉及的字段（不写任何东西）。"""
    ordered = sort_ops(ops)
    lines = [_prelude(), "", "_readers = {"]
    for op in ordered:
        target, _setter = SETTERS[(op.binding.object_type, op.binding.field)]
        lines.append(f"    {op.binding.key()!r}: lambda: {_render(target, op.binding, 'None')},")
    lines.append("}")
    lines.append("_values = {_key: _safe(_readers[_key], {'__error__': True}) for _key in _readers}")
    lines.append(f"print({JSON_MARKER!r} + json.dumps({{'values': _values}}, ensure_ascii=False, default=str))")
    return "\n".join(lines)


def _literal(value: Any, binding: binding_module.Binding) -> str:
    """值的字面量。**一律经 repr 量化**，绝不做字符串拼接。"""
    if binding_module.is_asset_binding(binding):
        binding_module.assert_no_client_path(value)
        return repr(str(value))

    if binding.object_type == "COLOR_RAMP" and binding.field == "elements":
        return repr(normalise_ramp_elements(value))

    if isinstance(value, bool):
        return repr(bool(value))
    if isinstance(value, int):
        return repr(int(value))
    if isinstance(value, float):
        return repr(float(value))
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return repr([_normalise_item(item) for item in value])
    raise errors.ToonTunerError(
        errors.PARAM_INVALID,
        f"不支持的取值类型：{type(value).__name__}",
        details={"binding": binding.to_public()},
    )


def _normalise_item(item: Any) -> Any:
    if isinstance(item, bool):
        return bool(item)
    if isinstance(item, int):
        return int(item)
    if isinstance(item, float):
        return float(item)
    if isinstance(item, str):
        return item
    if isinstance(item, (list, tuple)):
        return [_normalise_item(entry) for entry in item]
    raise errors.ToonTunerError(
        errors.PARAM_INVALID, f"复合取值里含不支持的类型：{type(item).__name__}"
    )


def normalise_ramp_elements(value: Any) -> list[dict[str, Any]]:
    """色带整体值：``[{"position": 0.0-1.0, "color": [r,g,b,a]}, ...]``。

    服务端先挡明显非法的输入；Blender 侧 ``_set_ramp_elements`` 再守一次最终形态
    （双保险：这里是「早点给出可读错误」，那里是「绝不写坏工程」）。
    """
    if not isinstance(value, (list, tuple)):
        raise errors.ToonTunerError(
            errors.PARAM_INVALID, "色带取值必须是色标列表（elements）。"
        )
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise errors.ToonTunerError(errors.PARAM_INVALID, "色带元素必须是对象。")
        position = item.get("position")
        color = item.get("color")
        if isinstance(position, bool) or not isinstance(position, (int, float)):
            raise errors.ToonTunerError(errors.PARAM_INVALID, "色标 position 必须是数字。")
        if float(position) < 0.0 or float(position) > 1.0:
            raise errors.ToonTunerError(
                errors.PARAM_INVALID, f"色标 position 越界：{position}（须在 0–1）。"
            )
        if not isinstance(color, (list, tuple)) or len(color) != 4:
            raise errors.ToonTunerError(errors.PARAM_INVALID, "色标 color 必须含 4 个分量。")
        out.append(
            {
                "position": float(position),
                "color": [float(component) for component in color],
            }
        )
    if len(out) < RAMP_MIN_ELEMENTS:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID, f"色带至少需要 {RAMP_MIN_ELEMENTS} 个色标。"
        )
    if len(out) > RAMP_MAX_ELEMENTS:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID, f"色带最多 {RAMP_MAX_ELEMENTS} 个色标。"
        )
    return out


def extract_json(captured_stdout: str) -> dict[str, Any]:
    """从捕获的 stdout 取最后一行结构化载荷。"""
    payload: str | None = None
    for line in captured_stdout.splitlines():
        if line.startswith(JSON_MARKER):
            payload = line[len(JSON_MARKER):]
    if payload is None:
        raise errors.BlenderUnexpectedResponse("执行器输出中未找到结构化结果行。")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise errors.BlenderUnexpectedResponse(
            "执行器输出的结构化结果不是合法 JSON。"
        ) from exc
    if not isinstance(parsed, dict):
        raise errors.BlenderUnexpectedResponse("执行器输出的结构不是对象。")
    return parsed
