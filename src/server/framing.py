"""取景（framing）：构图诊断 + 「临时自动取景」代码生成。

背景（本次要修的问题）
----------------------
预览沿用当前 Blender 帧的当前相机，而该相机带动画、且带很大的 ``shift``
（实测 shift_x=0.40 / shift_y=-0.16）。结果角色落在画面外，看起来像「参数没生效」。

本模块做两件事：

1. **只读诊断**（``build_context_code``）：给出当前帧、当前相机、相机是否带动画、
   角色世界包围盒是否完整落在当前相机画面内。投影数学完全复刻 Blender 的
   ``bpy_extras.object_utils.world_to_camera_view`` 口径——以
   ``camera.data.view_frame()`` 为唯一真源（它已经把 lens / sensor_fit /
   shift_x / shift_y / 渲染纵横比全部算进去了），因此不需要自己猜 shift 的归一化方式。
   ⚠ 关键细节：``view_frame()`` 返回的矩形不在单位距离上（实测 z≈-2.85），
   必须先用该距离归一化，否则 shift 会被放大成「永远放不进画面」。

2. **临时自动取景**（``build_render_code`` 的 auto 分支）：按角色真实网格的世界包围盒，
   新建一台**独立临时预览相机**（shift 归零、朝向沿用原相机），算出刚好装下的机位距离，
   渲染后 **finally 恢复 ``scene.camera`` 并删除临时相机**。
   全程不修改原相机、不改帧、不保存工程。

安全约束与既有模块一致：生成的代码是服务端内置常量拼接，所有字面量经 ``repr`` 量化，
浏览器无法注入任何 Python。
"""

from __future__ import annotations

import json
import math
from typing import Any

from . import errors

# -- 取景方式 ---------------------------------------------------------------

MODE_CURRENT = "current_camera"
MODE_FULL = "auto_full_body"
MODE_UPPER = "auto_upper_body"
MODE_HEAD = "auto_headshot"

FRAMING_MODES: dict[str, str] = {
    MODE_CURRENT: "当前相机",
    MODE_FULL: "自动全身",
    MODE_UPPER: "自动半身",
    MODE_HEAD: "自动头像",
}

AUTO_MODES = (MODE_FULL, MODE_UPPER, MODE_HEAD)

DEFAULT_MODE = MODE_CURRENT
#: 安全边距：要求被取景区域最多占画面的 1/(1+margin)。0.15 = 四周各留约 15%。
DEFAULT_MARGIN = 0.15
MARGIN_MIN = 0.0
MARGIN_MAX = 0.40

#: 临时预览相机的对象名（固定名，便于异常退出后清理残留）
TEMP_CAMERA_NAME = "__TOON_TUNER_PREVIEW_CAM__"

#: 每个取景方式截取角色包围盒的哪一段：(高度占比, 水平半宽系数)
#:   全身 = 整体；半身 = 顶部 55%；头像 = 顶部 16%，水平收窄到 ±20%
REGION_RULES: dict[str, tuple[float, float]] = {
    MODE_FULL: (1.0, 1.0),
    MODE_UPPER: (0.55, 1.0),
    MODE_HEAD: (0.16, 0.40),
}

#: 判定「基线失效」的数值容差（相机位置/旋转、角色包围盒）
TRANSFORM_TOLERANCE = 1e-4
BOUNDS_TOLERANCE = 1e-3

SCHEMA = "toon-tuner-framing/1"

CONTEXT_MARKER = "__TOON_FRAMING_JSON__"


# =============================================================================
#  Blender 侧公共助手（生成代码的内嵌片段）
# =============================================================================

#: 角色筛选 / 矩阵 / 投影 的公共助手。上下文读取与渲染两段代码共用同一份，
#: 保证「基线记录的包围盒」与「自动取景算出来的包围盒」永远同源。
_HELPERS = r'''
# ---- 取景助手（服务端生成，客户端不可注入）--------------------------------
_STAGE_PATTERNS = (
    "outline", "shell", "outline_mesh", "edge_line", "描边", "轮廓",
    "rigid", "rigidbody", "physical", "物理", "刚体",
    "インジケータ", "インジケーター", "ガイド", "ダミー", "操作",
)
_MAX_CHARACTER_OBJECTS = 24


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


def _polygons(obj):
    return _safe(lambda: len(obj.data.polygons), 0)


def _mat44(obj):
    m = obj.matrix_world
    return [[float(m[i][j]) for j in range(4)] for i in range(4)]


def _xform(m, p):
    return (
        m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + m[0][3],
        m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + m[1][3],
        m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + m[2][3],
    )


def _cols(m):
    """取 4x4 的旋转部分，逐列归一化（去缩放），返回世界坐标下的三根本地轴。"""
    out = []
    for j in range(3):
        c = (m[0][j], m[1][j], m[2][j])
        n = math.sqrt(c[0] * c[0] + c[1] * c[1] + c[2] * c[2]) or 1.0
        out.append((c[0] / n, c[1] / n, c[2] / n))
    return out


def _cols_t(cols, v):
    """R^T @ v：世界向量 -> 相机本地坐标。"""
    return (
        cols[0][0] * v[0] + cols[0][1] * v[1] + cols[0][2] * v[2],
        cols[1][0] * v[0] + cols[1][1] * v[1] + cols[1][2] * v[2],
        cols[2][0] * v[0] + cols[2][1] * v[1] + cols[2][2] * v[2],
    )


def _is_stage_artifact(obj):
    """刚体代理 / 描边壳：按 rigid_body 与命名模式排除。"""
    if _safe(lambda: obj.rigid_body is not None, False):
        return True
    name = (obj.name or "").lower()
    for pat in _STAGE_PATTERNS:
        if pat in name:
            return True
    return False


def _armature_of(obj):
    for mod in _safe(lambda: list(obj.modifiers), []) or []:
        if getattr(mod, "type", "") == "ARMATURE" and getattr(mod, "object", None) is not None:
            return getattr(mod.object, "name", None)
    parent = obj.parent
    if parent is not None and getattr(parent, "type", "") == "ARMATURE":
        return getattr(parent, "name", None)
    return None


def _character_candidates(scene):
    """候选角色网格：可见参与渲染 + 有材质槽 + 不是刚体代理/描边壳。"""
    kept = []
    excluded = {"rigid_or_shell": 0, "hidden": 0, "no_material": 0}
    for obj in scene.objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        if _is_stage_artifact(obj):
            excluded["rigid_or_shell"] += 1
            continue
        if bool(getattr(obj, "hide_render", False)) or not _visible(obj):
            excluded["hidden"] += 1
            continue
        if _safe(lambda: len(obj.material_slots), 0) <= 0:
            excluded["no_material"] += 1
            continue
        kept.append(obj)
    return kept, excluded


def _union_bounds(objects, dg):
    """世界空间包围盒（用求值后的网格，含修改器；取包围盒 8 角）。"""
    mn = [None, None, None]
    mx = [None, None, None]
    for obj in objects:
        ev = obj.evaluated_get(dg) if dg is not None else obj
        m = _mat44(ev)
        for corner in ev.bound_box:
            w = _xform(m, (corner[0], corner[1], corner[2]))
            for i in range(3):
                if mn[i] is None or w[i] < mn[i]:
                    mn[i] = w[i]
                if mx[i] is None or w[i] > mx[i]:
                    mx[i] = w[i]
    if mn[0] is None:
        return None
    return {"min": mn, "max": mx}


def _character_report(scene, dg):
    """选定「主要角色」= 被骨架驱动、面数最多的那一组网格。"""
    candidates, excluded = _character_candidates(scene)
    groups = {}
    for obj in candidates:
        key = _armature_of(obj) or "（无骨架）"
        groups.setdefault(key, []).append(obj)

    summary = [
        {
            "key": key,
            "objects": [o.name for o in objs],
            "max_polygons": max([_polygons(o) for o in objs] or [0]),
            "total_polygons": sum([_polygons(o) for o in objs]),
        }
        for key, objs in groups.items()
    ]
    summary.sort(key=lambda item: (-item["max_polygons"], item["key"]))

    report = {
        "group": None,
        "groups": summary,
        "objects": [],
        "bounds": None,
        "excluded": excluded,
    }
    if not summary:
        return report

    chosen_key = summary[0]["key"]
    chosen = groups[chosen_key]
    bounds = _union_bounds(chosen, dg)
    objects = []
    for obj in sorted(chosen, key=lambda o: -_polygons(o))[:_MAX_CHARACTER_OBJECTS]:
        objects.append(
            {
                "name": obj.name,
                "polygons": _polygons(obj),
                "materials": _safe(lambda: len(obj.material_slots), 0),
                "armature": _armature_of(obj),
                "matrix_world": _mat44(obj),
                "bounds": _union_bounds([obj], dg),
            }
        )
    report.update({"group": chosen_key, "objects": objects, "bounds": bounds})
    return report


def _frame_rect(camera, scene):
    """相机取景矩形（归一化到单位距离）。

    以 ``view_frame()`` 为唯一真源：它已含 lens / sensor_fit / shift / 纵横比。
    注意其返回点不在单位距离上，必须除以其 z 距离后再使用。
    """
    corners = list(camera.data.view_frame(scene=scene))
    xs = [float(c[0]) for c in corners]
    ys = [float(c[1]) for c in corners]
    zs = [float(c[2]) for c in corners]
    ortho = (getattr(camera.data, "type", "PERSP") == "ORTHO")
    depth = 1.0
    if not ortho:
        depth = -max(zs) if zs else 1.0
        if depth <= 1e-9:
            depth = 1.0
    return {
        "half_w": ((max(xs) - min(xs)) / 2.0) / depth,
        "half_h": ((max(ys) - min(ys)) / 2.0) / depth,
        "cx": ((max(xs) + min(xs)) / 2.0) / depth,
        "cy": ((max(ys) + min(ys)) / 2.0) / depth,
        "ortho": ortho,
    }


def _project(rect, cols, cam_pos, point):
    """世界点 -> 归一化画面坐标 (u, v, depth)。u/v ∈ [-0.5, 0.5] 为画面内。"""
    local = _cols_t(cols, (point[0] - cam_pos[0], point[1] - cam_pos[1], point[2] - cam_pos[2]))
    if rect["ortho"]:
        return (
            (local[0] - rect["cx"]) / rect["half_w"],
            (local[1] - rect["cy"]) / rect["half_h"],
            local[2],
        )
    depth = -local[2]
    if depth <= 1e-9:
        return None
    return (
        (local[0] / depth - rect["cx"]) / rect["half_w"],
        (local[1] / depth - rect["cy"]) / rect["half_h"],
        depth,
    )


def _corners(bounds):
    mn, mx = bounds["min"], bounds["max"]
    return [
        (x, y, z)
        for x in (mn[0], mx[0])
        for y in (mn[1], mx[1])
        for z in (mn[2], mx[2])
    ]


def _fit_report(camera, target_bounds, full_bounds, scene, temporary, clip_start, resolution):
    """判断 target_bounds 是否完整落在 camera 画面内；同时给出整角色是否完整。"""
    if camera is None:
        return {
            "evaluated": True, "camera": None, "inside": None, "reason": "no_camera",
            "message": "当前场景没有活动相机。",
            "worst_ndc": None, "full_inside": None, "full_worst_ndc": None,
            "overflow": None, "bounds_ndc": None, "clipped_near": None,
            "temporary": bool(temporary), "resolution": list(resolution),
        }
    if not target_bounds:
        return {
            "evaluated": True, "camera": camera.name, "inside": None, "reason": "no_character",
            "message": "没有找到可用的角色网格（已排除刚体代理 / 描边壳 / 隐藏对象）。",
            "worst_ndc": None, "full_inside": None, "full_worst_ndc": None,
            "overflow": None, "bounds_ndc": None, "clipped_near": None,
            "temporary": bool(temporary), "resolution": list(resolution),
        }

    rect = _frame_rect(camera, scene)
    cols = _cols(_mat44(camera))
    pos = (_mat44(camera)[0][3], _mat44(camera)[1][3], _mat44(camera)[2][3])

    def _measure(bounds):
        us, vs, behind, near = [], [], False, False
        for point in _corners(bounds):
            got = _project(rect, cols, pos, point)
            if got is None:
                behind = True
                continue
            u, v, depth = got
            if clip_start is not None and depth < clip_start:
                near = True
            us.append(u)
            vs.append(v)
        if behind or not us:
            return {"usable": False, "behind": True, "near": near}
        min_u, max_u = min(us), max(us)
        min_v, max_v = min(vs), max(vs)
        worst = max(abs(min_u), abs(max_u), abs(min_v), abs(max_v))
        return {
            "usable": True,
            "behind": False,
            "near": near,
            "worst": worst,
            "bounds_ndc": {"u": [min_u, max_u], "v": [min_v, max_v]},
            "inside": worst <= 1.0 + 1e-6,
            # 超出画面边长的比例：>0 即出画（0.5 = 超出半条边长）
            "overflow": {
                "left": max(0.0, -min_u - 1.0),
                "right": max(0.0, max_u - 1.0),
                "bottom": max(0.0, -min_v - 1.0),
                "top": max(0.0, max_v - 1.0),
            },
        }

    target = _measure(target_bounds)
    full = _measure(full_bounds) if full_bounds else target

    if target.get("behind"):
        reason, message, inside = (
            "behind_camera",
            "有部分内容落在相机背后（近平面之前），画面里看不到。",
            False,
        )
    elif target.get("near"):
        reason, message, inside = (
            "clipped_near",
            "有内容比相机近裁剪面更近，会被裁掉。",
            False,
        )
    elif target.get("inside"):
        inside = True
        reason = "ok"
        message = "被取景目标完整落在画面内（留白约 %.1f%%。）" % ((1.0 / max(target["worst"], 1e-6) - 1.0) * 100.0)
    else:
        reason = "outside_frame"
        inside = False
        over = target["overflow"]
        parts = []
        for label, key in (("左", "left"), ("右", "right"), ("下", "bottom"), ("上", "top")):
            if over[key] > 0.001:
                parts.append("%s %.0f%%" % (label, over[key] * 100.0))
        message = "超出画面（按画面边长计）：" + "、".join(parts) + "。"

    return {
        "evaluated": True,
        "camera": camera.name,
        "camera_type": getattr(camera.data, "type", "PERSP"),
        "inside": inside,
        "reason": reason,
        "message": message,
        "worst_ndc": target.get("worst"),
        "bounds_ndc": target.get("bounds_ndc"),
        "overflow": target.get("overflow"),
        "clipped_near": bool(target.get("near")),
        "full_inside": full.get("inside") if full.get("usable") else False,
        "full_worst_ndc": full.get("worst"),
        "temporary": bool(temporary),
        "resolution": list(resolution),
        "margin_hint": None if inside else "建议切到「自动全身 / 自动半身 / 自动头像」重新取景。",
    }


def _region(bounds, mode):
    """按取景方式截取包围盒的一段，返回 (瞄准点, 需要装进画面的包围盒)。"""
    frac, width_frac = _REGIONS.get(mode, (1.0, 1.0))
    mn, mx = bounds["min"], bounds["max"]
    size = (mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2])
    center_x = (mn[0] + mx[0]) / 2.0
    center_y = (mn[1] + mx[1]) / 2.0
    region_h = size[2] * frac
    if region_h <= 1e-6:
        region_h = size[2]
    top_z = mx[2]
    target = (center_x, center_y, top_z - region_h / 2.0)
    half_x = size[0] / 2.0 * width_frac
    half_y = size[1] / 2.0
    half_z = region_h / 2.0
    probe = {
        "min": (center_x - half_x, center_y - half_y, target[2] - half_z),
        "max": (center_x + half_x, center_y + half_y, target[2] + half_z),
    }
    return target, probe


def _cols_from_matrix(m):
    return _cols(m)


def _matrix_from_cols(cols):
    return [
        [cols[0][0], cols[1][0], cols[2][0]],
        [cols[0][1], cols[1][1], cols[2][1]],
        [cols[0][2], cols[1][2], cols[2][2]],
    ]


def _euler_from_matrix(rot):
    """XYZ 欧拉角提取（R = Rz @ Ry @ Rx）。"""
    sy = -rot[2][0]
    sy = max(-1.0, min(1.0, sy))
    ry = math.asin(sy)
    if abs(sy) < 0.999999:
        rx = math.atan2(rot[2][1], rot[2][2])
        rz = math.atan2(rot[1][0], rot[0][0])
    else:
        rx = math.atan2(-rot[1][2], rot[1][1])
        rz = 0.0
    return (rx, ry, rz)


def _build_preview_camera(scene, source, bounds, mode, margin):
    """新建临时预览相机：shift 归零、朝向沿用原相机（无原相机则正面平视）。"""
    cam_data = bpy.data.cameras.new("__TEMP_CAM__")
    cam_obj = bpy.data.objects.new("__TEMP_CAM__", cam_data)
    scene.collection.objects.link(cam_obj)

    if source is not None and getattr(source, "type", "") == "CAMERA":
        src_data = source.data
        cam_data.lens = float(getattr(src_data, "lens", 50.0))
        cam_data.sensor_width = float(getattr(src_data, "sensor_width", 36.0))
        cam_data.sensor_height = float(getattr(src_data, "sensor_height", 24.0))
        cam_data.sensor_fit = getattr(src_data, "sensor_fit", "AUTO")
        cols = _cols_from_matrix(_mat44(source))
        euler = _euler_from_matrix(_matrix_from_cols(cols))
    else:
        cam_data.lens = 50.0
        cam_data.sensor_width = 36.0
        cam_data.sensor_height = 24.0
        # 正面平视：局部 -Z 指向世界 +Y，局部 +Y 指向世界 +Z
        cols = [(1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)]
        euler = (math.pi / 2.0, 0.0, 0.0)

    cam_data.shift_x = 0.0
    cam_data.shift_y = 0.0
    cam_data.clip_start = 0.01
    cam_data.clip_end = 100000.0
    cam_obj.rotation_mode = "XYZ"
    cam_obj.rotation_euler = euler

    forward = (-cols[2][0], -cols[2][1], -cols[2][2])
    target, probe = _region(bounds, mode)
    rect = _frame_rect(cam_obj, scene)

    # 解析解：对每个角点要求 |a|/(d-c) <= hw 且 |b|/(d-c) <= hh
    #   (a, b, c) = R^T @ (角点 - 瞄准点)，d 为机位到瞄准点沿朝向的距离
    half_w = rect["half_w"] / (1.0 + margin)
    half_h = rect["half_h"] / (1.0 + margin)
    distance = 0.05
    for point in _corners(probe):
        q = _cols_t(cols, (point[0] - target[0], point[1] - target[1], point[2] - target[2]))
        distance = max(distance, q[2] + abs(q[0]) / half_w, q[2] + abs(q[1]) / half_h)

    cam_obj.location = (
        target[0] - forward[0] * distance,
        target[1] - forward[1] * distance,
        target[2] - forward[2] * distance,
    )
    return cam_obj, distance, target
'''

#: 上面模板里的占位符替换（避免 f-string 与生成代码的大括号冲突）
_PRELUDE = r'''
import bpy
import json
import math
import os

scene = bpy.context.scene
render = scene.render
'''

_HELPERS_WITH_REGIONS = None


def _helpers() -> str:
    """把取景模式的区域规则注入助手代码（注入点用注释标记，避免手写片段失配）。"""
    global _HELPERS_WITH_REGIONS
    if _HELPERS_WITH_REGIONS is None:
        anchor = "_MAX_CHARACTER_OBJECTS = 24"
        if anchor not in _HELPERS:  # pragma: no cover - 防御性检查
            raise errors.ToonTunerError(
                errors.INTERNAL_ERROR, "取景助手模板缺少区域规则注入点。"
            )
        _HELPERS_WITH_REGIONS = _HELPERS.replace(
            anchor, anchor + "\n_REGIONS = " + repr(REGION_RULES)
        )
    return _HELPERS_WITH_REGIONS


# =============================================================================
#  只读：取景上下文
# =============================================================================


def build_context_code() -> str:
    """读取「当前帧 / 相机 / 相机动画 / 角色包围盒 / 是否完整入画」。只读，不写任何 bpy 数据。"""
    return (
        _PRELUDE
        + _helpers()
        + r'''

dg = bpy.context.evaluated_depsgraph_get()
_camera = scene.camera
_resolution = [render.resolution_x, render.resolution_y, render.resolution_percentage]


def _camera_anim(obj):
    if obj is None:
        return {"has_animation": False, "sources": [], "fcurves": 0, "nla_tracks": 0, "action": None}
    sources = []
    fcurves = 0
    action = None
    ad = _safe(lambda: obj.animation_data)
    if ad is not None:
        if getattr(ad, "action", None) is not None:
            sources.append("object.action")
            action = getattr(ad.action, "name", None)
            fcurves += _safe(lambda: len(ad.action.fcurves), 0)
        tracks = _safe(lambda: len(ad.nla_tracks), 0)
        if tracks:
            sources.append("object.nla")
    dad = _safe(lambda: obj.data.animation_data)
    if dad is not None:
        if getattr(dad, "action", None) is not None:
            sources.append("data.action")
            fcurves += _safe(lambda: len(dad.action.fcurves), 0)
        if _safe(lambda: len(dad.nla_tracks), 0):
            sources.append("data.nla")
    has_driver = False
    for holder in (ad, dad):
        if holder is None:
            continue
        drv = _safe(lambda: holder.drivers)
        if drv is not None and len(drv) > 0:
            has_driver = True
    if has_driver:
        sources.append("drivers")
    return {
        "has_animation": bool(sources),
        "sources": sources,
        "fcurves": fcurves,
        "nla_tracks": _safe(lambda: len(ad.nla_tracks), 0) if ad is not None else 0,
        "action": action,
    }


def _camera_transform(obj):
    m = _mat44(obj)
    return {
        "location": [m[0][3], m[1][3], m[2][3]],
        "rotation_mode": getattr(obj, "rotation_mode", "XYZ"),
        "rotation_euler": [float(v) for v in obj.rotation_euler],
        "scale": [float(v) for v in obj.scale],
        "matrix_world": m,
    }


_character = _character_report(scene, dg)
_bounds = _character["bounds"]
_fit = _fit_report(
    _camera, _bounds, _bounds, scene, False,
    getattr(_camera.data, "clip_start", None) if _camera is not None else None,
    _resolution,
)

_camera_info = None
if _camera is not None:
    data = _camera.data
    _camera_info = {
        "name": _camera.name,
        "type": getattr(data, "type", "PERSP"),
        "lens": float(getattr(data, "lens", 0.0)),
        "sensor_width": float(getattr(data, "sensor_width", 0.0)),
        "sensor_height": float(getattr(data, "sensor_height", 0.0)),
        "sensor_fit": getattr(data, "sensor_fit", "AUTO"),
        "shift_x": float(getattr(data, "shift_x", 0.0)),
        "shift_y": float(getattr(data, "shift_y", 0.0)),
        "clip_start": float(getattr(data, "clip_start", 0.0)),
        "clip_end": float(getattr(data, "clip_end", 0.0)),
        "transform": _camera_transform(_camera),
        "animation": _camera_anim(_camera),
    }

_payload = {
    "protocol": "toon-tuner-framing/1",
    "frame_current": scene.frame_current,
    "frame_start": getattr(scene, "frame_start", None),
    "frame_end": getattr(scene, "frame_end", None),
    "camera": (_camera.name if _camera is not None else None),
    "camera_info": _camera_info,
    "cameras": [
        {"name": o.name, "animation": _camera_anim(o)}
        for o in scene.objects
        if getattr(o, "type", "") == "CAMERA"
    ],
    "character": _character,
    "fit": _fit,
    "resolution": _resolution,
}
print("__TOON_FRAMING_JSON__" + json.dumps(_payload, ensure_ascii=False, default=str))
'''
    )


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float_list(value: Any, size: int | None = None) -> list[float] | None:
    if not isinstance(value, (list, tuple)):
        return None
    out: list[float] = []
    for item in value:
        number = _as_float(item)
        if number is None:
            return None
        out.append(number)
    if size is not None and len(out) != size:
        return None
    return out


def _as_matrix(value: Any) -> list[list[float]] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    rows = []
    for row in value:
        parsed = _as_float_list(row, 4)
        if parsed is None:
            return None
        rows.append(parsed)
    return rows


def _normalize_bounds(raw: Any) -> dict[str, list[float]] | None:
    if not isinstance(raw, dict):
        return None
    mn = _as_float_list(raw.get("min"), 3)
    mx = _as_float_list(raw.get("max"), 3)
    if mn is None or mx is None:
        return None
    return {"min": mn, "max": mx}


def normalize_fit(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    overflow = raw.get("overflow") if isinstance(raw.get("overflow"), dict) else None
    bounds_ndc = raw.get("bounds_ndc") if isinstance(raw.get("bounds_ndc"), dict) else None
    return {
        "evaluated": bool(raw.get("evaluated", True)),
        "camera": raw.get("camera") if isinstance(raw.get("camera"), str) else None,
        "camera_type": raw.get("camera_type"),
        "inside": raw.get("inside") if isinstance(raw.get("inside"), bool) else None,
        "reason": str(raw.get("reason") or "unknown"),
        "message": str(raw.get("message") or ""),
        "worst_ndc": _as_float(raw.get("worst_ndc")),
        "full_inside": raw.get("full_inside") if isinstance(raw.get("full_inside"), bool) else None,
        "full_worst_ndc": _as_float(raw.get("full_worst_ndc")),
        "clipped_near": bool(raw.get("clipped_near")),
        "temporary": bool(raw.get("temporary")),
        "resolution": _as_float_list(raw.get("resolution")) or [],
        "overflow": {
            key: _as_float((overflow or {}).get(key), 0.0)
            for key in ("left", "right", "top", "bottom")
        } if overflow else None,
        "bounds_ndc": {
            key: _as_float_list((bounds_ndc or {}).get(key), 2)
            for key in ("u", "v")
        } if bounds_ndc else None,
        "margin_hint": raw.get("margin_hint"),
    }


def normalize_character(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    objects = []
    for item in raw.get("objects") or []:
        if not isinstance(item, dict):
            continue
        objects.append(
            {
                "name": str(item.get("name") or ""),
                "polygons": _as_int(item.get("polygons"), 0),
                "materials": _as_int(item.get("materials"), 0),
                "armature": item.get("armature"),
                "matrix_world": _as_matrix(item.get("matrix_world")),
                "bounds": _normalize_bounds(item.get("bounds")),
            }
        )
    groups = []
    for item in raw.get("groups") or []:
        if not isinstance(item, dict):
            continue
        groups.append(
            {
                "key": str(item.get("key") or ""),
                "objects": [str(n) for n in (item.get("objects") or [])],
                "max_polygons": _as_int(item.get("max_polygons"), 0),
                "total_polygons": _as_int(item.get("total_polygons"), 0),
            }
        )
    excluded = raw.get("excluded") if isinstance(raw.get("excluded"), dict) else {}
    return {
        "group": raw.get("group"),
        "groups": groups,
        "objects": objects,
        "bounds": _normalize_bounds(raw.get("bounds")),
        "excluded": {
            key: _as_int(excluded.get(key), 0)
            for key in ("rigid_or_shell", "hidden", "no_material")
        },
    }


def _normalize_animation(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "has_animation": bool(raw.get("has_animation")),
        "sources": [str(s) for s in (raw.get("sources") or [])],
        "fcurves": _as_int(raw.get("fcurves"), 0),
        "nla_tracks": _as_int(raw.get("nla_tracks"), 0),
        "action": raw.get("action") if isinstance(raw.get("action"), str) else None,
    }


def _normalize_transform(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "location": _as_float_list(raw.get("location"), 3),
        "rotation_mode": str(raw.get("rotation_mode") or "XYZ"),
        "rotation_euler": _as_float_list(raw.get("rotation_euler"), 3),
        "scale": _as_float_list(raw.get("scale"), 3),
        "matrix_world": _as_matrix(raw.get("matrix_world")),
    }


def parse_context(stdout: str) -> dict[str, Any]:
    """从捕获输出里取出取景上下文（原始负载）。"""
    payload: str | None = None
    for line in stdout.splitlines():
        if line.startswith(CONTEXT_MARKER):
            payload = line[len(CONTEXT_MARKER):]
    if payload is None:
        raise errors.BlenderUnexpectedResponse(
            "取景探针输出中未找到结构化结果行。",
            details={"stdout_tail": stdout[-300:]},
        )
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise errors.BlenderUnexpectedResponse(
            "取景探针输出的结构化结果不是合法 JSON。",
            details={"stdout_tail": stdout[-300:]},
        ) from exc
    if not isinstance(parsed, dict):
        raise errors.BlenderUnexpectedResponse("取景探针输出的结构不是对象。")
    return parsed


def normalize_context(raw: dict[str, Any]) -> dict[str, Any]:
    """把原始负载规整成 API 契约（容忍字段缺失）。"""
    camera_info_raw = raw.get("camera_info") if isinstance(raw.get("camera_info"), dict) else None
    camera_info = None
    if camera_info_raw is not None:
        camera_info = {
            "name": str(camera_info_raw.get("name") or ""),
            "type": str(camera_info_raw.get("type") or "PERSP"),
            "lens": _as_float(camera_info_raw.get("lens"), 0.0),
            "sensor_width": _as_float(camera_info_raw.get("sensor_width"), 0.0),
            "sensor_height": _as_float(camera_info_raw.get("sensor_height"), 0.0),
            "sensor_fit": str(camera_info_raw.get("sensor_fit") or "AUTO"),
            "shift_x": _as_float(camera_info_raw.get("shift_x"), 0.0),
            "shift_y": _as_float(camera_info_raw.get("shift_y"), 0.0),
            "clip_start": _as_float(camera_info_raw.get("clip_start"), 0.0),
            "clip_end": _as_float(camera_info_raw.get("clip_end"), 0.0),
            "transform": _normalize_transform(camera_info_raw.get("transform")),
            "animation": _normalize_animation(camera_info_raw.get("animation")),
        }

    return {
        "protocol": str(raw.get("protocol") or SCHEMA),
        "frame_current": _as_int(raw.get("frame_current"), 0),
        "frame_start": _as_int(raw.get("frame_start")),
        "frame_end": _as_int(raw.get("frame_end")),
        "camera": raw.get("camera") if isinstance(raw.get("camera"), str) else None,
        "camera_info": camera_info,
        "cameras": [
            {"name": str(item.get("name") or ""), "animation": _normalize_animation(item.get("animation"))}
            for item in (raw.get("cameras") or [])
            if isinstance(item, dict)
        ],
        "character": normalize_character(raw.get("character")),
        "fit": normalize_fit(raw.get("fit")),
        "resolution": [_as_int(v, 0) for v in (raw.get("resolution") or [])],
    }


def collect_context(client) -> dict[str, Any]:
    """执行只读取景探针并返回归一化上下文。"""
    stdout = client.execute_code(build_context_code())
    return normalize_context(parse_context(stdout))


# =============================================================================
#  取景选项校验
# =============================================================================


def validate_options(raw: Any) -> dict[str, Any]:
    """校验前端传来的取景方式与安全边距；越界即拒绝，不做静默兜底。"""
    if raw is None:
        return {"mode": DEFAULT_MODE, "margin": DEFAULT_MARGIN}
    if not isinstance(raw, dict):
        raise errors.ToonTunerError(errors.PARAM_INVALID, "framing 必须是对象。")

    unknown = sorted(set(raw) - {"mode", "margin"})
    if unknown:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID, f"framing 中存在未知字段：{unknown}"
        )

    mode = raw.get("mode", DEFAULT_MODE)
    if not isinstance(mode, str) or mode not in FRAMING_MODES:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID,
            f"取景方式必须是 {sorted(FRAMING_MODES)} 之一，收到 {mode!r}。",
        )

    margin = raw.get("margin", DEFAULT_MARGIN)
    if isinstance(margin, bool) or not isinstance(margin, (int, float)):
        raise errors.ToonTunerError(errors.PARAM_INVALID, "安全边距必须是数字。")
    margin = float(margin)
    if not math.isfinite(margin):
        raise errors.ToonTunerError(errors.PARAM_INVALID, "安全边距必须是有限数值。")
    if margin < MARGIN_MIN or margin > MARGIN_MAX:
        raise errors.ToonTunerError(
            errors.PARAM_INVALID,
            f"安全边距需在 {MARGIN_MIN:g}–{MARGIN_MAX:g} 之间，收到 {margin:g}。",
        )
    return {"mode": mode, "margin": round(margin, 4)}


def describe_options(options: dict[str, Any]) -> dict[str, Any]:
    mode = options.get("mode", DEFAULT_MODE)
    return {
        "mode": mode,
        "mode_label": FRAMING_MODES.get(mode, mode),
        "margin": options.get("margin", DEFAULT_MARGIN),
        "uses_temporary_camera": mode in AUTO_MODES,
    }


# =============================================================================
#  基线失效比对
# =============================================================================


def _vec_diff(a: Any, b: Any, tolerance: float) -> float | None:
    left = _as_float_list(a)
    right = _as_float_list(b)
    if left is None or right is None or len(left) != len(right):
        return None
    return max(abs(x - y) for x, y in zip(left, right)) if left else 0.0


def baseline_snapshot(context: dict[str, Any]) -> dict[str, Any]:
    """从取景上下文里抽出「建立基线时必须记录」的部分。"""
    camera_info = context.get("camera_info") or {}
    character = context.get("character") or {}
    return {
        "schema": SCHEMA,
        "frame_current": context.get("frame_current"),
        "frame_start": context.get("frame_start"),
        "frame_end": context.get("frame_end"),
        "camera": context.get("camera"),
        "camera_type": camera_info.get("type"),
        "camera_transform": camera_info.get("transform"),
        "lens": camera_info.get("lens"),
        "shift_x": camera_info.get("shift_x"),
        "shift_y": camera_info.get("shift_y"),
        "sensor_width": camera_info.get("sensor_width"),
        "sensor_height": camera_info.get("sensor_height"),
        "sensor_fit": camera_info.get("sensor_fit"),
        "camera_animation": camera_info.get("animation"),
        "character_group": character.get("group"),
        "character_objects": [
            {
                "name": item.get("name"),
                "matrix_world": item.get("matrix_world"),
                "bounds": item.get("bounds"),
            }
            for item in (character.get("objects") or [])
        ],
        "character_bounds": character.get("bounds"),
        "resolution": context.get("resolution"),
        "fit": context.get("fit"),
    }


def compare_context(baseline: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """比对当前取景与基线：帧/相机被外部改动 => 失效（硬）；其余仅告警。"""
    reasons: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not isinstance(baseline, dict) or not baseline:
        return {"stale": False, "reasons": [], "warnings": [], "checked": False}

    base_frame = _as_int(baseline.get("frame_current"))
    now_frame = _as_int(current.get("frame_current"))
    if base_frame is not None and now_frame is not None and base_frame != now_frame:
        reasons.append(
            {
                "code": "frame_changed",
                "message": f"当前帧已从 {base_frame} 变为 {now_frame}。",
            }
        )

    base_camera = baseline.get("camera")
    now_camera = current.get("camera")
    if base_camera != now_camera:
        reasons.append(
            {
                "code": "camera_changed",
                "message": f"当前相机已从 {base_camera or '（无）'} 变为 {now_camera or '（无）'}。",
            }
        )

    camera_info = current.get("camera_info") or {}
    transform = camera_info.get("transform") or {}
    if base_camera == now_camera and base_camera is not None:
        delta = _vec_diff(
            (baseline.get("camera_transform") or {}).get("location"),
            transform.get("location"),
            TRANSFORM_TOLERANCE,
        )
        if delta is not None and delta > TRANSFORM_TOLERANCE:
            reasons.append(
                {
                    "code": "camera_moved",
                    "message": f"相机位置较基线移动了 {delta:.4f}（世界单位）。",
                }
            )
        delta_r = _vec_diff(
            (baseline.get("camera_transform") or {}).get("rotation_euler"),
            transform.get("rotation_euler"),
            TRANSFORM_TOLERANCE,
        )
        if delta_r is not None and delta_r > TRANSFORM_TOLERANCE:
            reasons.append(
                {
                    "code": "camera_rotated",
                    "message": f"相机朝向较基线旋转了 {delta_r:.4f} 弧度。",
                }
            )
        delta_lens = _vec_diff(
            [baseline.get("lens")], [camera_info.get("lens")], TRANSFORM_TOLERANCE
        )
        if delta_lens is not None and delta_lens > 1e-3:
            reasons.append(
                {
                    "code": "lens_changed",
                    "message": f"相机焦距已从 {baseline.get('lens')} 变为 {camera_info.get('lens')}。",
                }
            )
        delta_shift = _vec_diff(
            [baseline.get("shift_x"), baseline.get("shift_y")],
            [camera_info.get("shift_x"), camera_info.get("shift_y")],
            TRANSFORM_TOLERANCE,
        )
        if delta_shift is not None and delta_shift > 1e-3:
            reasons.append(
                {"code": "shift_changed", "message": "相机 shift_x / shift_y 较基线已变化。"}
            )

    base_bounds = baseline.get("character_bounds") or {}
    now_bounds = (current.get("character") or {}).get("bounds") or {}
    if base_bounds and now_bounds:
        tolerance = BOUNDS_TOLERANCE
        size = _as_float_list((_bounds_size(base_bounds)) or []) or [0.0]
        span = max(size) if size else 0.0
        limit = max(tolerance, span * 1e-3)
        delta_b = _vec_diff(base_bounds.get("min"), now_bounds.get("min"), limit)
        delta_x = _vec_diff(base_bounds.get("max"), now_bounds.get("max"), limit)
        worst = max([v for v in (delta_b, delta_x) if v is not None] or [0.0])
        if worst > limit:
            warnings.append(
                {
                    "code": "character_moved",
                    "message": f"角色包围盒较基线变化了 {worst:.4f}（世界单位）。",
                }
            )
    elif base_bounds != now_bounds:
        warnings.append(
            {"code": "character_scope_changed", "message": "参与取景的角色对象与基线不同。"}
        )

    base_group = baseline.get("character_group")
    now_group = (current.get("character") or {}).get("group")
    if base_group != now_group:
        warnings.append(
            {
                "code": "character_group_changed",
                "message": f"主要角色判定由「{base_group}」变为「{now_group}」。",
            }
        )

    animation = camera_info.get("animation") or {}
    if animation.get("has_animation"):
        sources = "、".join(animation.get("sources") or []) or "动画数据"
        warnings.append(
            {
                "code": "camera_animated",
                "message": (
                    f"当前相机带有动画（{sources}"
                    + (f"；{animation.get('fcurves')} 条曲线" if animation.get("fcurves") else "")
                    + "）。预览固定使用建立基线时的帧，帧一旦变化基线即失效。"
                ),
            }
        )

    base_res = _as_float_list(baseline.get("resolution") or [])
    now_res = _as_float_list(current.get("resolution") or [])
    if base_res and now_res and len(base_res) == len(now_res):
        base_aspect = (base_res[0] or 0) / max(base_res[1] or 1, 1)
        now_aspect = (now_res[0] or 0) / max(now_res[1] or 1, 1)
        if abs(base_aspect - now_aspect) > 1e-3:
            warnings.append(
                {
                    "code": "aspect_changed",
                    "message": f"输出纵横比已从 {base_aspect:.3f} 变为 {now_aspect:.3f}。",
                }
            )

    return {
        "stale": bool(reasons),
        "reasons": reasons,
        "warnings": warnings,
        "checked": True,
    }


def _bounds_size(bounds: dict[str, Any]) -> list[float] | None:
    mn = _as_float_list(bounds.get("min"), 3)
    mx = _as_float_list(bounds.get("max"), 3)
    if mn is None or mx is None:
        return None
    return [mx[i] - mn[i] for i in range(3)]


def stale_error_payload(verdict: dict[str, Any]) -> dict[str, Any]:
    reasons = verdict.get("reasons") or []
    detail = "；".join(r.get("message", "") for r in reasons if isinstance(r, dict))
    return {
        "message": "当前帧/相机已变化，请刷新基线。" + (f"（{detail}）" if detail else ""),
        "reasons": reasons,
        "warnings": verdict.get("warnings") or [],
    }


# =============================================================================
#  渲染代码（当前相机 / 临时自动取景）
# =============================================================================


def build_render_code(
    png_path: str,
    width: int,
    height: int,
    percentage: int,
    framing: dict[str, Any] | None = None,
    expected: dict[str, Any] | None = None,
) -> str:
    """渲染单张 PNG。

    * ``framing.mode == "current_camera"``：沿用当前相机，**不建任何临时相机**。
    * 其余模式：建独立临时预览相机 → 设 ``scene.camera`` → 渲染 →
      **finally 恢复 ``scene.camera`` 并删除临时相机**（失败路径同样恢复）。
    * 渲染前后都**不改帧**，渲染结束后恢复原分辨率。
    * ``expected`` 给出基线时的帧与相机名：若已被外部改动，则**不渲染**，
      直接回一个 ``FRAMING_STALE`` 中止标记（兜底，防止提交与执行之间的时间差）。

    **输出格式（PNG）绝不硬写**：渲染前把 ``media_type`` / ``file_format`` /
    ``color_mode`` / ``color_depth`` / ``filepath`` 整份记下，再「赋值 + 回读」地
    切到 PNG —— 工程是影片输出（``media_type == "VIDEO"`` / ``FFMPEG``）时，
    ``file_format`` 的可用集合被限定为影片格式，必须先切回 ``IMAGE`` 才能赋 PNG。
    切不过去就**什么都不改**地中止并回 ``PREVIEW_OUTPUT_UNAVAILABLE``；
    渲染结束（含异常、含中止）后在 ``finally`` 里**逐项**写回这些设置，
    单项失败不阻断其余项，结果放在 ``output`` 里（``restored_ok`` / ``mismatches``）。

    注意：输出格式是**工具运行设置**，只活在这一条生成代码的生命周期里 ——
    既不写进 ``.blend``，也不随渲染结果持久化。
    """
    options = validate_options(framing)
    mode = options["mode"]
    margin = float(options["margin"])
    expected = expected or {}
    expect_frame = expected.get("frame_current")
    expect_camera = expected.get("camera")

    return (
        _PRELUDE
        + _helpers()
        + f'''
_target = {png_path!r}
_mode = {mode!r}
_margin = {margin!r}
_expect_frame = {expect_frame!r}
_expect_camera = {expect_camera!r}
_width = {int(width)}
_height = {int(height)}
_percentage = {int(percentage)}

_TARGET_FORMAT = "PNG"
_TARGET_COLOR_MODE = "RGBA"
#: 预览期间会临时改动的输出设置。``filepath`` 在 ``render`` 上，其余在 ``image_settings`` 上。
_OUTPUT_FIELDS = ("media_type", "file_format", "color_mode", "color_depth")
_image_settings = getattr(render, "image_settings", None)


def _read_output_state():
    """整份读回预览会碰的输出设置（读不到就是 None，不猜）。"""
    _state = {{}}
    for _name in _OUTPUT_FIELDS:
        if _image_settings is None:
            _state[_name] = None
        else:
            _state[_name] = _safe(lambda _n=_name: getattr(_image_settings, _n, None))
    _state["filepath"] = _safe(lambda: getattr(render, "filepath", None))
    return _state


def _set_and_readback(_obj, _name, _value):
    """写一个属性并**回读**；绝不把「没报错」当成「生效」。"""
    _current = _safe(lambda: getattr(_obj, _name, None))
    if _current == _value:
        return {{"requested": _value, "readback": _current, "ok": True, "unchanged": True}}
    _rec = {{"requested": _value, "readback": None, "ok": False, "error": None}}
    try:
        setattr(_obj, _name, _value)
    except Exception as _exc:
        _rec["error"] = "%s: %s" % (type(_exc).__name__, _exc)
    _rec["readback"] = _safe(lambda: getattr(_obj, _name, None))
    _rec["ok"] = _rec["readback"] == _value
    return _rec


def _use_png_output():
    """把场景输出切到 PNG；失败时回一个**明确原因**（不抛给调用方）。

    两条实测事实（Blender 5.2）决定了这里不能只写一行赋值：

    * ``image_settings.media_type == "VIDEO"``（工程是影片输出）时，``file_format``
      的可用集合被限定为影片格式，直接赋 ``"PNG"`` 会抛
      ``enum "PNG" not found in ('FFMPEG')`` —— 必须先切回 ``IMAGE``；
    * ``bl_rna.properties["file_format"].enum_items`` **不可信**：影片态下它照样把
      PNG 列出来，只有赋值才报错。所以可用性一律以「赋值 + 回读」判定。
    """
    if _image_settings is None:
        return False, "当前 Blender 的 render 上没有 image_settings，无法指定 PNG 输出。"
    _media_now = _safe(lambda: getattr(_image_settings, "media_type", None))
    if _media_now is not None and _media_now != "IMAGE":
        try:
            _image_settings.media_type = "IMAGE"
        except Exception as _exc:
            return False, "无法把输出媒体类型从 %s 切到 IMAGE：%s" % (_media_now, _exc)
        if _safe(lambda: getattr(_image_settings, "media_type", None)) != "IMAGE":
            return False, "把输出媒体类型切成 IMAGE 后回读仍不为 IMAGE。"
    try:
        _image_settings.file_format = _TARGET_FORMAT
    except Exception as _exc:
        return False, "当前工程的输出格式不接受 PNG：%s" % _exc
    _fmt_now = _safe(lambda: getattr(_image_settings, "file_format", None))
    if _fmt_now != _TARGET_FORMAT:
        return False, "把输出格式赋成 PNG 后回读仍为 %s。" % _fmt_now
    if hasattr(_image_settings, "color_mode"):
        _safe(lambda: setattr(_image_settings, "color_mode", _TARGET_COLOR_MODE))
    return True, None


def _restore_output_state(_state):
    """逐项写回输出设置；**单项失败不阻断其余项**，并把每项结果报出来。

    顺序有意为之：``media_type`` 决定 ``file_format`` 的可用集合，必须最先恢复；
    ``color_mode`` / ``color_depth`` 放最后 —— 改 ``file_format`` 会连带改它们。
    """
    _report = {{}}
    if _image_settings is not None:
        for _name in _OUTPUT_FIELDS:
            _want = _state.get(_name)
            if _want is None:
                continue
            _report[_name] = _set_and_readback(_image_settings, _name, _want)
    _want_path = _state.get("filepath")
    if _want_path is not None:
        _report["filepath"] = _set_and_readback(render, "filepath", _want_path)
    return _report


os.makedirs(os.path.dirname(_target), exist_ok=True)
_original_resolution = [render.resolution_x, render.resolution_y, render.resolution_percentage]
_original_output = _read_output_state()
_original_camera = scene.camera
_original_camera_name = _original_camera.name if _original_camera is not None else None
_frame_at_start = scene.frame_current

_png_applied = None
_png_reason = None
_output_restore = {{}}
_removed_partial = False
_temporary = None
_rendered = False
_aborted = None
_fit = None
_character = None
_camera_used_name = None
_distance = None

try:
    if _expect_frame is not None and int(_frame_at_start) != int(_expect_frame):
        _aborted = {{
            "code": "FRAMING_STALE",
            "message": "当前帧已从 %s 变为 %s，预览已中止。" % (_expect_frame, _frame_at_start),
        }}
    elif _expect_camera is not None and _original_camera_name != _expect_camera:
        _aborted = {{
            "code": "FRAMING_STALE",
            "message": "当前相机已从 %s 变为 %s，预览已中止。" % (_expect_camera, _original_camera_name),
        }}

    if _aborted is None:
        # ★ 先把输出格式**安全**切到 PNG，再动任何东西：切不过去就什么都不改地中止。
        #   绝不硬写 "PNG" —— 影片输出（FFMPEG）的工程上那行赋值必抛 TypeError。
        _png_ok, _png_why = _use_png_output()
        _png_applied = bool(_png_ok)
        _png_reason = _png_why
        if not _png_ok:
            _aborted = {{
                "code": "PREVIEW_OUTPUT_UNAVAILABLE",
                "message": "无法把工程输出格式安全切到 PNG，预览已中止：%s" % _png_why,
            }}

    if _aborted is None:
        render.resolution_x = _width
        render.resolution_y = _height
        render.resolution_percentage = _percentage

        dg = bpy.context.evaluated_depsgraph_get()
        _character = _character_report(scene, dg)
        _bounds = _character["bounds"]

        if _mode == "current_camera":
            _camera_used = _original_camera
            if _camera_used is None:
                raise RuntimeError("当前场景没有活动相机，无法用「当前相机」取景。")
            _probe_bounds = _bounds
            _camera_used_name = _camera_used.name
        else:
            if not _bounds:
                raise RuntimeError("没有找到可用的角色网格，无法自动取景。")
            _camera_used, _distance, _target_point = _build_preview_camera(
                scene, _original_camera, _bounds, _mode, _margin
            )
            _temporary = _camera_used
            _camera_used.name = {TEMP_CAMERA_NAME!r}
            _camera_used_name = _camera_used.name
            scene.camera = _camera_used
            _region_target, _probe_bounds = _region(_bounds, _mode)

        _safe(lambda: bpy.context.view_layer.update())
        _fit = _fit_report(
            _camera_used, _probe_bounds, _bounds, scene, _temporary is not None,
            _safe(lambda: getattr(_camera_used.data, "clip_start", None)),
            [_width, _height, _percentage],
        )

        bpy.ops.render.render(write_still=False)
        _image = bpy.data.images.get("Render Result")
        if _image is None:
            raise RuntimeError("渲染结束后未找到 Render Result")
        _image.save_render(filepath=_target, scene=scene)
        _rendered = os.path.isfile(_target)
finally:
    # ★ 无论成功、异常、还是被中止，都必须把工程恢复原样
    try:
        if scene.camera is not _original_camera:
            scene.camera = _original_camera
    except Exception:
        pass
    if _temporary is not None:
        _temp_data = _safe(lambda: _temporary.data)
        try:
            bpy.data.objects.remove(_temporary, do_unlink=True)
        except Exception:
            pass
        if _temp_data is not None:
            try:
                bpy.data.cameras.remove(_temp_data, do_unlink=True)
            except Exception:
                pass
    try:
        render.resolution_x, render.resolution_y, render.resolution_percentage = _original_resolution
    except Exception:
        pass
    # 输出设置（media_type / file_format / color_mode / color_depth / filepath）逐项写回。
    # 这一块自己吞掉一切异常，且逐项独立 —— 渲染、存图、删临时相机有任何一步炸了，
    # 都不会阻断这里的恢复。
    _output_restore = _restore_output_state(_original_output)
    # 失败路径不留半成品预览文件
    if not _rendered and _safe(lambda: os.path.isfile(_target), False):
        try:
            os.remove(_target)
            _removed_partial = True
        except Exception:
            _removed_partial = False

_camera_restored = scene.camera is _original_camera
_leftovers = 0
try:
    _leftovers = len([o for o in bpy.data.objects if o.name == {TEMP_CAMERA_NAME!r}])
except Exception:
    _leftovers = -1

# 输出设置的无污染验证：逐项与「渲染前记录的原值」比对，验证不了就算**没通过**
_output_now = _read_output_state()
_output_mismatches = {{}}
for _name in list(_OUTPUT_FIELDS) + ["filepath"]:
    _want = _original_output.get(_name)
    if _want is None:
        continue
    _got = _output_now.get(_name)
    if _got != _want:
        _output_mismatches[_name] = {{"expected": _want, "actual": _got}}
_output_restored = not _output_mismatches

_out = {{
    "rendered": _rendered,
    "aborted": _aborted,
    "path": _target,
    "size_bytes": (os.path.getsize(_target) if os.path.isfile(_target) else 0),
    "render_resolution": [_width, _height, _percentage],
    "restored_resolution": [render.resolution_x, render.resolution_y, render.resolution_percentage],
    "frame": _frame_at_start,
    "frame_after": scene.frame_current,
    "camera_original": _original_camera_name,
    "camera_used": _camera_used_name,
    "camera_restored": _camera_restored,
    "temporary_camera_leftovers": _leftovers,
    "partial_file_removed": _removed_partial,
    "output": {{
        "target_format": _TARGET_FORMAT,
        "target_color_mode": _TARGET_COLOR_MODE,
        "applied": _png_applied,
        "unavailable_reason": _png_reason,
        "original": _original_output,
        "restored": _output_now,
        "restore_steps": _output_restore,
        "restored_ok": _output_restored,
        "mismatches": _output_mismatches,
    }},
    "framing": {{
        "mode": _mode,
        "mode_label": {FRAMING_MODES.get(mode, mode)!r},
        "margin": _margin,
        "temporary": _temporary is not None,
        "temporary_camera_name": {TEMP_CAMERA_NAME!r},
        "original_camera": _original_camera_name,
        "camera_used": _camera_used_name,
        "camera_restored": _camera_restored,
        "distance": _distance,
        "fit": _fit,
        "character": _character,
    }},
}}
print("__TOON_JSON__" + json.dumps(_out, ensure_ascii=False, default=str))
'''.strip()
    )
