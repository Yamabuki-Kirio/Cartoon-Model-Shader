"""极简 `bpy` 桩：让服务端生成的代码在无 Blender 环境下真实执行。

这不是「假响应」，而是**真的把生成的 Python 代码跑一遍**，只是把 ``bpy`` 换成桩。
因此它能捕捉到生成代码里的逻辑错误（例如字符串被 ``list()`` 拆成字符）。

覆盖范围仅限 MVP-02 生成的代码所触达的 API。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import types
from pathlib import Path
from typing import Any, Iterator

# 一个最小合法 PNG（1x1 透明），用于校验「确实落盘了图片」
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


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


class FakeViewSettings:
    def __init__(self) -> None:
        self.view_transform = "AgX"
        self.look = "AgX - High Contrast"
        self.exposure = 0.0
        self.gamma = 1.0


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
        self.film_transparent = True
        self.filepath = ""
        self.image_settings = FakeImageSettings()


class FakeDisplaySettings:
    def __init__(self) -> None:
        self.display_device = "sRGB"


class FakeScene:
    def __init__(self) -> None:
        self.view_settings = FakeViewSettings()
        self.render = FakeRender()
        self.display_settings = FakeDisplaySettings()
        self.frame_current = 1
        self.name = "Scene"


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
        self._bpy.render_count += 1
        self._bpy.last_render_resolution = (
            self._bpy.context.scene.render.resolution_x,
            self._bpy.context.scene.render.resolution_y,
            self._bpy.context.scene.render.resolution_percentage,
        )


class _Ops:
    def __init__(self, bpy: "FakeBpy") -> None:
        self.render = FakeOpsRender(bpy)


class _Data:
    def __init__(self, bpy: "FakeBpy") -> None:
        self.node_groups = _NodeGroupMap(bpy.node_groups)
        self.images = _ImageMap()


class _NodeGroupMap:
    def __init__(self, groups: dict[str, FakeNodeGroup]) -> None:
        self._groups = groups

    def get(self, name: str) -> FakeNodeGroup | None:
        return self._groups.get(name)


class _App:
    version_string = "5.2.1 LTS"
    background = False


class FakeBpy:
    """可执行的 `bpy` 替身。"""

    def __init__(self) -> None:
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
        self.context = types.SimpleNamespace(scene=FakeScene())
        self.app = _App()
        self.data = _Data(self)
        self.ops = _Ops(self)
        self.render_count = 0
        self.last_render_resolution: tuple[int, int, int] | None = None

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
    # PyOpenColorIO 必须不可用，以走「静态回退」分支
    previous_ocio = sys.modules.pop("PyOpenColorIO", None)
    try:
        yield fake
    finally:
        if previous is not None:
            sys.modules["bpy"] = previous
        else:
            sys.modules.pop("bpy", None)
        if previous_ocio is not None:
            sys.modules["PyOpenColorIO"] = previous_ocio


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
