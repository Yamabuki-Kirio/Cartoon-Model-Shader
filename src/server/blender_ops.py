"""由白名单生成 Blender 侧代码，并解析其结构化回读。

约束
----
* 本模块**只接受已通过 ``params`` 校验的取值**，不接受任何客户端传入的代码。
* 生成的所有字面量都经过 ``repr()`` / 类型强制，杜绝字符串拼接注入。
* 每次回读都以 ``__TOON_JSON__`` 前缀输出一行 JSON，便于稳定解析。
"""

from __future__ import annotations

import json
from typing import Any

from . import errors, params

JSON_MARKER = "__TOON_JSON__"

# 各绑定键 -> 生成赋值语句所用到的表达式
_VIEW_BINDINGS = {
    "view.exposure": "exposure",
    "view.gamma": "gamma",
    "view.view_transform": "view_transform",
    "view.look": "look",
}


def _float_literal(value: float) -> str:
    return repr(float(value))


def _str_literal(value: str) -> str:
    return repr(str(value))


# -- 只读 ---------------------------------------------------------------


def build_read_code() -> str:
    """读取曝光/辉光现值 + 动态枚举候选 + 渲染设置。

    枚举候选取值来源（按可靠性排序）：
    1. ``PyOpenColorIO`` 当前配置（Blender 自带）：``getViews(display)`` 给出的正是
       ``view_transform`` 的合法标识；``getLookNames()`` 给出 look 名称。
    2. ``bl_rna.enum_items``（在无 UI 上下文时通常只返回 ``NONE``，故仅作兜底）。
    3. ``params`` 中的静态回退列表（由服务端补齐）。
    """
    return f"""
import bpy, json

scene = bpy.context.scene
vs = scene.view_settings
ng = bpy.data.node_groups.get({params.COMPOSITOR_GROUP_NAME!r})
glare = ng.nodes.get({params.GLARE_NODE_NAME!r}) if ng is not None else None


def _ocio_options():
    try:
        import PyOpenColorIO as ocio

        cfg = ocio.GetCurrentConfig()
        display = scene.display_settings.display_device
        views = [str(v) for v in cfg.getViews(display)]
        looks = ["None"] + [str(v) for v in cfg.getLookNames()]
        return views, looks
    except Exception:
        return [], []


def _rna_options(prop_owner, prop_name):
    try:
        prop = prop_owner.bl_rna.properties[prop_name]
        return [i.identifier for i in prop.enum_items if i.identifier != "NONE"]
    except Exception:
        return []


def _dedupe(values):
    seen = set()
    out = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _glare_value(name):
    if glare is None:
        return None
    socket = glare.inputs.get(name)
    if socket is None:
        return None
    value = getattr(socket, "default_value", None)
    if isinstance(value, str):
        return value
    try:
        return list(value)
    except TypeError:
        return value


views, looks = _ocio_options()
if not views:
    views = _rna_options(vs, "view_transform")
if not looks:
    looks = _rna_options(vs, "look")

r = scene.render
payload = {{
    "blender": bpy.app.version_string,
    "view": {{
        "exposure": vs.exposure,
        "gamma": vs.gamma,
        "view_transform": vs.view_transform,
        "look": vs.look,
    }},
    "view_options": {{
        "view.view_transform": _dedupe(views),
        "view.look": _dedupe(looks),
    }},
    "glare_present": glare is not None,
    "glare": {{
        "Type": _glare_value("Type"),
        "Quality": _glare_value("Quality"),
        "Threshold": _glare_value("Threshold"),
        "Smoothness": _glare_value("Smoothness"),
        "Strength": _glare_value("Strength"),
        "Size": _glare_value("Size"),
    }},
    "render": {{
        "engine": r.engine,
        "resolution_x": r.resolution_x,
        "resolution_y": r.resolution_y,
        "resolution_percentage": r.resolution_percentage,
        "film_transparent": r.film_transparent,
    }},
}}
print({JSON_MARKER!r} + json.dumps(payload, ensure_ascii=False, default=str))
""".strip()


# -- 写入 ---------------------------------------------------------------


def _write_line(binding: str, value: Any) -> str:
    if binding in _VIEW_BINDINGS:
        attr = _VIEW_BINDINGS[binding]
        if binding in ("view.view_transform", "view.look"):
            return f"vs.{attr} = {_str_literal(value)}"
        return f"vs.{attr} = {_float_literal(value)}"

    if binding.startswith("glare."):
        socket = binding.split(".", 1)[1]
        if socket in ("Type", "Quality"):
            return f"_glare_set({socket!r}, {_str_literal(value)})"
        return f"_glare_set({socket!r}, {_float_literal(value)})"

    raise errors.ToonTunerError(errors.PARAM_INVALID, f"未知绑定：{binding}")


def build_set_code(values: dict[str, Any]) -> str:
    """按白名单生成「一次性应用完整草稿」的代码，并在末尾回读。"""
    lines = [
        "import bpy, json",
        "",
        "scene = bpy.context.scene",
        "vs = scene.view_settings",
        f"ng = bpy.data.node_groups.get({params.COMPOSITOR_GROUP_NAME!r})",
        f"glare = ng.nodes.get({params.GLARE_NODE_NAME!r}) if ng is not None else None",
        "",
        "",
        "def _glare_set(name, value):",
        "    if glare is None:",
        f"        raise RuntimeError('合成器节点组缺少 {params.GLARE_NODE_NAME}（辉光节点）')",
        "    socket = glare.inputs.get(name)",
        "    if socket is None:",
        "        raise RuntimeError('辉光节点缺少插座：' + str(name))",
        "    socket.default_value = value",
        "",
        "",
        "def _glare_value(name):",
        "    if glare is None:",
        "        return None",
        "    socket = glare.inputs.get(name)",
        "    if socket is None:",
        "        return None",
        "    value = getattr(socket, 'default_value', None)",
        "    if isinstance(value, str):",
        "        return value",
        "    try:",
        "        return list(value)",
        "    except TypeError:",
        "        return value",
        "",
    ]

    for param_id in sorted(values):
        spec = params.get(param_id)
        if spec is None:  # 理论上不会发生（已在 API 层校验）
            raise errors.ToonTunerError(errors.PARAM_INVALID, f"未知参数：{param_id}")
        lines.append(_write_line(spec.binding, values[param_id]))

    lines.append("")
    lines.append(_readback_body())
    return "\n".join(lines)


def _readback_body() -> str:
    return f"""
out = {{
    "view": {{
        "exposure": vs.exposure,
        "gamma": vs.gamma,
        "view_transform": vs.view_transform,
        "look": vs.look,
    }},
    "glare": {{
        "Threshold": _glare_value("Threshold"),
        "Strength": _glare_value("Strength"),
        "Size": _glare_value("Size"),
        "Type": _glare_value("Type"),
        "Quality": _glare_value("Quality"),
        "Smoothness": _glare_value("Smoothness"),
    }} if glare is not None else None,
}}
print({JSON_MARKER!r} + json.dumps(out, ensure_ascii=False, default=str))
""".strip()


# -- 渲染预览 -----------------------------------------------------------


def build_render_code(png_path: str, width: int, height: int, percentage: int) -> str:
    """降分辨率渲染单张 PNG，随后**必定**恢复原分辨率。"""
    return f"""
import bpy, json, os

scene = bpy.context.scene
r = scene.render
target = {png_path!r}
os.makedirs(os.path.dirname(target), exist_ok=True)

original = (r.resolution_x, r.resolution_y, r.resolution_percentage)
frame = scene.frame_current
rendered = False
error = None
try:
    r.resolution_x = {int(width)}
    r.resolution_y = {int(height)}
    r.resolution_percentage = {int(percentage)}
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    bpy.ops.render.render(write_still=False)
    image = bpy.data.images.get("Render Result")
    if image is None:
        raise RuntimeError("渲染结束后未找到 Render Result")
    image.save_render(filepath=target, scene=scene)
    rendered = os.path.isfile(target)
finally:
    r.resolution_x, r.resolution_y, r.resolution_percentage = original

out = {{
    "rendered": rendered,
    "path": target,
    "size_bytes": (os.path.getsize(target) if os.path.isfile(target) else 0),
    "render_resolution": [{int(width)}, {int(height)}, {int(percentage)}],
    "restored_resolution": [r.resolution_x, r.resolution_y, r.resolution_percentage],
    "frame": frame,
}}
print({JSON_MARKER!r} + json.dumps(out, ensure_ascii=False, default=str))
""".strip()


# -- 解析 ---------------------------------------------------------------


def extract_json(captured_stdout: str) -> dict[str, Any]:
    """从捕获的 stdout 中取出最后一行 ``__TOON_JSON__`` 载荷。"""
    payload: str | None = None
    for line in captured_stdout.splitlines():
        if line.startswith(JSON_MARKER):
            payload = line[len(JSON_MARKER):]
    if payload is None:
        raise errors.BlenderUnexpectedResponse(
            "探针输出中未找到结构化结果行。",
            details={"stdout_tail": captured_stdout[-500:]},
        )
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise errors.BlenderUnexpectedResponse(
            "探针输出的结构化结果不是合法 JSON。",
            details={"stdout_tail": captured_stdout[-500:]},
        ) from exc
    if not isinstance(parsed, dict):
        raise errors.BlenderUnexpectedResponse("探针输出的结构不是对象。")
    return parsed
