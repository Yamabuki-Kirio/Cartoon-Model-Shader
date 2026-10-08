"""只读场景探针：固定的 Blender Python 模板 + 结果解析。

安全约束：
* 探针代码是后端内置常量，浏览器无法传入任何 Python。
* 探针只读取数据并打印带唯一标记的 JSON，不写任何 ``bpy`` 数据，
  不调用渲染 / 保存 / 导入 / 追加资源，也不改变活动对象或选择状态。
"""

from __future__ import annotations

import json
from typing import Any

from . import errors
from .blender_mcp import BlenderMCPClient
from .redact import redact

PROBE_MARKER = "__TOON_TUNER_SCENE_PROBE__"
PROBE_SCHEMA = "toon-tuner-scene-probe/1"
MAX_ROLE_CANDIDATES = 10

# 只读探针（在 Blender 主线程内执行）。仅读取，不赋值任何 bpy 数据。
PROBE_CODE = '''
import json as _json
import bpy as _bpy


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _visible(obj):
    value = _safe(obj.visible_get)
    if value is None:
        value = _safe(lambda: not obj.hide_viewport, True)
    return bool(value)


_scene = _bpy.context.scene
_objects = list(_scene.objects)
_file_path = _bpy.data.filepath or ""
_mesh_objects = [o for o in _objects if o.type == "MESH"]


def _candidate(obj):
    return {
        "name": obj.name,
        "polygons": len(obj.data.polygons),
        "material_slots": len(obj.material_slots),
        "visible": _visible(obj),
        "hide_render": bool(obj.hide_render),
    }


_candidates = [_candidate(o) for o in _mesh_objects if len(o.material_slots) > 0]
_candidates.sort(key=lambda item: (-item["polygons"], item["name"]))

_payload = {
    "protocol": "toon-tuner-scene-probe/1",
    "blender": {
        "version": _bpy.app.version_string,
        "file_path": _file_path,
        "is_saved": bool(_file_path) and not _bpy.data.is_dirty,
    },
    "scene": {
        "name": _scene.name,
        "render_engine": _safe(lambda: _scene.render.engine),
        "resolution": [
            _scene.render.resolution_x,
            _scene.render.resolution_y,
            _scene.render.resolution_percentage,
        ],
        "frame_current": _scene.frame_current,
        "camera": _safe(lambda: _scene.camera.name) if _scene.camera else None,
    },
    "objects": {
        "total": len(_objects),
        "mesh_count": len(_mesh_objects),
        "visible_mesh_count": len([o for o in _mesh_objects if _visible(o)]),
        "light_count": len([o for o in _objects if o.type == "LIGHT"]),
        "camera_count": len([o for o in _objects if o.type == "CAMERA"]),
    },
    "role_candidates": _candidates[:10],
}

print("__TOON_TUNER_SCENE_PROBE__" + _json.dumps(_payload, ensure_ascii=False))
'''


def parse_probe_output(stdout: str) -> dict[str, Any]:
    """从探针 stdout 中提取标记后的 JSON 负载。"""
    if not stdout or not stdout.strip():
        raise errors.BlenderUnexpectedResponse("只读探针没有任何输出。")

    index = stdout.find(PROBE_MARKER)
    if index < 0:
        raise errors.BlenderUnexpectedResponse(
            "探针输出里找不到约定标记，无法确认这是本工具的探针结果。",
            details={"stdout_head": redact(stdout[:400])},
        )

    tail = stdout[index + len(PROBE_MARKER):].strip()
    if not tail:
        raise errors.BlenderUnexpectedResponse("探针标记之后没有 JSON 内容。")

    payload_text = tail.splitlines()[0]
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise errors.BlenderUnexpectedResponse(
            "探针输出的 JSON 无法解析。",
            details={"reason": str(exc), "payload_head": redact(payload_text[:200])},
        ) from exc

    if not isinstance(payload, dict):
        raise errors.BlenderUnexpectedResponse(
            "探针输出的 JSON 不是对象。",
            details={"payload_type": type(payload).__name__},
        )
    return payload


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def normalize_probe(payload: dict[str, Any]) -> dict[str, Any]:
    """把探针负载规整成 API 契约（容忍字段缺失）。"""
    blender_raw = payload.get("blender") or {}
    scene_raw = payload.get("scene") or {}
    objects_raw = payload.get("objects") or {}

    resolution_raw = scene_raw.get("resolution") or []
    resolution: list[int] = [_as_int(v) for v in resolution_raw[:3]]

    candidates_raw = payload.get("role_candidates") or []
    candidates: list[dict[str, Any]] = []
    for item in candidates_raw:
        if not isinstance(item, dict):
            continue
        candidates.append(
            {
                "name": _as_str(item.get("name")),
                "polygons": _as_int(item.get("polygons")),
                "material_slots": _as_int(item.get("material_slots")),
                "visible": bool(item.get("visible")),
                "hide_render": bool(item.get("hide_render")),
            }
        )
    # 兜底：即使 Blender 侧排序异常，也在服务端再排一次并截断
    candidates.sort(key=lambda entry: (-entry["polygons"], entry["name"]))
    candidates = candidates[:MAX_ROLE_CANDIDATES]

    file_path = _as_str(blender_raw.get("file_path"))

    return {
        "protocol": _as_str(payload.get("protocol"), PROBE_SCHEMA),
        "blender": {
            "version": _as_str(blender_raw.get("version"), "unknown"),
            "file_name": _basename(file_path),
            "file_path": file_path,
            "is_saved": bool(blender_raw.get("is_saved")),
        },
        "scene": {
            "name": _as_str(scene_raw.get("name")),
            "render_engine": _as_str(scene_raw.get("render_engine"), "unknown"),
            "resolution": resolution,
            "frame_current": _as_int(scene_raw.get("frame_current")),
            "camera": scene_raw.get("camera") if isinstance(scene_raw.get("camera"), str) else None,
        },
        "objects": {
            "total": _as_int(objects_raw.get("total")),
            "mesh_count": _as_int(objects_raw.get("mesh_count")),
            "visible_mesh_count": _as_int(objects_raw.get("visible_mesh_count")),
            "light_count": _as_int(objects_raw.get("light_count")),
            "camera_count": _as_int(objects_raw.get("camera_count")),
        },
        "role_candidates": candidates,
    }


def _basename(path: str) -> str | None:
    if not path:
        return None
    # 同时兼容 Windows 与 POSIX 分隔符，不依赖本机 os
    for sep in ("\\", "/"):
        if sep in path:
            path = path.rsplit(sep, 1)[-1]
    return path or None


def collect_scene(client: BlenderMCPClient) -> dict[str, Any]:
    """执行只读探针并返回归一化后的场景摘要。"""
    stdout = client.execute_code(PROBE_CODE)
    payload = parse_probe_output(stdout)
    return normalize_probe(payload)
