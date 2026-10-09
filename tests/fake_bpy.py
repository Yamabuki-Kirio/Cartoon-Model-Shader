"""极简 `bpy` 桩：让服务端生成的代码在无 Blender 环境下真实执行。

这不是「假响应」，而是**真的把生成的 Python 代码跑一遍**，只是把 ``bpy`` 换成桩。
因此它能捕捉到生成代码里的逻辑错误（例如字符串被 ``list()`` 拆成字符、
或者取景矩形忘记按 view_frame 的距离归一化）。

覆盖范围：MVP-02 的曝光/辉光代码，以及本次的取景代码
（``camera.data.view_frame`` / 角色包围盒 / ``bpy.data.objects|cameras`` 的增删）。

⚠ ``FakeCameraData.view_frame`` 刻意复刻真实 Blender 的口径：
  返回点**不在单位距离上**（fit 方向的半高恒为 0.5），因此忘记归一化的实现
  会在这里直接暴露。实测真实值：lens=68.4966 / sensor_fit=VERTICAL 时
  ``view_frame()`` 返回 z≈-2.854、半高 0.5。
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import types
from pathlib import Path
from typing import Any, Iterator

# 一个最小合法 PNG（1x1 透明），用于校验「确实落盘了图片」
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

#: 默认「角色」网格的局部包围盒（量级参照真实 MMD 模型：约 1.56 m 高）
CHARACTER_LOCAL_MIN = (-0.34, -0.25, -0.03)
CHARACTER_LOCAL_MAX = (0.29, 0.18, 1.52)


# =============================================================================
#  基础：矩阵 / 欧拉角
# =============================================================================


def euler_xyz_matrix(rotation: tuple[float, float, float]) -> list[list[float]]:
    """XYZ 欧拉角 -> 3x3（R = Rz @ Ry @ Rx），行主序。"""
    rx, ry, rz = (float(v) for v in rotation)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    rx_m = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    ry_m = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    rz_m = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]
    tmp = [[sum(rz_m[i][k] * ry_m[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return [[sum(tmp[i][k] * rx_m[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


class FakeMatrix:
    """4x4 行主序矩阵；支持 ``m[i][j]`` 与 ``.translation``。"""

    def __init__(self, rows: list[list[float]] | None = None) -> None:
        self._rows = rows or [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]

    def __getitem__(self, index: int) -> list[float]:
        return list(self._rows[index])

    def __iter__(self) -> Iterator[list[float]]:
        return iter([list(r) for r in self._rows])

    def __len__(self) -> int:
        return 4

    @property
    def translation(self) -> tuple[float, float, float]:
        return (self._rows[0][3], self._rows[1][3], self._rows[2][3])


def compose_matrix(
    location: tuple[float, float, float],
    rotation: tuple[float, float, float],
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> FakeMatrix:
    rot = euler_xyz_matrix(rotation)
    rows = [[0.0] * 4 for _ in range(4)]
    for i in range(3):
        for j in range(3):
            rows[i][j] = rot[i][j] * float(scale[j])
    rows[0][3], rows[1][3], rows[2][3] = (float(v) for v in location)
    rows[3][3] = 1.0
    return FakeMatrix(rows)


# =============================================================================
#  节点（合成器）
# =============================================================================


class FakeSocket:
    def __init__(self, name: str, value: Any) -> None:
        self.name = name
        self.default_value = value
        self.is_linked = False


class FakeInputs:
    """模拟 ``node.inputs``：支持 ``.get(name)`` 与迭代。"""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self._sockets = [FakeSocket(k, v) for k, v in mapping.items()]
        self._by_name = {s.name: s for s in self._sockets}

    def get(self, name: str) -> FakeSocket | None:
        return self._by_name.get(name)

    def __iter__(self) -> Iterator[FakeSocket]:
        return iter(self._sockets)

    def __getitem__(self, name: str) -> FakeSocket:
        socket = self._by_name.get(name)
        if socket is None:
            raise KeyError(name)
        return socket


class FakeNode:
    def __init__(self, name: str, node_type: str, inputs: dict[str, Any] | None = None) -> None:
        self.name = name
        self.label = name
        self.type = node_type
        self.bl_idname = "CompositorNode" + node_type.title()
        self.mute = False
        self.inputs = FakeInputs(inputs or {})


class _NodeMap:
    def __init__(self, nodes: list[FakeNode]) -> None:
        self._by_name = {n.name: n for n in nodes}

    def get(self, name: str) -> FakeNode | None:
        return self._by_name.get(name)

    def __iter__(self) -> Iterator[FakeNode]:
        return iter(self._by_name.values())


class FakeNodeGroup:
    def __init__(self, name: str, nodes: list[FakeNode]) -> None:
        self.name = name
        self.nodes = _NodeMap(nodes)
        self.links: list[Any] = []


DEFAULT_GLARE_INPUTS: dict[str, Any] = {
    "Image": [1.0, 1.0, 1.0, 1.0],
    "Type": "Bloom",
    "Quality": "Medium",
    "Threshold": 1.1,
    "Smoothness": 0.1,
    "Clamp": False,
    "Maximum": 10.0,
    "Strength": 1.182417631149292,
    "Saturation": 1.0,
    "Tint": [1.0, 1.0, 1.0, 1.0],
    "Size": 0.5,
}


# =============================================================================
#  场景 / 渲染设置
# =============================================================================


#: Blender 为非族前缀视图提供的通用对比度档位（与 src/server/color_looks 一致）
GENERIC_LOOKS: tuple[str, ...] = (
    "Very High Contrast",
    "High Contrast",
    "Medium High Contrast",
    "Medium Contrast",
    "Medium Low Contrast",
    "Low Contrast",
    "Very Low Contrast",
)

#: 复刻 Blender 5.2.1 的真实能力表：**同一字符串在不同视图下合法性不同**。
#: 这正是 look 报 enum not found 的根源，桩必须照抄，否则测试形同虚设。
LOOK_CAPABILITY: dict[str, list[str]] = {
    "Standard": ["None", *GENERIC_LOOKS],
    "Filmic": ["None", *GENERIC_LOOKS],
    "Filmic Log": ["None", *GENERIC_LOOKS],
    "Raw": ["None", *GENERIC_LOOKS],
    "Khronos PBR Neutral": ["None", *GENERIC_LOOKS],
    "AgX": [
        "None",
        "AgX - Punchy",
        "AgX - Greyscale",
        "AgX - Very High Contrast",
        "AgX - High Contrast",
        "AgX - Medium High Contrast",
        "AgX - Base Contrast",
        "AgX - Medium Low Contrast",
        "AgX - Low Contrast",
        "AgX - Very Low Contrast",
    ],
    "False Color": [
        "None",
        "False Color - Punchy",
        "False Color - Greyscale",
        "False Color - Very High Contrast",
        "False Color - High Contrast",
        "False Color - Medium High Contrast",
        "False Color - Base Contrast",
        "False Color - Medium Low Contrast",
        "False Color - Low Contrast",
        "False Color - Very Low Contrast",
    ],
    "ACES 1.3": ["None", "ACES 1.3 - Reference Gamut Compression"],
    "ACES 2.0": ["None", "ACES 2.0 - Reference Gamut Compression"],
}

#: 「只提供通用 contrast identifier」的另一种 Blender 口味：
#: AgX 也只认通用档位。用于验证「label 由前端组合、value 保持 identifier」的迁移路径。
LEGACY_LOOK_CAPABILITY: dict[str, list[str]] = {
    **LOOK_CAPABILITY,
    "AgX": ["None", *GENERIC_LOOKS],
}


def _allowed_looks_for(view_transform: str, capability: dict[str, list[str]] | None = None) -> list[str]:
    table = capability if capability is not None else LOOK_CAPABILITY
    return list(table.get(view_transform, ["None"]))


def _look_slot(identifier: str) -> str:
    """取「对比度档位」本身，丢掉族前缀（``"AgX - High Contrast"`` -> ``"High Contrast"``）。"""
    if " - " in identifier:
        return identifier.split(" - ", 1)[1]
    return identifier


#: 默认初始 look：Blender 5.2.1 的 AgX 下真实合法。
_DEFAULT_LOOK = "AgX - High Contrast"


def _initial_look(capability: dict[str, list[str]]) -> str:
    """挑一个在 ``AgX`` 下**真的合法**的初始 look。

    ``LEGACY_LOOK_CAPABILITY`` 模拟的是「AgX 只认通用档位」的老版本，那里
    ``"AgX - High Contrast"`` 本身就是非法状态 —— 真实 Blender 不可能停在这种状态，
    桩也不该（否则探针的 ``finally`` 恢复会失败，测出来的是桩的毛病而非产品逻辑）。
    """
    allowed = list(capability.get("AgX") or ["None"])
    if _DEFAULT_LOOK in allowed:
        return _DEFAULT_LOOK
    slot = _look_slot(_DEFAULT_LOOK)
    for candidate in allowed:
        if _look_slot(candidate) == slot:
            return candidate
    return "None"


def _enum_error(attribute: str, value: Any, allowed: list[str]) -> TypeError:
    """复刻 Blender 的枚举报错格式 —— color_looks 正是从这段文本里解析允许列表。"""
    rendered = ", ".join(repr(item) for item in allowed)
    return TypeError(
        f'bpy_struct: item.attr = val: enum "{value}" not found in ({rendered})'
    )


class FakeViewSettings:
    """带**真实枚举约束**的 view_settings。

    ``look`` 的合法集合随 ``view_transform`` 变化；写入非法值会抛与 Blender 同格式的
    ``TypeError``。切换视图变换时会像 Blender 一样**按档位跨视图映射** look
    （实测：AgX 的 ``"AgX - High Contrast"`` 切到 Standard 后变成 ``"High Contrast"``，
    切回 AgX 又变回来）。
    """

    def __init__(self, capability: dict[str, list[str]] | None = None) -> None:
        self._capability = capability if capability is not None else LOOK_CAPABILITY
        self._view_transform = "AgX"
        # 初始 look 必须与能力表自洽，否则「探针 finally 恢复原值」这条断言测的是桩的毛病
        self._look = _initial_look(self._capability)
        self.exposure = 0.0
        self.gamma = 1.0
        #: 测试注入：写入等于该值的 look 时抛 RuntimeError（模拟 Blender 侧写入失败）
        self.fail_on_look: str | None = None
        #: 测试注入：``"enum"``（默认，复刻 Blender 报错格式，可被解析）
        #: 或 ``"bare"``（不可解析的报错，逼迫调用方走逐个赋值探测兜底）
        self.enum_error_style = "enum"
        #: 写入轨迹，供断言「label 没有被直接写进去」
        self.write_log: list[tuple[str, Any]] = []

    def _reject(self, attribute: str, value: Any, allowed: list[str]) -> TypeError:
        if self.enum_error_style == "bare":
            return TypeError(f"{attribute} 不支持取值 {value!r}")
        return _enum_error(attribute, value, allowed)

    # -- view_transform ---------------------------------------------------
    @property
    def view_transform(self) -> str:
        return self._view_transform

    @view_transform.setter
    def view_transform(self, value: Any) -> None:
        text = str(value)
        if text not in self._capability:
            raise self._reject("view_transform", value, list(self._capability))
        if text == self._view_transform:
            return
        self._view_transform = text
        self.write_log.append(("view_transform", text))
        # 像 Blender 一样按档位映射 look；找不到对应档位就落到 None
        if self._look not in self.allowed_looks:
            slot = _look_slot(self._look)
            mapped = next(
                (c for c in self.allowed_looks if _look_slot(c) == slot),
                "None",
            )
            self._look = mapped

    # -- look -------------------------------------------------------------
    @property
    def allowed_looks(self) -> list[str]:
        return _allowed_looks_for(self._view_transform, self._capability)

    @property
    def look(self) -> str:
        return self._look

    @look.setter
    def look(self, value: Any) -> None:
        text = str(value)
        if self.fail_on_look is not None and text == self.fail_on_look:
            raise RuntimeError(f"注入的 look 写入失败：{text}")
        if text not in self.allowed_looks:
            raise self._reject("look", value, self.allowed_looks)
        self._look = text
        self.write_log.append(("look", text))


class FakeImageSettings:
    def __init__(self) -> None:
        self.file_format = "PNG"
        self.color_mode = "RGBA"


class FakeRender:
    def __init__(self) -> None:
        self.engine = "BLENDER_EEVEE"
        self.resolution_x = 1080
        self.resolution_y = 1980
        self.resolution_percentage = 100
        self.pixel_aspect_x = 1.0
        self.pixel_aspect_y = 1.0
        self.film_transparent = True
        self.filepath = ""
        self.image_settings = FakeImageSettings()


class FakeDisplaySettings:
    def __init__(self) -> None:
        self.display_device = "sRGB"


# =============================================================================
#  动画数据（用于「相机是否带动画」）
# =============================================================================


class FakeFCurve:
    def __init__(self, data_path: str = "location") -> None:
        self.data_path = data_path
        self.keyframe_points: list[Any] = []


class FakeAction:
    def __init__(self, name: str, fcurves: int = 3) -> None:
        self.name = name
        self.fcurves = [FakeFCurve() for _ in range(fcurves)]


class FakeAnimData:
    def __init__(self, action: FakeAction | None = None, nla_tracks: int = 0, drivers: int = 0) -> None:
        self.action = action
        self.nla_tracks = [object() for _ in range(nla_tracks)]
        self.drivers = [object() for _ in range(drivers)]


# =============================================================================
#  数据块
# =============================================================================


class FakeMeshData:
    def __init__(self, name: str, polygons: int = 100, vertices: int = 8) -> None:
        self.name = name
        self.polygons = [object() for _ in range(polygons)]
        self.vertices = [object() for _ in range(vertices)]
        #: 局部包围盒 (min, max)；None 表示用默认角色盒
        self._bbox: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None


class FakeMaterialSlot:
    def __init__(self, name: str = "Material") -> None:
        self.name = name
        self.material = None


class FakeModifier:
    def __init__(self, type: str, object: Any = None) -> None:
        self.type = type
        self.object = object
        self.name = type.title()


class FakeCameraData:
    """相机数据：``view_frame`` 的口径与真实 Blender 一致。"""

    def __init__(
        self,
        name: str = "Camera",
        *,
        lens: float = 50.0,
        sensor_width: float = 36.0,
        sensor_height: float = 24.0,
        sensor_fit: str = "AUTO",
        shift_x: float = 0.0,
        shift_y: float = 0.0,
        cam_type: str = "PERSP",
        ortho_scale: float = 6.0,
        clip_start: float = 0.1,
        clip_end: float = 100.0,
    ) -> None:
        self.name = name
        self.type = cam_type
        self.lens = lens
        self.sensor_width = sensor_width
        self.sensor_height = sensor_height
        self.sensor_fit = sensor_fit
        self.shift_x = shift_x
        self.shift_y = shift_y
        self.ortho_scale = ortho_scale
        self.clip_start = clip_start
        self.clip_end = clip_end
        self.animation_data: FakeAnimData | None = None

    # -- 取景矩形 ---------------------------------------------------------
    def resolved_fit(self, scene: "FakeScene | None") -> str:
        if self.sensor_fit != "AUTO":
            return self.sensor_fit
        rx, ry = _resolution(scene)
        pax, pay = _pixel_aspect(scene)
        return "HORIZONTAL" if rx * pax >= ry * pay else "VERTICAL"

    def half_extents(self, scene: "FakeScene | None") -> tuple[float, float]:
        """单位距离处的半宽/半高（= tan(hfov/2), tan(vfov/2)）。"""
        fit = self.resolved_fit(scene)
        rx, ry = _resolution(scene)
        pax, pay = _pixel_aspect(scene)
        if self.type == "ORTHO":
            if fit == "HORIZONTAL":
                half_w = self.ortho_scale / 2.0
                half_h = half_w * (ry * pay) / (rx * pax)
            else:
                half_h = self.ortho_scale / 2.0
                half_w = half_h * (rx * pax) / (ry * pay)
            return half_w, half_h
        if fit == "HORIZONTAL":
            half_w = (self.sensor_width / 2.0) / self.lens
            half_h = half_w * (ry * pay) / (rx * pax)
        else:
            half_h = (self.sensor_height / 2.0) / self.lens
            half_w = half_h * (rx * pax) / (ry * pay)
        return half_w, half_h

    def frame_center(self, scene: "FakeScene | None") -> tuple[float, float]:
        """画面中心相对光轴的偏移（单位距离处）；shift 以 fit 方向的传感器尺寸归一化。"""
        fit = self.resolved_fit(scene)
        half_w, half_h = self.half_extents(scene)
        unit = (half_w * 2.0) if fit == "HORIZONTAL" else (half_h * 2.0)
        return self.shift_x * unit, self.shift_y * unit

    def view_frame(self, scene: "FakeScene | None" = None, depsgraph: Any = None) -> list[tuple[float, float, float]]:
        """复刻真实 Blender：返回 fit 方向半高恒为 0.5 的矩形，且 z 不在单位距离上。"""
        fit = self.resolved_fit(scene)
        half_w, half_h = self.half_extents(scene)
        if self.type == "ORTHO":
            depth = 1.0
        else:
            depth = 0.5 / (half_h if fit == "VERTICAL" else half_w)
        cx, cy = self.frame_center(scene)
        hw = half_w * depth
        hh = half_h * depth
        return [
            (cx - hw, cy - hh, -depth),
            (cx + hw, cy - hh, -depth),
            (cx + hw, cy + hh, -depth),
            (cx - hw, cy + hh, -depth),
        ]


def _resolution(scene: "FakeScene | None") -> tuple[float, float]:
    if scene is None:
        return (1920.0, 1080.0)
    return (float(scene.render.resolution_x), float(scene.render.resolution_y))


def _pixel_aspect(scene: "FakeScene | None") -> tuple[float, float]:
    if scene is None:
        return (1.0, 1.0)
    return (
        float(getattr(scene.render, "pixel_aspect_x", 1.0)),
        float(getattr(scene.render, "pixel_aspect_y", 1.0)),
    )


class FakeObject:
    """场景对象。``type`` 由 data 推断，行为贴近 bpy。"""

    def __init__(self, name: str, data: Any = None, obj_type: str | None = None) -> None:
        self.name = name
        self.data = data
        if obj_type is None:
            obj_type = "CAMERA" if isinstance(data, FakeCameraData) else (
                "MESH" if isinstance(data, FakeMeshData) else "EMPTY"
            )
        self.type = obj_type
        self.location: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.rotation_euler: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.rotation_mode = "XYZ"
        self.scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
        self.parent: "FakeObject | None" = None
        self.modifiers: list[FakeModifier] = []
        self.material_slots: list[FakeMaterialSlot] = []
        self.hide_render = False
        self.hide_viewport = False
        self.rigid_body: Any = None
        self.animation_data: FakeAnimData | None = None
        self._scene: "FakeScene | None" = None

    # -- 通用 -------------------------------------------------------------
    @property
    def matrix_world(self) -> FakeMatrix:
        local = compose_matrix(self.location, self.rotation_euler, self.scale)
        if self.parent is None:
            return local
        parent = self.parent.matrix_world
        rows = [[0.0] * 4 for _ in range(4)]
        for i in range(3):
            for j in range(3):
                rows[i][j] = sum(parent[i][k] * local[k][j] for k in range(3))
            rows[i][3] = sum(parent[i][k] * local[k][3] for k in range(3)) + parent[i][3]
        rows[3][3] = 1.0
        return FakeMatrix(rows)

    def visible_get(self) -> bool:
        return not self.hide_viewport

    def evaluated_get(self, depsgraph: Any = None) -> "FakeObject":
        return self

    @property
    def bound_box(self) -> list[tuple[float, float, float]]:
        if isinstance(self.data, FakeMeshData) and self.data._bbox is not None:
            mn, mx = self.data._bbox
        else:
            mn, mx = CHARACTER_LOCAL_MIN, CHARACTER_LOCAL_MAX
        return [
            (x, y, z) for x in (mn[0], mx[0]) for y in (mn[1], mx[1]) for z in (mn[2], mx[2])
        ]


def _character_bbox(offset: float = 0.0) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """默认角色网格的局部包围盒（可整体外扩，用于模拟描边壳）。"""
    mn = tuple(v - offset for v in CHARACTER_LOCAL_MIN)
    mx = tuple(v + offset for v in CHARACTER_LOCAL_MAX)
    return mn, mx  # type: ignore[return-value]


# =============================================================================
#  bpy.data 集合
# =============================================================================


class FakeObjects:
    """``bpy.data.objects``：new / remove / get / 迭代，并与场景联动。"""

    def __init__(self, bpy: "FakeBpy") -> None:
        self._bpy = bpy
        self._items: list[FakeObject] = []

    def new(self, name: str, data: Any = None) -> FakeObject:
        obj = FakeObject(name, data)
        obj._scene = self._bpy.context.scene
        self._items.append(obj)
        return obj

    def add(self, obj: FakeObject) -> FakeObject:  # 测试便利方法：同时链接进场景
        obj._scene = self._bpy.context.scene
        self._items.append(obj)
        if obj not in obj._scene.objects:  # noqa: SLF001
            obj._scene.objects.append(obj)  # noqa: SLF001
        return obj

    def remove(self, obj: FakeObject, do_unlink: bool = False) -> None:
        if obj in self._items:
            self._items.remove(obj)
        scene = obj._scene
        if scene is not None and obj in scene.objects:
            scene.objects.remove(obj)

    def get(self, name: str) -> FakeObject | None:
        for item in self._items:
            if item.name == name:
                return item
        return None

    def __iter__(self) -> Iterator[FakeObject]:
        return iter(list(self._items))

    def __len__(self) -> int:
        return len(self._items)


class FakeCameras:
    """``bpy.data.cameras``。"""

    def __init__(self, bpy: "FakeBpy") -> None:
        self._bpy = bpy
        self._items: list[FakeCameraData] = []

    def new(self, name: str = "Camera", **kwargs: Any) -> FakeCameraData:
        data = FakeCameraData(name=name, **kwargs)
        self._items.append(data)
        return data

    def remove(self, data: FakeCameraData, do_unlink: bool = False) -> None:
        if data in self._items:
            self._items.remove(data)

    def get(self, name: str) -> FakeCameraData | None:
        for item in self._items:
            if item.name == name:
                return item
        return None

    def __iter__(self) -> Iterator[FakeCameraData]:
        return iter(list(self._items))

    def __len__(self) -> int:
        return len(self._items)


class _CollectionObjects:
    def __init__(self, scene: "FakeScene", bpy: "FakeBpy") -> None:
        self._scene = scene
        self._bpy = bpy

    def link(self, obj: FakeObject) -> None:
        if obj not in self._scene.objects:
            self._scene.objects.append(obj)
        obj._scene = self._scene
        if obj not in self._bpy.data.objects._items:  # noqa: SLF001 - 桩内部
            self._bpy.data.objects._items.append(obj)  # noqa: SLF001


class FakeCollection:
    def __init__(self, scene: "FakeScene", bpy: "FakeBpy") -> None:
        self.objects = _CollectionObjects(scene, bpy)


# =============================================================================
#  场景
# =============================================================================


class FakeScene:
    def __init__(
        self,
        bpy: "FakeBpy | None" = None,
        capability: dict[str, list[str]] | None = None,
    ) -> None:
        self.view_settings = FakeViewSettings(capability)
        self.render = FakeRender()
        self.display_settings = FakeDisplaySettings()
        self.frame_current = 1
        self.frame_start = 1
        self.frame_end = 250
        self.name = "Scene"
        self.objects: list[FakeObject] = []
        self.camera: FakeObject | None = None
        self._bpy = bpy
        self.collection = FakeCollection(self, bpy) if bpy is not None else None


class FakeDepsgraph:
    """仅作标记；``evaluated_get`` 不需要它。"""


# =============================================================================
#  bpy.data / ops / app
# =============================================================================


class FakeImage:
    def __init__(self, name: str) -> None:
        self.name = name
        self.saved_to: list[str] = []

    def save_render(self, filepath: str, scene: Any = None) -> None:
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG_BYTES)
        self.saved_to.append(filepath)


class _ImageMap:
    def __init__(self) -> None:
        self.render_result = FakeImage("Render Result")
        self._by_name = {"Render Result": self.render_result}

    def get(self, name: str) -> FakeImage | None:
        return self._by_name.get(name)


class FakeOpsRender:
    def __init__(self, bpy: "FakeBpy") -> None:
        self._bpy = bpy

    def render(self, write_still: bool = True, **kwargs: Any) -> None:
        scene = self._bpy.context.scene
        self._bpy.render_count += 1
        self._bpy.last_render_camera = scene.camera.name if scene.camera is not None else None
        self._bpy.last_render_resolution = (
            scene.render.resolution_x,
            scene.render.resolution_y,
            scene.render.resolution_percentage,
        )
        self._bpy.render_scene_camera_history.append(self._bpy.last_render_camera)


#: 落盘的「.blend」占位内容（只要非空即可，用于断言文件确实被写出来）
BLEND_BYTES = b"BLENDER-v300RENDH" + b"\x00" * 32


class FakeOpsWm:
    """``bpy.ops.wm``：保存工程。

    复刻真实行为里**与正确性有关**的三点：
    * ``save_as_mainfile`` 之后 ``bpy.data.filepath`` 变为新路径、``is_dirty`` 变 False；
    * ``save_mainfile`` 存回**当前** ``filepath``；未保存过则报错；
    * 目标目录不存在时抛异常（服务端据此回 ``SAVE_FAILED``，而不是假装成功）。
    """

    def __init__(self, bpy: "FakeBpy") -> None:
        self._bpy = bpy

    def save_as_mainfile(self, filepath: Any = None, check_existing: bool = True, **kwargs: Any) -> None:
        self._write(str(filepath), "save_as", check_existing)

    def save_mainfile(self, check_existing: bool = True, **kwargs: Any) -> None:
        current = self._bpy.data.filepath
        if not current:
            raise RuntimeError("尚未保存过工程，save_mainfile 无目标可写。")
        self._write(current, "save_mainfile", check_existing)

    def _write(self, target: str, kind: str, check_existing: bool) -> None:
        if self._bpy.save_error is not None:
            raise RuntimeError(self._bpy.save_error)
        path = Path(target)
        if not path.parent.is_dir():
            raise RuntimeError(f"目标目录不存在：{path.parent.name}")
        path.write_bytes(BLEND_BYTES)
        self._bpy.data.filepath = str(path)
        self._bpy.data.is_dirty = False
        self._bpy.save_calls.append(
            {"kind": kind, "path": str(path), "check_existing": bool(check_existing)}
        )


class _Ops:
    def __init__(self, bpy: "FakeBpy") -> None:
        self.render = FakeOpsRender(bpy)
        self.wm = FakeOpsWm(bpy)


class _NodeGroupMap:
    def __init__(self, groups: dict[str, FakeNodeGroup]) -> None:
        self._groups = groups

    def get(self, name: str) -> FakeNodeGroup | None:
        return self._groups.get(name)


class _Data:
    def __init__(self, bpy: "FakeBpy") -> None:
        self.node_groups = _NodeGroupMap(bpy.node_groups)
        self.images = _ImageMap()
        self.filepath = ""
        self.is_dirty = False
        self.objects = FakeObjects(bpy)
        self.cameras = FakeCameras(bpy)


class _App:
    version_string = "5.2.1 LTS"
    background = False


class FakeViewLayer:
    def update(self) -> None:
        return None


class _Context:
    def __init__(self, bpy: "FakeBpy") -> None:
        self.scene = bpy._scene  # noqa: SLF001
        self.view_layer = FakeViewLayer()

    def evaluated_depsgraph_get(self) -> FakeDepsgraph:
        return FakeDepsgraph()


class FakeBpy:
    """可执行的 `bpy` 替身。"""

    def __init__(self, capability: dict[str, list[str]] | None = None) -> None:
        glare = FakeNode("Autocel_Glow", "GLARE", dict(DEFAULT_GLARE_INPUTS))
        self.node_groups: dict[str, FakeNodeGroup] = {
            "AI_Compositor": FakeNodeGroup(
                "AI_Compositor",
                [
                    FakeNode("Render Layers", "R_LAYERS"),
                    glare,
                    FakeNode("Group Output", "GROUP_OUTPUT", {"Image": [0, 0, 0, 1]}),
                ],
            )
        }
        #: 是否让 PyOpenColorIO 可用（关闭时只能退回 RNA，用于验证降级路径）
        self.ocio_available = True
        self._scene = FakeScene(self, capability)
        self.context = _Context(self)
        self.app = _App()
        self.data = _Data(self)
        self.ops = _Ops(self)
        self.render_count = 0
        self.last_render_resolution: tuple[int, int, int] | None = None
        self.last_render_camera: str | None = None
        self.render_scene_camera_history: list[str | None] = []
        #: 保存调用轨迹（供断言 check_existing=False 等）
        self.save_calls: list[dict[str, Any]] = []
        #: 测试注入：设置后任何保存都抛 RuntimeError（模拟磁盘/权限失败）
        self.save_error: str | None = None
        self.build_default_scene()

    # -- 工程（.blend）状态 ------------------------------------------------
    def set_project(self, path: str | Path, *, dirty: bool = False, create: bool = True) -> Path:
        """把桩切成「已经打开某个 .blend」的状态。

        ``dirty=True`` 模拟「磁盘上有上一次保存的版本，但内存里还有未保存改动」。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if create and not target.exists():
            target.write_bytes(BLEND_BYTES)
        self.data.filepath = str(target)
        self.data.is_dirty = bool(dirty)
        return target

    @property
    def view_settings(self) -> FakeViewSettings:
        return self.context.scene.view_settings

    # -- 默认场景（量级参照真实工程）--------------------------------------
    def build_default_scene(self) -> None:
        objects = self.data.objects

        arm = objects.add(FakeObject("model_arm", obj_type="ARMATURE"))

        mesh_data = FakeMeshData("model_body", polygons=35544)
        mesh_data._bbox = _character_bbox()  # noqa: SLF001
        body = FakeObject("model_mesh", mesh_data)
        body.material_slots = [FakeMaterialSlot(f"mat_{i:02d}") for i in range(20)]
        body.modifiers = [FakeModifier("ARMATURE", arm)]
        body.parent = arm
        objects.add(body)

        # 刚体代理（真实工程里 138 个，全部 hide_render）
        for index, bone in enumerate(("下半身A", "左足", "左ひざ")):
            proxy_data = FakeMeshData(f"proxy_{index}", polygons=40)
            proxy = FakeObject(f"{index:03d}_{bone}", proxy_data)
            proxy.material_slots = [FakeMaterialSlot("物理")]
            proxy.hide_render = True
            proxy.hide_viewport = True
            proxy.rigid_body = object()
            objects.add(proxy)

        # 描边壳（按命名排除，即使它可见且带材质槽）
        shell_data = FakeMeshData("outline_shell", polygons=35544)
        shell_data._bbox = _character_bbox(offset=0.001)  # noqa: SLF001
        shell = FakeObject("model_mesh_outline", shell_data)
        shell.material_slots = [FakeMaterialSlot("Outline")]
        objects.add(shell)

        # 默认相机：正面平视，斜后方 6 m，角色完整入画
        camera = FakeObject("Camera", FakeCameraData("Camera", lens=50.0))
        camera.location = (0.0, -6.0, 0.8)
        camera.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
        objects.add(camera)
        self._scene.camera = camera

    def use_project_camera(self, *, frame: int = 217) -> FakeObject:
        """把场景切成「真实工程那台相机」：长焦 + 大 shift + 带动画。

        实测值来自现场 Blender：lens=68.4966 / sensor_fit=VERTICAL /
        shift_x=0.40 / shift_y=-0.16，角色因此落在画面外。
        """
        scene = self._scene
        data = FakeCameraData(
            "Duo_Vertical_MMD_Camera",
            lens=68.49658966064453,
            sensor_width=36.0,
            sensor_height=24.0,
            sensor_fit="VERTICAL",
            shift_x=0.4,
            shift_y=-0.16,
            clip_start=0.1,
            clip_end=40.0,
        )
        data.animation_data = FakeAnimData(FakeAction("Duo_Vertical_MMD_Camera动作.001", fcurves=3))
        camera = FakeObject("Duo_Vertical_MMD_Camera", data)
        camera.location = (-0.2975170314311981, -1.2671719789505005, 1.3735994100570679)
        camera.rotation_euler = (1.5707963705062866, 0.0, 0.0)
        camera.animation_data = FakeAnimData(FakeAction("Duo_Vertical_MMD_Camera动作.001", fcurves=3))
        self.data.objects.add(camera)
        scene.camera = camera
        scene.frame_current = frame
        scene.frame_start = 0
        scene.frame_end = 741
        scene.render.resolution_x = 1080
        scene.render.resolution_y = 1980
        scene.render.resolution_percentage = 50
        return camera

    # -- 便利 -------------------------------------------------------------
    @property
    def scene(self) -> FakeScene:
        return self._scene

    @property
    def glare_node(self) -> FakeNode:
        group = self.node_groups["AI_Compositor"]
        node = group.nodes.get("Autocel_Glow")
        assert node is not None
        return node

    def snapshot_exposure(self) -> dict[str, Any]:
        vs = self.context.scene.view_settings
        glare = self.glare_node
        return {
            "view": {
                "exposure": vs.exposure,
                "gamma": vs.gamma,
                "view_transform": vs.view_transform,
                "look": vs.look,
            },
            "glare": {
                "Type": glare.inputs.get("Type").default_value,  # type: ignore[union-attr]
                "Quality": glare.inputs.get("Quality").default_value,  # type: ignore[union-attr]
                "Threshold": glare.inputs.get("Threshold").default_value,  # type: ignore[union-attr]
                "Smoothness": glare.inputs.get("Smoothness").default_value,  # type: ignore[union-attr]
                "Strength": glare.inputs.get("Strength").default_value,  # type: ignore[union-attr]
                "Size": glare.inputs.get("Size").default_value,  # type: ignore[union-attr]
            },
        }

    # -- 取景快照（测试断言用）--------------------------------------------
    def snapshot_framing(self) -> dict[str, Any]:
        scene = self.context.scene
        camera = scene.camera
        return {
            "frame": scene.frame_current,
            "camera": camera.name if camera is not None else None,
            "resolution": [
                scene.render.resolution_x,
                scene.render.resolution_y,
                scene.render.resolution_percentage,
            ],
            "camera_location": None if camera is None else tuple(camera.location),
            "camera_rotation": None if camera is None else tuple(camera.rotation_euler),
            "camera_lens": None if camera is None else camera.data.lens,
            "camera_shift": None if camera is None else (camera.data.shift_x, camera.data.shift_y),
        }

    def temp_camera_objects(self) -> list[FakeObject]:
        return [o for o in self.data.objects if o.name.startswith("__TOON_TUNER_PREVIEW_CAM__")]

    def temp_camera_datablocks(self) -> list[FakeCameraData]:
        return [d for d in self.data.cameras if d.name.startswith("__TEMP_CAM__")]


def _character_bbox(offset: float = 0.0) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    mn = tuple(v - offset for v in CHARACTER_LOCAL_MIN)
    mx = tuple(v + offset for v in CHARACTER_LOCAL_MAX)
    return mn, mx  # type: ignore[return-value]


# =============================================================================
#  安装 / 执行
# =============================================================================


class _FakeOCIOConfig:
    """只实现生成的代码真正用到的两个方法。"""

    def __init__(self, views: list[str], looks: list[str]) -> None:
        self._views = list(views)
        self._looks = list(looks)

    def getViews(self, display: Any = None) -> list[str]:
        return list(self._views)

    def getLookNames(self) -> list[str]:
        return list(self._looks)


class _FakeOCIO:
    def __init__(self, views: list[str], looks: list[str]) -> None:
        self._config = _FakeOCIOConfig(views, looks)

    def GetCurrentConfig(self) -> _FakeOCIOConfig:
        return self._config


def _ocio_view_names() -> list[str]:
    return list(LOOK_CAPABILITY.keys())


def _ocio_look_names() -> list[str]:
    """OCIO 的**全局** look 名单 —— 刻意是各视图合法集合的超集。

    真实 Blender 就是这样：``getLookNames()`` 会返回大量当前视图并不接受的名字。
    旧实现直接拿它当候选，于是写出了 ``enum not found``。
    """
    out: list[str] = []
    for names in LOOK_CAPABILITY.values():
        for name in names:
            if name != "None" and name not in out:
                out.append(name)
    return out


@contextlib.contextmanager
def installed(fake: FakeBpy) -> Iterator[FakeBpy]:
    """临时把桩注册为 ``bpy`` 模块，使 ``import bpy`` 命中它。"""
    module = types.ModuleType("bpy")
    module.__dict__.update(
        {
            "context": fake.context,
            "app": fake.app,
            "data": fake.data,
            "ops": fake.ops,
            "types": types.SimpleNamespace(blendermcp_server=None),
        }
    )
    previous = sys.modules.get("bpy")
    sys.modules["bpy"] = module
    previous_ocio = sys.modules.get("PyOpenColorIO")
    if fake.ocio_available:
        ocio = types.ModuleType("PyOpenColorIO")
        ocio.GetCurrentConfig = _FakeOCIO(_ocio_view_names(), _ocio_look_names()).GetCurrentConfig  # type: ignore[attr-defined]
        sys.modules["PyOpenColorIO"] = ocio
    else:
        # 模拟「OCIO 不可用」：视图列表只能退回 RNA（真实环境下 RNA 只给 NONE）
        sys.modules.pop("PyOpenColorIO", None)
    try:
        yield fake
    finally:
        if previous is not None:
            sys.modules["bpy"] = previous
        else:
            sys.modules.pop("bpy", None)
        if previous_ocio is not None:
            sys.modules["PyOpenColorIO"] = previous_ocio
        else:
            sys.modules.pop("PyOpenColorIO", None)


def run_generated_code(code: str, fake: FakeBpy) -> str:
    """执行生成的代码并返回其捕获的 stdout。"""
    buffer = io.StringIO()
    namespace: dict[str, Any] = {"__name__": "__generated__"}
    with installed(fake), contextlib.redirect_stdout(buffer):
        exec(compile(code, "<generated>", "exec"), namespace)
    return buffer.getvalue()


def extract_marker(stdout: str, marker: str) -> dict[str, Any]:
    payload = None
    for line in stdout.splitlines():
        if line.startswith(marker):
            payload = line[len(marker):]
    if payload is None:
        raise AssertionError(f"未找到 {marker} 行：{stdout!r}")
    return json.loads(payload)
