# -*- coding: utf-8 -*-
"""
体积雾灯光方案 ``volume_fog``（PR #8 规格 §二）。

从 :mod:`render_pipeline.一键卡通渲染` 的 ``setup_volumetric_lights`` 提取 ——
那份代码由 ``fb43b61``（2026-10-11 差异移植）引入，是竖屏角色近景里验证过的
7 灯冷暖布光 + 程序化体积雾。本模块保持与它**同一套口径**：

* 7 盏冷暖灯（``LGT_01``…``LGT_07``）；
* 集合 ``TOON_体积雾灯光组``、总控 ``TOON_灯光总控``；
* 程序化体积雾（4D Noise → Power → Multiply(密度) → Principled Volume）；
* 黑色无环境光世界；
* 按目标模型世界包围盒缩放与定位；
* 重复执行时刷新旧灯组；
* 原场景灯光只暂时停用、不删除；
* 任务结束后恢复原状态。
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .base import ParamSpec, Scheme, SchemeResult
from .snapshot import SceneSnapshot, dump_legacy_light_state

#: 与主脚本一致的固定命名 —— 真机排查时两套代码必须能对得上。
VOLUME_FOG_COLLECTION = "TOON_体积雾灯光组"
VOLUME_FOG_CONTROLLER = "TOON_灯光总控"
BLACK_WORLD_NAME = "TOON_体积雾黑场世界"
FOG_OBJECT_NAME = "VOLUME_01_程序化体积雾"
FOG_MATERIAL_NAME = "MAT_程序化体积雾"

#: 参考包围盒（竖屏角色近景基准）。与主脚本逐字一致。
REF_CENTER = (0.0, 0.15, 0.64)
REF_SIZE_Z = 1.36


def _bpy() -> Any:
    import bpy  # noqa: PLC0415 - 延迟导入

    return bpy


# -------------------------------------------------------------------------------------
#  第一版动态参数（规格 §二）
# -------------------------------------------------------------------------------------
#
# ★ 只开放 fog_density。
#
# 规格原文：「如果灯光强度项尚未经过真实 Blender 标定，第一版可以只开放 fog_density，
#           其余字段保持只读描述，不得凭空开放。」
#
# 本机**没有可用的 Blender**，因此 light_intensity / cool_rim_intensity /
# warm_rim_intensity 三个强度项**无法标定**。给它们编一个「看起来合理」的范围，
# 等于把一个没验证过的安全边界写进产品 —— 用户按这个范围调到上限，画面过曝，
# 而系统宣称这是「允许的取值」。所以这里把它们标 readonly，只展示、不提交。
#
# 等真机标定完成（在真实 Blender 里跑出各强度的可用区间），把 readonly 去掉、
# 补上 minimum/maximum 即可 —— 但要**同时**升 version（见类属性注释）。

VOLUME_FOG_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec(
        id="fog_density",
        kind="number",
        label="雾密度",
        minimum=0.0,
        maximum=0.1,
        step=0.001,
        default=0.018,
        description="程序化体积雾密度。0 = 无雾，0.1 = 浓雾；默认 0.018 为近景验证值。",
    ),
    # ---- 以下三项为本版**只读展示**项：未经真实 Blender 标定，不开放调整 ----
    ParamSpec(
        id="light_intensity",
        kind="number",
        label="灯光强度",
        minimum=0.25,
        maximum=2.0,
        step=0.05,
        default=1.0,
        description="7 灯的总体强度倍率。当前未经真实 Blender 标定，仅展示不可调。",
        readonly=True,
    ),
    ParamSpec(
        id="cool_rim_intensity",
        kind="number",
        label="冷轮廓强度",
        minimum=0.0,
        maximum=2.0,
        step=0.05,
        default=1.0,
        description="左后冷轮廓灯（LGT_02）强度倍率。未经标定，仅展示不可调。",
        readonly=True,
    ),
    ParamSpec(
        id="warm_rim_intensity",
        kind="number",
        label="暖轮廓强度",
        minimum=0.0,
        maximum=2.0,
        step=0.05,
        default=1.0,
        description="右后暖轮廓灯（LGT_03）强度倍率。未经标定，仅展示不可调。",
        readonly=True,
    ),
    ParamSpec(
        id="black_world",
        kind="boolean",
        label="黑色无环境光世界",
        default=True,
        description="是否替换为黑色无环境光世界，避免原 HDRI 与冷暖灯组重复补光。",
        readonly=True,
    ),
)


class VolumeFogScheme(Scheme):
    """7 灯冷暖布光 + 程序化体积雾。"""

    id = "volume_fog"
    #: ★ 版本进入任务指纹（规格 §五）。改灯组数值 / 改参数语义 / 把只读项改成可调，
    #: 都必须 +1，否则旧任务会被错误复用，用户拿到的是「上一版方案的产物」。
    version = 1
    label = "体积雾冷暖灯光"
    description = "7 灯冷暖布光与程序化体积雾"
    recommended_use = "角色近景、竖构图；需要氛围感与冷暖分离"
    has_volume_fog = True
    performance_note = "含程序化体积雾；EEVEE 下体积采样会增加开销"

    PARAMS = VOLUME_FOG_PARAMS

    COLLECTIONS = (VOLUME_FOG_COLLECTION,)

    # -- 应用 ---------------------------------------------------------------------

    def apply(
        self, scene: Any, targets: Sequence[Any], parameters: Mapping[str, Any]
    ) -> SchemeResult:
        from mathutils import Vector  # noqa: PLC0415 - 只在 Blender 里可用

        bpy = _bpy()
        fog_density = float(parameters["fog_density"])

        snapshot = SceneSnapshot.capture(
            scene, collection_names=self.COLLECTIONS
        )

        center, size = _world_bbox(targets)
        ref_center = Vector(REF_CENTER)
        if center is None:
            center, size = ref_center.copy(), Vector((0.94, 0.84, REF_SIZE_Z))
        scale = max(0.05, float(size.z) / REF_SIZE_Z)

        # ---- 刷新旧灯组：重复执行不累计对象（规格 §八「重复执行不累计对象」）----
        old_coll = bpy.data.collections.get(VOLUME_FOG_COLLECTION)
        if old_coll is not None:
            for obj in list(old_coll.objects):
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                except Exception:
                    pass
            bpy.data.collections.remove(old_coll)

        coll = bpy.data.collections.new(VOLUME_FOG_COLLECTION)
        scene.collection.children.link(coll)

        # ---- 原场景的灯不删除、只停用；原状态写进集合自定义属性，便于恢复 ----
        coll["旧灯光状态"] = dump_legacy_light_state(scene)
        coll["配置版本"] = "volume_fog_v1"
        coll["参考来源"] = "笨蛋的体积雾练习 + 偷懒用打光 + Blender混沌的打光预设"
        for obj in list(getattr(scene, "objects", []) or []):
            if getattr(obj, "type", None) == "LIGHT" and obj.name not in snapshot.light_flags:
                # 新出现的灯（理论上不该有）也一并停用，不让它混进渲染。
                pass
            if getattr(obj, "type", None) == "LIGHT":
                obj.hide_viewport = True
                obj.hide_render = True

        aim = center + Vector((0.0, -0.07, 0.38)) * scale
        ctrl = bpy.data.objects.new(VOLUME_FOG_CONTROLLER, None)
        coll.objects.link(ctrl)
        ctrl.location = aim
        ctrl.empty_display_type = "CIRCLE"
        ctrl.empty_display_size = 0.42 * scale
        ctrl.show_in_front = True
        ctrl["使用说明"] = "移动/旋转/缩放本对象可整体调整灯光与体积雾；单灯能量仍可独立修改"

        def p(ref_location: Sequence[float]) -> Any:
            return center + (Vector(ref_location) - ref_center) * scale

        def add(name, light_type, ref_location, energy, color, spot_deg=75.0,
                spot_blend=1.0, radius=0.12, area_size=1.0) -> Any:
            data = bpy.data.lights.get(name)
            if data is None or getattr(data, "type", None) != light_type:
                data = bpy.data.lights.new(name, light_type)
            data.energy = energy * scale * scale
            data.color = color
            data.use_shadow = True
            if hasattr(data, "shadow_soft_size"):
                data.shadow_soft_size = radius * scale
            if light_type == "AREA":
                data.shape = "DISK"
                data.size = area_size * scale
            elif light_type == "SPOT":
                data.spot_size = math.radians(spot_deg)
                data.spot_blend = spot_blend
            obj = bpy.data.objects.new(name, data)
            coll.objects.link(obj)
            obj.location = p(ref_location)
            if light_type in {"AREA", "SPOT", "SUN"}:
                obj.rotation_euler = (aim - obj.location).to_track_quat("-Z", "Y").to_euler()
            world = obj.matrix_world.copy()
            obj.parent = ctrl
            obj.matrix_parent_inverse = ctrl.matrix_world.inverted()
            obj.matrix_world = world
            obj.hide_viewport = False
            obj.hide_render = False
            obj["基础能量"] = float(data.energy)
            return obj

        lights = [
            add("LGT_01_正面暖主光", "SPOT", (-0.30, -2.35, 2.22), 115.0,
                (1.0, 0.91, 0.80), 74.0, 0.72, 0.18),
            add("LGT_02_左后冷轮廓", "SPOT", (-1.75, 1.60, 2.38), 520.0,
                (0.40, 0.66, 1.0), 72.0, 1.0, 0.10),
            add("LGT_03_右后暖轮廓", "SPOT", (1.55, 1.20, 2.24), 220.0,
                (1.0, 0.30, 0.11), 70.0, 1.0, 0.10),
            add("LGT_04_左侧冷辅光", "POINT", (-0.82, 0.02, 1.22), 24.0,
                (0.54, 0.68, 1.0), radius=0.22),
            add("LGT_05_右侧红辅光", "POINT", (0.72, 0.02, 1.26), 34.0,
                (1.0, 0.10, 0.035), radius=0.20),
            add("LGT_06_正面肤色补光", "POINT", (0.10, -0.72, 1.30), 10.0,
                (1.0, 0.78, 0.62), radius=0.25),
            add("LGT_07_脚底暖柔光", "AREA", (0.0, 0.0, -0.18), 5.0,
                (1.0, 0.42, 0.25), area_size=1.20),
        ]

        # ---- 黑色无环境光世界 ----
        world = bpy.data.worlds.get(BLACK_WORLD_NAME) or bpy.data.worlds.new(BLACK_WORLD_NAME)
        world.use_nodes = True
        ntw = world.node_tree
        ntw.nodes.clear()
        bg = ntw.nodes.new("ShaderNodeBackground")
        bg.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        bg.inputs["Strength"].default_value = 0.0
        outw = ntw.nodes.new("ShaderNodeOutputWorld")
        ntw.links.new(bg.outputs["Background"], outw.inputs["Surface"])
        scene.world = world
        snapshot.extra_checks.append(
            ("原世界恢复", lambda: _world_restored(snapshot))
        )

        # ---- 程序化体积雾 ----
        bpy.ops.mesh.primitive_cube_add(location=p((0.0, 0.10, 1.10)))
        fog = bpy.context.object
        fog.name = FOG_OBJECT_NAME
        fog.dimensions = tuple(v * scale for v in (5.2, 5.2, 4.2))
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        for owner in list(getattr(fog, "users_collection", []) or []):
            owner.objects.unlink(fog)
        coll.objects.link(fog)
        fog.display_type = "WIRE"

        mat = bpy.data.materials.get(FOG_MATERIAL_NAME) or bpy.data.materials.new(FOG_MATERIAL_NAME)
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        nodes.clear()
        links = mat.node_tree.links
        outm = nodes.new("ShaderNodeOutputMaterial")
        vol = nodes.new("ShaderNodeVolumePrincipled")
        vol.inputs["Color"].default_value = (0.50, 0.52, 0.58, 1.0)
        vol.inputs["Anisotropy"].default_value = 0.14
        noise = nodes.new("ShaderNodeTexNoise")
        noise.noise_dimensions = "4D"
        noise.inputs["W"].default_value = 5.2
        noise.inputs["Scale"].default_value = 19.7
        noise.inputs["Detail"].default_value = 2.0
        power = nodes.new("ShaderNodeMath")
        power.operation = "POWER"
        power.inputs[1].default_value = 2.2
        mult = nodes.new("ShaderNodeMath")
        mult.operation = "MULTIPLY"
        mult.inputs[1].default_value = fog_density
        links.new(noise.outputs["Fac"], power.inputs[0])
        links.new(power.outputs[0], mult.inputs[0])
        links.new(mult.outputs[0], vol.inputs["Density"])
        links.new(vol.outputs["Volume"], outm.inputs["Volume"])
        fog.data.materials.append(mat)

        world_matrix = fog.matrix_world.copy()
        fog.parent = ctrl
        fog.matrix_parent_inverse = ctrl.matrix_world.inverted()
        fog.matrix_world = world_matrix
        fog.hide_select = True

        ctrl["灯光数量"] = len(lights)
        ctrl["雾密度"] = fog_density
        ctrl["灯光顺序"] = _json_list([o.name for o in lights])

        # ---- 快照登记：本次新建的集合 / 对象 / 材质 ----
        if VOLUME_FOG_COLLECTION not in snapshot.preexisting_collections:
            snapshot.scheme_collections.append(VOLUME_FOG_COLLECTION)
        created = [ctrl, fog] + list(lights)
        snapshot.scheme_objects = [
            o.name for o in created if o.name not in snapshot.preexisting_objects
        ]
        if FOG_MATERIAL_NAME not in snapshot.preexisting_materials:
            snapshot.scheme_materials.append(FOG_MATERIAL_NAME)

        result = SchemeResult(
            scheme_id=self.id,
            version=self.version,
            parameters=dict(parameters),
            snapshot=snapshot,
            lights=list(lights),
            created_objects=list(created),
        )
        result.notes.append("7 灯冷暖灯组 + 程序化体积雾（雾密度 %s）" % fog_density)
        result.notes.append("原场景灯光已暂时停用（不删除），任务结束后恢复")
        return result

    def restore(self, snapshot: Any) -> None:
        if snapshot is None:
            return
        snapshot.restore()


def _world_restored(snapshot: SceneSnapshot) -> bool:
    """回读：世界是否已回到应用前那个。"""
    bpy = _bpy()
    current = getattr(bpy.context.scene, "world", None)
    name = getattr(current, "name", None) if current is not None else None
    return name == snapshot.world_name


def _json_list(values: Sequence[str]) -> str:
    import json  # noqa: PLC0415

    return json.dumps(list(values), ensure_ascii=False)


def _world_bbox(objs: Sequence[Any]) -> tuple[Any, Any]:
    """合并计算一组对象的世界空间包围盒 → (中心, 尺寸)。

    与主脚本 ``world_bbox`` 同一口径；抽到这里是为了让体积雾方案可以独立执行
    （旧实现依赖主脚本次调）。
    """
    from mathutils import Vector  # noqa: PLC0415

    mn = Vector((1e9, 1e9, 1e9))
    mx = Vector((-1e9, -1e9, -1e9))
    found = False
    for o in objs:
        if getattr(o, "type", None) != "MESH" or not getattr(getattr(o, "data", None), "vertices", None):
            continue
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            for i in range(3):
                mn[i] = min(mn[i], w[i])
                mx[i] = max(mx[i], w[i])
            found = True
    if not found:
        return None, None
    return (mn + mx) * 0.5, (mx - mn)
