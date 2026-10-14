"""渲染方案系统回归测试（PR #8 规格 §八）。

五组覆盖：

1. **注册表**：固定映射、未知 id、默认方案、清单不含路径、版本号；
2. **参数校验**：只读项拒绝提交、未知参数拒绝、越界不夹取、默认值填充；
3. **场景生命周期**：用 Fake ``bpy`` 真跑 ``apply()`` / ``restore()``，
   锁住灯名与数量、重复执行不累计对象、恢复后原灯/世界/合成器还原；
4. **任务与进程**：去重指纹包含方案 id / 版本 / 规范化参数；适配器命令行
   只发固定方案 id；
5. **Web**：见 ``web/src/features/render/RenderWizard.test.tsx``（前端负责，
   此处只守住后端契约）。

⚠ 这里**没有真实 Blender**：``apply()`` 全部在 Fake ``bpy`` 上执行，
因此测试只能证明「结构正确、幂等、可恢复」，**不能**证明真机渲染效果。
真机验收矩阵见 ``docs/渲染方案_真机验收矩阵.md``。
"""

from __future__ import annotations

import json
import math
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "render_pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))


# =============================================================================
#  极简 mathutils / bpy 桩
# =============================================================================


class _Vec:
    """够用的 3D 向量：算术 + copy + to_track_quat().to_euler()。"""

    __slots__ = ("x", "y", "z")

    def __init__(self, v: Any = (0.0, 0.0, 0.0)) -> None:
        if isinstance(v, _Vec):
            v = (v.x, v.y, v.z)
        if isinstance(v, (int, float)):
            v = (float(v), float(v), float(v))
        seq = list(v)
        while len(seq) < 3:
            seq.append(0.0)
        self.x, self.y, self.z = (float(seq[0]), float(seq[1]), float(seq[2]))

    def __add__(self, other: Any) -> "_Vec":
        o = _Vec(other)
        return _Vec((self.x + o.x, self.y + o.y, self.z + o.z))

    def __sub__(self, other: Any) -> "_Vec":
        o = _Vec(other)
        return _Vec((self.x - o.x, self.y - o.y, self.z - o.z))

    def __mul__(self, k: float) -> "_Vec":
        return _Vec((self.x * float(k), self.y * float(k), self.z * float(k)))

    __rmul__ = __mul__

    def copy(self) -> "_Vec":
        return _Vec((self.x, self.y, self.z))

    def to_track_quat(self, *_a: Any, **_k: Any) -> Any:
        # 只需要一个能 `.to_euler()` 的东西；数值本身对结构测试无意义。
        return types.SimpleNamespace(to_euler=lambda: (0.0, 0.0, 0.0))

    def __iter__(self):
        return iter((self.x, self.y, self.z))

    def __eq__(self, other: Any) -> bool:
        try:
            o = _Vec(other)
        except Exception:  # noqa: BLE001
            return NotImplemented
        return (self.x, self.y, self.z) == (o.x, o.y, o.z)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Vec({self.x:.3f},{self.y:.3f},{self.z:.3f})"


class _Matrix:
    """占位矩阵：只被赋值/取 copy/inverted，不参与计算。"""

    def copy(self) -> "_Matrix":
        return _Matrix()

    def inverted(self) -> "_Matrix":
        return _Matrix()


class _PropDict(dict):
    """支持 obj["自定义属性"] = ... 的对象基类。"""

    def __setitem__(self, key: str, value: Any) -> None:
        dict.__setitem__(self, key, value)

    def __getitem__(self, key: str) -> Any:
        return dict.__getitem__(self, key)


def _make_bpy() -> types.SimpleNamespace:
    """构造一个够用的 `bpy` 桩（覆盖 schemes/* 实际调用的 API）。"""

    class Obj(_PropDict):
        def __init__(self, name: str, data: Any = None, obj_type: str = "LIGHT") -> None:
            super().__init__()
            self.name = name
            self.data = data
            self.type = obj_type
            self.hide_viewport = False
            self.hide_render = False
            self.hide_select = False
            self.location = _Vec()
            self.rotation_euler = (0.0, 0.0, 0.0)
            self.rotation_mode = "XYZ"
            self.scale = (1.0, 1.0, 1.0)
            self.dimensions = (1.0, 1.0, 1.0)
            self.parent = None
            self.matrix_world = _Matrix()
            self.matrix_parent_inverse = _Matrix()
            self.matrix_basis = _Matrix()
            self.display_type = "SOLID"
            self.empty_display_type = "PLAIN_AXES"
            self.empty_display_size = 1.0
            self.show_in_front = False
            self.users_collection: list[Any] = []

        def __repr__(self) -> str:  # pragma: no cover
            return f"<Obj {self.name} {self.type}>"

    class LightData:
        def __init__(self, name: str, light_type: str) -> None:
            self.name = name
            self.type = light_type
            self.energy = 0.0
            self.color = (1.0, 1.0, 1.0)
            self.use_shadow = True
            self.shadow_soft_size = 0.0
            self.size = 0.0
            self.shape = "DISK"
            self.spot_size = 1.0
            self.spot_blend = 0.0

    class Node:
        def __init__(self, bl_idname: str) -> None:
            self.bl_idname = bl_idname
            self.operation = None
            self.noise_dimensions = "3D"
            self.inputs = _Inputs()
            self.outputs = _Outputs()

    class _Sock:
        def __init__(self) -> None:
            self.default_value: Any = None

    class _Inputs:
        def __init__(self) -> None:
            self._d: dict[str, Any] = {}

        def __getitem__(self, key: str) -> Any:
            return self._d.setdefault(key, _Sock())

        def __contains__(self, key: str) -> bool:
            return True

    class _Outputs:
        def __init__(self) -> None:
            self._d: dict[str, Any] = {}

        def __getitem__(self, key: str) -> Any:
            return self._d.setdefault(key, object())

    class NodeList(list):
        def new(self, bl_idname: str) -> Node:
            node = Node(bl_idname)
            self.append(node)
            return node

        def clear(self) -> None:
            del self[:]

    class LinkTree:
        def __init__(self) -> None:
            self.links: list[tuple[Any, Any]] = []

        def new(self, a: Any, b: Any) -> None:
            self.links.append((a, b))

    class NodeTree:
        def __init__(self) -> None:
            self.nodes = NodeList()
            self.links = LinkTree()

    class World:
        def __init__(self, name: str) -> None:
            self.name = name
            self.use_nodes = False
            self.node_tree = NodeTree()

    class Material:
        def __init__(self, name: str) -> None:
            self.name = name
            self.use_nodes = False
            self.node_tree = NodeTree()
            self.blend_method = "OPAQUE"

    class Mesh:
        def __init__(self) -> None:
            self.materials: list[Any] = []

    class Collection(_PropDict):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name
            self.objects = _ObjList(self)
            self.children = _CollChildren()

    class _ObjList(list):
        """对象列表。``_owner`` 是它所属的 Collection（用于维护 users_collection）。

        ``scene.objects`` 不属于任何集合，``_owner=None`` 时只做列表语义。
        """

        def __init__(self, owner: Any = None) -> None:
            super().__init__()
            self._owner = owner

        def link(self, obj: Any) -> None:
            if obj not in self:
                self.append(obj)
            if self._owner is not None and self._owner not in obj.users_collection:
                obj.users_collection.append(self._owner)

        def unlink(self, obj: Any) -> None:
            while obj in self:
                self.remove(obj)
            if self._owner is not None:
                while self._owner in obj.users_collection:
                    obj.users_collection.remove(self._owner)

        def get(self, name: str) -> Any:
            for obj in self:
                if getattr(obj, "name", None) == name:
                    return obj
            return None

    class _CollChildren(list):
        def link(self, coll: Any) -> None:
            self.append(coll)

    class _NamedStore:
        def __init__(self, factory: Any = None) -> None:
            self._d: dict[str, Any] = {}
            self._factory = factory
            #: 删除对象时要顺带从哪些列表里摘掉（模拟 Blender 的 do_unlink）。
            self._unlink_from: list[Any] = []

        def get(self, name: str) -> Any:
            # 先查索引；再按当前 name 扫一遍 —— 真实 Blender 里改名后仍能按新名取到，
            # 而我们的桩在 ``obj.name = X`` 时不会回写索引。
            if name in self._d:
                return self._d[name]
            for obj in self._d.values():
                if getattr(obj, "name", None) == name:
                    return obj
            return None

        def new(self, name: str, *args: Any) -> Any:
            obj = self._factory(name, *args) if self._factory else types.SimpleNamespace(name=name)
            self._d[name] = obj
            return obj

        def remove(self, obj: Any, do_unlink: bool = True) -> None:
            # 按**对象身份**删，而不是按 key —— 对象可能已改名。
            for key, value in list(self._d.items()):
                if value is obj:
                    self._d.pop(key, None)
            self._d.pop(getattr(obj, "name", str(obj)), None)
            if do_unlink:
                for holder in self._unlink_from:
                    while obj in holder:
                        holder.remove(obj)

        def __iter__(self):
            return iter(list(self._d.values()))

        def __len__(self) -> int:
            return len(self._d)

    class Scene:
        def __init__(self) -> None:
            self.objects = _ObjList()
            self.world: Any = None
            self.camera: Any = None
            self.use_nodes = False
            self.collection = Collection("Scene Collection")
            self.eevee = types.SimpleNamespace(use_volumetric_lights=False)

    class Ops:
        def __init__(self, ctx: Any) -> None:
            self._ctx = ctx

        class _Mesh:
            def __init__(self, ctx: Any) -> None:
                self._ctx = ctx

            def primitive_cube_add(self, location: Any = None, **_k: Any) -> None:
                obj = Obj("Cube", Mesh(), obj_type="MESH")
                # 真实 Blender 会把它同时加进 bpy.data.objects 与当前集合。
                self._ctx.data.objects._d["Cube"] = obj
                obj.location = _Vec(location) if location is not None else _Vec()
                self._ctx.scene.objects.append(obj)
                # 默认落在 Scene Collection 里（真实 Blender 行为），方案会 unlink 再挂到灯组。
                self._ctx.scene.collection.objects.link(obj)
                self._ctx.object = obj

        class _Obj:
            def __init__(self, ctx: Any) -> None:
                self._ctx = ctx

            def transform_apply(self, **_k: Any) -> None:
                obj = self._ctx.object
                if obj is not None:
                    obj.scale = (1.0, 1.0, 1.0)

        mesh = None  # 运行时替换
        object = None  # 运行时替换

    class Context:
        def __init__(self) -> None:
            self.scene = Scene()
            self.object: Any = None
            self.ops = Ops(self)
            self.ops.mesh = Ops._Mesh(self)
            self.ops.object = Ops._Obj(self)

    ctx = Context()

    class Data:
        def __init__(self) -> None:
            self._objects = _NamedStore()
            self._objects._factory = _mk_object
            self.objects = self._objects
            self.lights = _NamedStore(LightData)
            self.materials = _NamedStore(Material)
            self.collections = _NamedStore(Collection)
            self.worlds = _NamedStore(World)
            self.cameras = _NamedStore()

    def _mk_object(name: str, data: Any = None) -> Any:
        # ★ 关键口径：``obj.type`` 是 **"LIGHT"**，而 ``data.type`` 才是 "POINT"/"SPOT"/…
        #   方案代码按 ``obj.type == "LIGHT"`` 判断（与真实 Blender 一致），
        #   所以这里绝不能把 data.type 当成 obj.type。
        if isinstance(data, LightData):
            obj_type = "LIGHT"
        elif isinstance(data, Mesh):
            obj_type = "MESH"
        elif data is None:
            obj_type = "EMPTY"
        else:
            obj_type = "EMPTY"
        obj = Obj(name, data, obj_type=obj_type)
        return obj

    # 删除对象时必须一并从 scene.objects 摘掉（真实 Blender 的 do_unlink 语义）。
    ctx.data = Data()
    ctx.data.objects._unlink_from.append(ctx.scene.objects)

    return types.SimpleNamespace(
        data=ctx.data,
        context=ctx,
        ops=ctx.ops,
        _Obj=Obj,
        _LightData=LightData,
        _Collection=Collection,
        _Mesh=Mesh,
        _World=World,
        _Material=Material,
    )


def _install_fake_bpy(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """把 fake `bpy` 与 `mathutils.Vector` 装进 sys.modules。"""
    fake = _make_bpy()
    mod = types.ModuleType("bpy")
    for key in ("data", "context", "ops"):
        setattr(mod, key, getattr(fake, key))
    # 有些实现会 `from bpy import context`。
    mod.__dict__.update({"data": fake.data, "context": fake.context, "ops": fake.ops})

    mu = types.ModuleType("mathutils")
    mu.Vector = _Vec
    mu.Matrix = _Matrix
    mu.Euler = lambda v: tuple(v)

    monkeypatch.setitem(sys.modules, "bpy", mod)
    monkeypatch.setitem(sys.modules, "mathutils", mu)
    return fake


# =============================================================================
#  1. 注册表
# =============================================================================


def test_registry_is_fixed_and_complete() -> None:
    from schemes import registry

    assert set(registry.RENDER_SCHEMES) == {"classic", "volume_fog"}
    assert registry.DEFAULT_SCHEME_ID in registry.RENDER_SCHEMES
    for sid, cls in registry.RENDER_SCHEMES.items():
        # 键与类 id 必须一致：防止有人改了类属性忘了改表。
        assert cls.id == sid
        assert isinstance(cls.version, int) and cls.version >= 1
        assert cls.label and cls.description


def test_registry_unknown_scheme_reports_stable_code() -> None:
    from schemes import registry
    from schemes.base import ERROR_INVALID_SCHEME, SchemeError

    for bad in ("nope", "", "classic2", "../classic", "os.system"):
        with pytest.raises(SchemeError) as exc:
            registry.get_scheme(bad)
        assert exc.value.code == ERROR_INVALID_SCHEME


def test_registry_list_puts_default_first() -> None:
    from schemes import registry

    ids = [item["id"] for item in registry.list_schemes()]
    assert ids[0] == registry.DEFAULT_SCHEME_ID
    assert set(ids) == set(registry.RENDER_SCHEMES)


def test_scheme_public_description_has_no_paths_or_code() -> None:
    """规格 §一：对外清单不得含脚本路径 / 模块路径 / 命令片段。"""
    from schemes import registry

    blob = json.dumps(registry.list_schemes(), ensure_ascii=False).lower()
    for banned in (".py", "/usr/", "\\\\", "import ", "subprocess", "bpy.ops"):
        assert banned not in blob, banned
    for item in registry.list_schemes():
        assert set(item) >= {
            "id", "version", "label", "description", "recommended",
            "recommended_use", "has_volume_fog", "performance_note",
            "adjustable_count", "parameters",
        }


def test_volume_fog_only_exposes_fog_density() -> None:
    """规格裁决：第一版只开放 fog_density，其余只读。"""
    from schemes import registry

    desc = registry.describe_scheme("volume_fog")
    adjustable = [p["id"] for p in desc["parameters"] if not p["readonly"]]
    readonly = [p["id"] for p in desc["parameters"] if p["readonly"]]
    assert adjustable == ["fog_density"]
    assert set(readonly) == {
        "light_intensity", "cool_rim_intensity", "warm_rim_intensity", "black_world"
    }
    assert desc["adjustable_count"] == 1


def test_classic_has_no_adjustable_params() -> None:
    from schemes import registry

    desc = registry.describe_scheme("classic")
    assert desc["parameters"] == []
    assert desc["adjustable_count"] == 0


def test_legacy_lighting_maps_to_scheme() -> None:
    """旧字段翻译 + 弃用提示（规格 §五）。"""
    from schemes import registry

    sid, note = registry.resolve_scheme_id(lighting="legacy")
    assert sid == "classic"
    assert note and "弃用" in note

    sid2, note2 = registry.resolve_scheme_id(lighting="volume_fog")
    assert sid2 == "volume_fog"
    assert note2 and "弃用" in note2


def test_conflicting_new_and_old_fields_are_rejected() -> None:
    """新旧同时出现且语义不同 → 拒绝（规格 §五）。"""
    from schemes import registry
    from schemes.base import SchemeError

    with pytest.raises(SchemeError):
        registry.resolve_scheme_id(scheme="classic", lighting="volume_fog")
    # 语义一致则放行，但仍给弃用提示。
    sid, note = registry.resolve_scheme_id(scheme="classic", lighting="legacy")
    assert sid == "classic"
    assert note


def test_legacy_lighting_rejects_unknown_value() -> None:
    from schemes import registry
    from schemes.base import ERROR_INVALID_SCHEME, SchemeError

    with pytest.raises(SchemeError) as exc:
        registry.resolve_scheme_id(lighting="neon")
    assert exc.value.code == ERROR_INVALID_SCHEME


# =============================================================================
#  2. 参数校验
# =============================================================================


def test_normalize_fills_defaults() -> None:
    from schemes import registry

    assert registry.normalize_parameters("volume_fog", {}) == {"fog_density": 0.018}


def test_normalize_rejects_unknown_param() -> None:
    """规格：未知参数**拒绝**，不是静默忽略。"""
    from schemes import registry
    from schemes.base import ERROR_INVALID_PARAM, SchemeError

    with pytest.raises(SchemeError) as exc:
        registry.normalize_parameters("volume_fog", {"nope": 1})
    assert exc.value.code == ERROR_INVALID_PARAM


def test_normalize_rejects_readonly_param_submission() -> None:
    """只读项不得提交（未经真实 Blender 标定）。"""
    from schemes import registry
    from schemes.base import ERROR_INVALID_PARAM, SchemeError

    for readonly_id in ("light_intensity", "cool_rim_intensity",
                        "warm_rim_intensity", "black_world"):
        with pytest.raises(SchemeError) as exc:
            registry.normalize_parameters("volume_fog", {readonly_id: 999})
        assert exc.value.code == ERROR_INVALID_PARAM, readonly_id


def test_normalize_does_not_clamp_out_of_range() -> None:
    """越界**不夹取**：宁可报错，也不悄悄改成别的值。"""
    from schemes import registry
    from schemes.base import ERROR_INVALID_PARAM, SchemeError

    for bad in (-1.0, 0.5, 1e9):
        with pytest.raises(SchemeError) as exc:
            registry.normalize_parameters("volume_fog", {"fog_density": bad})
        assert exc.value.code == ERROR_INVALID_PARAM, bad


def test_normalize_accepts_boundary_values() -> None:
    from schemes import registry

    assert registry.normalize_parameters(
        "volume_fog", {"fog_density": 0.0}
    ) == {"fog_density": 0.0}
    assert registry.normalize_parameters(
        "volume_fog", {"fog_density": 0.1}
    ) == {"fog_density": 0.1}


def test_normalize_rejects_wrong_type() -> None:
    from schemes import registry
    from schemes.base import ERROR_INVALID_PARAM, SchemeError

    for bad in ("0.02", None, [0.02], {"v": 1}, True):
        with pytest.raises(SchemeError) as exc:
            registry.normalize_parameters("volume_fog", {"fog_density": bad})
        assert exc.value.code == ERROR_INVALID_PARAM, bad


def test_classic_rejects_volume_fog_params() -> None:
    """经典方案没有参数：提交任何参数都应被拒绝（防止误带体积雾参数）。"""
    from schemes import registry
    from schemes.base import ERROR_INVALID_PARAM, SchemeError

    with pytest.raises(SchemeError) as exc:
        registry.normalize_parameters("classic", {"fog_density": 0.03})
    assert exc.value.code == ERROR_INVALID_PARAM


def test_param_spec_self_check_catches_bad_definitions() -> None:
    from schemes.base import ParamSpec

    with pytest.raises(Exception):
        ParamSpec(id="x", kind="number", label="x", default=1.0)  # 缺 minimum/maximum
    with pytest.raises(Exception):
        ParamSpec(id="x", kind="enum", label="x", default="a")  # 缺 choices
    with pytest.raises(Exception):
        ParamSpec(id="x", kind="bogus", label="x", default=1)  # kind 非法
    with pytest.raises(Exception):
        # 默认值越界
        ParamSpec(id="x", kind="number", label="x", default=5.0, minimum=0.0, maximum=1.0)


# =============================================================================
#  3. 场景生命周期（Fake bpy）
# =============================================================================


def _seed_scene(fake: types.SimpleNamespace) -> Any:
    """造一个「已有原场景灯 + 世界 + 合成器」的场景，用于验证恢复。"""
    scene = fake.context.scene
    # 两盏原灯（只停用不删除）。
    # ★ 必须同时进 bpy.data.objects 与 scene.objects —— 真实 Blender 里
    #   ``bpy.data.objects`` 是全局表，方案与快照都按名字从这里取。
    for name in ("OLD_LIGHT_A", "OLD_LIGHT_B"):
        data = fake.data.lights.new(name, "POINT")
        obj = fake.data.objects.new(name, data)
        scene.objects.append(obj)
        scene.collection.objects.link(obj)
    # 原世界
    world = fake.data.worlds.new("OriginalWorld")
    scene.world = world
    # 合成器 + 体积设置
    scene.use_nodes = True
    scene.eevee.use_volumetric_lights = True
    return scene


def test_classic_apply_creates_expected_lights(monkeypatch: pytest.MonkeyPatch) -> None:
    """经典方案：灯名与数量来自主脚本字面量表，重构不得漂移。

    这里只锁**结构性事实**（数量一致、名字来自表），不锁具体数值 ——
    数值唯一来源是 ``一键卡通渲染.py`` 的 LIGHTS_ACTIVE / LIGHTS_SPARE。
    """
    fake = _install_fake_bpy(monkeypatch)
    from schemes import registry
    from schemes.classic import build_light_specs

    active, spare = build_light_specs()
    assert active, "应能从主脚本提取 LIGHTS_ACTIVE"
    assert spare, "应能从主脚本提取 LIGHTS_SPARE"

    scene = _seed_scene(fake)
    result = registry.apply_scheme(
        scene, [], {}, scheme_id="classic"
    )
    # ``result.lights`` 是本次创建的灯光**对象**（供结束后精确删除）。
    names = {obj.name for obj in result.lights}
    expected = {row[0] for row in active} | {row[0] for row in spare}
    assert names == expected
    assert result.scheme_id == "classic"
    assert result.version == registry.get_scheme("classic").version


def test_classic_apply_does_not_touch_original_lights(monkeypatch: pytest.MonkeyPatch) -> None:
    """经典方案不隐藏原灯（它本来就是原灯方案），也不建体积雾。"""
    fake = _install_fake_bpy(monkeypatch)
    from schemes import registry

    scene = _seed_scene(fake)
    registry.apply_scheme(scene, [], {}, scheme_id="classic")

    # 原灯仍在场景里（经典方案就是原灯方案，不隐藏它们）
    assert scene.objects.get("OLD_LIGHT_A") is not None
    assert scene.objects.get("OLD_LIGHT_A").hide_render is False
    # 没有体积雾集合
    assert fake.data.collections.get("TOON_体积雾灯光组") is None


def test_volume_fog_apply_creates_lights_and_fog(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_bpy(monkeypatch)
    from schemes import registry
    from schemes.volume_fog import (
        FOG_MATERIAL_NAME,
        FOG_OBJECT_NAME,
        VOLUME_FOG_COLLECTION,
        VOLUME_FOG_CONTROLLER,
    )

    scene = _seed_scene(fake)
    result = registry.apply_scheme(
        scene, [], {"fog_density": 0.03}, scheme_id="volume_fog"
    )

    # 7 盏灯，且名字统一前缀
    assert len(result.lights) == 7
    assert all(obj.name.startswith("LGT_") for obj in result.lights)
    # 集合 / 总控 / 雾对象 / 雾材质都在
    coll = fake.data.collections.get(VOLUME_FOG_COLLECTION)
    assert coll is not None
    assert fake.data.objects.get(VOLUME_FOG_CONTROLLER) is not None
    assert fake.data.objects.get(FOG_OBJECT_NAME) is not None
    assert fake.data.materials.get(FOG_MATERIAL_NAME) is not None
    # 原灯被停用（不删除）
    for name in ("OLD_LIGHT_A", "OLD_LIGHT_B"):
        obj = fake.data.objects.get(name)
        assert obj is not None, name
        assert obj.hide_render is True and obj.hide_viewport is True
    # 黑色无环境光世界
    assert scene.world is not None and scene.world.name != "OriginalWorld"
    assert result.parameters["fog_density"] == 0.03


def test_volume_fog_apply_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """规格 §八：重复执行不累计对象。"""
    fake = _install_fake_bpy(monkeypatch)
    from schemes import registry
    from schemes.volume_fog import FOG_OBJECT_NAME, VOLUME_FOG_COLLECTION

    scene = _seed_scene(fake)
    first = registry.apply_scheme(scene, [], {}, scheme_id="volume_fog")
    second = registry.apply_scheme(scene, [], {}, scheme_id="volume_fog")

    assert len(second.lights) == len(first.lights) == 7
    # 灯组集合只有一个，且对象数没翻倍：
    #   7 灯 + 1 总控 Empty + 1 体积雾 mesh = 9
    colls = [c for c in fake.data.collections if c.name == VOLUME_FOG_COLLECTION]
    assert len(colls) == 1
    assert len(colls[0].objects) == 9
    # 雾对象只有一个
    fogs = [o for o in fake.data.objects if o.name == FOG_OBJECT_NAME]
    assert len(fogs) == 1


def test_restore_returns_scene_to_original_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """规格 §四：结束/失败/取消后必须恢复并回读确认。"""
    fake = _install_fake_bpy(monkeypatch)
    from schemes import registry

    scene = _seed_scene(fake)
    original_world = scene.world
    original_nodes = scene.use_nodes
    original_vol = scene.eevee.use_volumetric_lights

    result = registry.apply_scheme(scene, [], {}, scheme_id="volume_fog")
    # 应用后：世界被换掉、原灯被停用
    assert scene.world is not original_world

    registry.restore_scheme(result.snapshot)

    # 世界回到原对象（同一对象，不是「同名新建」）
    assert scene.world is original_world
    assert scene.use_nodes == original_nodes
    assert scene.eevee.use_volumetric_lights == original_vol
    # 原灯可见性还原
    for name in ("OLD_LIGHT_A", "OLD_LIGHT_B"):
        obj = fake.data.objects.get(name)
        assert obj.hide_render is False and obj.hide_viewport is False, name
    # 方案临时对象 / 集合 / 材质被清掉
    from schemes.volume_fog import (
        FOG_MATERIAL_NAME,
        FOG_OBJECT_NAME,
        VOLUME_FOG_COLLECTION,
        VOLUME_FOG_CONTROLLER,
    )

    assert fake.data.collections.get(VOLUME_FOG_COLLECTION) is None
    assert fake.data.objects.get(FOG_OBJECT_NAME) is None
    assert fake.data.objects.get(VOLUME_FOG_CONTROLLER) is None
    assert fake.data.materials.get(FOG_MATERIAL_NAME) is None


def test_restore_reports_failure_with_stable_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """回读不通过时必须报 RENDER_SCHEME_RESTORE_FAILED，而不是静默成功。"""
    fake = _install_fake_bpy(monkeypatch)
    from schemes import registry
    from schemes.base import ERROR_RESTORE_FAILED, SchemeError

    scene = _seed_scene(fake)
    result = registry.apply_scheme(scene, [], {}, scheme_id="volume_fog")

    # 让快照声称记得某个「原本存在、现在却找不到」的灯 —— 恢复时必然回读失败。
    result.snapshot.light_flags["GHOST_LIGHT"] = [False, False]
    # 同时把原世界名改成不存在的名字，也会触发回读失败。
    result.snapshot.world_name = "NoSuchWorld"

    with pytest.raises(SchemeError) as exc:
        registry.restore_scheme(result.snapshot)
    assert exc.value.code == ERROR_RESTORE_FAILED
    assert "GHOST_LIGHT" in str(exc.value) or "NoSuchWorld" in str(exc.value)


def test_snapshot_summary_has_no_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """快照摘要会进 run_manifest：绝不能带本机路径。"""
    fake = _install_fake_bpy(monkeypatch)
    from schemes import registry

    scene = _seed_scene(fake)
    result = registry.apply_scheme(scene, [], {}, scheme_id="volume_fog")
    blob = json.dumps(result.snapshot.summary(), ensure_ascii=False)
    assert "C:\\" not in blob and "C:/" not in blob and "/home/" not in blob


# =============================================================================
#  4. 任务与进程
# =============================================================================


def test_dedup_key_includes_scheme_identity() -> None:
    """规格 §五：去重指纹必须含模型身份、方案 id、方案版本、规范化方案参数、
    画面模式、辉光参数、自动构图设置。"""
    from src.server.render_jobs import RenderJobManager

    base = {
        "mode": "faithful",
        "auto_frame": False,
        "scheme": "volume_fog",
        "scheme_version": 1,
        "scheme_parameters": {"fog_density": 0.018},
    }
    key_base = RenderJobManager.dedup_key("model-fp", base)

    # 换方案 id → 不同
    assert RenderJobManager.dedup_key(
        "model-fp", {**base, "scheme": "classic", "scheme_parameters": {}}
    ) != key_base
    # 换方案版本 → 不同
    assert RenderJobManager.dedup_key(
        "model-fp", {**base, "scheme_version": 2}
    ) != key_base
    # 换方案参数 → 不同
    assert RenderJobManager.dedup_key(
        "model-fp", {**base, "scheme_parameters": {"fog_density": 0.05}}
    ) != key_base
    # 换画面模式 → 不同
    assert RenderJobManager.dedup_key(
        "model-fp", {**base, "mode": "enhanced"}
    ) != key_base
    # 换自动构图 → 不同
    assert RenderJobManager.dedup_key(
        "model-fp", {**base, "auto_frame": True}
    ) != key_base
    # 换辉光参数 → 不同
    assert RenderJobManager.dedup_key(
        "model-fp", {**base, "glow_strength": 0.9}
    ) != key_base
    # 换模型身份 → 不同
    assert RenderJobManager.dedup_key("other-fp", base) != key_base
    # 完全一致 → 相同（否则「同设置复用」就失效了）
    assert RenderJobManager.dedup_key("model-fp", dict(base)) == key_base


def test_dedup_key_is_order_independent() -> None:
    from src.server.render_jobs import RenderJobManager

    a = {"mode": "faithful", "scheme": "classic", "scheme_parameters": {"fog_density": 0.01}}
    b = {"scheme": "classic", "scheme_parameters": {"fog_density": 0.01}, "mode": "faithful"}
    assert RenderJobManager.dedup_key("fp", a) == RenderJobManager.dedup_key("fp", b)


def test_adapter_command_line_only_sends_scheme_id() -> None:
    """适配器命令行只发白名单方案 id 与序列化后的参数 JSON。

    源码级守卫：防止有人把任何形式的路径/脚本/命令片段塞回命令行。
    """
    text = (ROOT / "src" / "server" / "render_pipeline_adapter.py").read_text(
        encoding="utf-8"
    )
    assert '"--scheme",' in text
    assert '"--scheme-params",' in text
    # 旧的灯光开关不得再下发（避免双写）
    assert '"--lighting",' not in text
    assert '"--fog-density",' not in text


def test_scheme_bridge_rejects_path_like_ids() -> None:
    from src.server import errors
    from src.server.scheme_bridge import resolve_scheme_request

    with pytest.raises(errors.ToonTunerError) as exc:
        resolve_scheme_request({"scheme": "../../evil.py"})
    assert exc.value.code == errors.RENDER_SCHEME_INVALID


def test_scheme_error_codes_are_registered_stable() -> None:
    """稳定错误码必须真的在 errors 表里（否则会被降级成 INTERNAL_ERROR）。"""
    from src.server import errors

    for code in (
        "RENDER_SCHEME_INVALID",
        "RENDER_SCHEME_PARAM_INVALID",
        "RENDER_SCHEME_APPLY_FAILED",
        "RENDER_SCHEME_RESTORE_FAILED",
    ):
        err = errors.ToonTunerError(code, "x")
        assert err.code == code, code


def test_schemes_api_payload_shape() -> None:
    from src.server.scheme_bridge import schemes_payload

    payload = schemes_payload()
    assert set(payload) == {"default", "schemes"}
    assert payload["default"] in {s["id"] for s in payload["schemes"]}


def test_scheme_description_never_leaks_paths() -> None:
    """规格 §一：对外清单不得含本机路径（bridge 层还有一道断言）。"""
    from src.server.scheme_bridge import scheme_public_list

    blob = json.dumps(scheme_public_list(), ensure_ascii=False).lower()
    for banned in (":\\", ":/", ".py", "/usr/", "\\\\"):
        assert banned not in blob, banned


def test_math_is_finite_in_extracted_tables() -> None:
    """锁住「经典方案数值来自主脚本」这一条：表里的数必须是有限字面量。"""
    from schemes.classic import build_light_specs

    active, spare = build_light_specs()
    for row in list(active) + list(spare):
        assert isinstance(row, (list, tuple)) and len(row) >= 2
        name = row[0]
        assert isinstance(name, str) and name
    assert len(active) >= 1 and len(spare) >= 1
    # 数值字段必须是数字（不是字符串拼接出来的）
    for row in active:
        numeric = [v for v in row[1:] if isinstance(v, (int, float))]
        assert numeric, row
        assert all(math.isfinite(float(v)) for v in numeric), row


# =============================================================================
#  方案层不得拥有执行权（规格 §一：禁止动态 import / 反射 / 命令片段）
# =============================================================================


#: 方案层出现即视为违规的模块（导入它们等于拿回执行权）。
_BANNED_MODULES = {
    "importlib",
    "subprocess",
    "ctypes",
    "pickle",
    "marshal",
    "runpy",
    "shutil",
}

#: 直接构造执行能力的内建函数。
_BANNED_BUILTINS = {"eval", "exec", "compile", "__import__"}

#: ``os`` 上属于「起进程 / 起 shell」的属性前缀。
_BANNED_OS_ATTRS = ("system", "popen", "spawn", "exec", "fork", "kill")


def _scheme_sources() -> list[tuple[str, str]]:
    """列出 ``render_pipeline/schemes/`` 下所有模块的 (名字, 源码)。"""
    root = PIPELINE / "schemes"
    out: list[tuple[str, str]] = []
    for path in sorted(root.glob("*.py")):
        out.append((path.name, path.read_text(encoding="utf-8")))
    assert out, "没有找到任何方案层模块，测试本身失效了"
    return out


def test_scheme_layer_has_no_dynamic_execution() -> None:
    """方案层源码里不得出现动态 import / eval / exec / 起进程。

    这条守卫的由来：``classic.build_light_specs()`` 早期版本用
    ``importlib.util.spec_from_file_location`` 按路径加载主脚本（虽然没真的
    ``exec_module``）。那既违反「写死 id → 实现类映射」的固定注册表要求，
    也让方案层重新拥有了「加载并执行任意 Python 文件」的能力。现在取值一律走
    AST 静态读取，本用例负责防止它悄悄回潮。
    """
    import ast

    violations: list[str] = []
    for name, source in _scheme_sources():
        tree = ast.parse(source, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _BANNED_MODULES:
                        violations.append("%s:%d import %s" % (name, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in _BANNED_MODULES:
                    violations.append("%s:%d from %s import ..." % (name, node.lineno, root))
            elif isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id in _BANNED_BUILTINS:
                    violations.append("%s:%d 调用 %s()" % (name, node.lineno, fn.id))
                if (
                    isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "os"
                    and fn.attr.startswith(_BANNED_OS_ATTRS)
                ):
                    violations.append("%s:%d 调用 os.%s()" % (name, node.lineno, fn.attr))
    assert not violations, "方案层出现动态执行构造：\n" + "\n".join(violations)


def test_classic_extraction_does_not_import_main_script() -> None:
    """取灯光表必须**不 import** 主脚本：它是脚本、顶层 import bpy，导入即炸。"""
    from schemes.classic import build_light_specs

    before = set(sys.modules)
    active, spare = build_light_specs()
    assert (len(active), len(spare)) == (23, 6)

    new = set(sys.modules) - before
    leaked = {m for m in new if "一键卡通渲染" in m or "toon_main" in m}
    assert not leaked, "提取灯光表时把主脚本载入了 sys.modules：%s" % sorted(leaked)


def test_scheme_layer_declares_no_light_values() -> None:
    """经典方案的数值只能来自主脚本，不能在方案层再抄一份。

    判定口径：把源表里的**所有数字**取出来，与 ``classic.py`` 里的浮点常量
    求交集。中性值 ``0.0 / 1.0 / -1.0`` 不算 —— 它们会合法地出现在
    ``obj.scale`` 之类的结构默认值上，不构成「抄写灯光数值」。

    只看 AST 常量节点，不看 docstring：文档里会引用 ``(2.0457,) * 3``
    作为「为什么要受限求值」的例子，那属于说明文字。
    """
    import ast

    from schemes.classic import build_light_specs

    source = (PIPELINE / "schemes" / "classic.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="classic.py")
    structural = {0.0, 1.0, -1.0}

    declared = {
        float(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, float)
        and node.value not in structural
    }

    def _numbers(obj: Any) -> set[float]:
        out: set[float] = set()
        if isinstance(obj, bool):
            return out
        if isinstance(obj, (int, float)):
            out.add(float(obj))
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                out |= _numbers(item)
        elif isinstance(obj, dict):
            for item in obj.values():
                out |= _numbers(item)
        return out

    active, spare = build_light_specs()
    from_source: set[float] = set()
    for row in list(active) + list(spare):
        from_source |= _numbers(list(row[1:]))
    from_source -= structural

    copied = declared & from_source
    assert not copied, (
        "classic.py 抄写了主脚本里的灯光数值（唯一来源应是主脚本）：%s"
        % sorted(copied)[:5]
    )

    from schemes import classic

    assert hasattr(classic, "build_light_specs")
