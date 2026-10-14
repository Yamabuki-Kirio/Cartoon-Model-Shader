# -*- coding: utf-8 -*-
"""
经典灯光方案 ``classic``（PR #8 规格 §二）。

**这是结构提取，不是重写。** 21 盏启用灯 + 6 盏备用灯的位置 / 旋转 / 颜色 / 强度，
全部来自 :mod:`render_pipeline.一键卡通渲染` 里的 ``LIGHTS_ACTIVE`` / ``LIGHTS_SPARE``
两个表 —— 那是源工程实测值，是这套灯组的**唯一来源**。

刻意**不**在这里重抄一遍数值：数值一旦抄成两份，改一处忘了另一处，
两套灯组就会静默漂移，而且没人会发现。这里只负责「怎么用那两个表」：

* 复用 / 新建同名灯的判定；
* 按灯型附加属性（AREA 的 size/spread、SPOT 的 spot_size/spot_blend、
  SUN 的 angle、以及 specular/diffuse/volume 系数）；
* 集合归属。

因此本模块**不包含任何灯光数值常量**。守卫测试会同时检查
「值只在主脚本里」与「灯名 / 数量 / 关键参数在提取前后一致」。

第一版**无动态方案参数**（规格 §二）：经典方案提交体恒为 ``{}``。

行为上刻意保留旧版兼容性子（规格 §二「旧版兼容行为」）：

* **不建立体积雾**；
* **不强制替换当前世界环境**（沿用工程既有 world，与体积雾的黑色无环境光世界相反）。
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .base import Scheme, SchemeResult
from .snapshot import SceneSnapshot


def _bpy() -> Any:
    import bpy  # noqa: PLC0415 - 延迟导入，见 snapshot._bpy 的说明

    return bpy


def build_light_specs() -> tuple[list[tuple], list[tuple]]:
    """从主脚本取灯光表。

    主脚本 ``一键卡通渲染.py`` 是个脚本、不是包。这里**只做 AST 静态读取**：
    不 ``import``、不 ``importlib``、不 ``exec``。

    为什么不 import：主脚本顶层写着 ``import bpy`` 与 ``os.environ.get(...)``，
    在无 Blender 的进程里 import 会直接炸；而且方案层一旦拥有「加载并执行
    任意 Python 文件」的能力，就等于把规格里禁止的动态执行重新引了回来。
    AST 取常量既无副作用，也与运行时**完全同值**。
    """
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "一键卡通渲染.py")
    return _extract_tables(path)


def _safe_literal(node: Any) -> Any:
    """求值主脚本灯光表里的**常量表达式**，比 ``ast.literal_eval`` 宽一点。

    为什么不能只用 ``literal_eval``：表里实际出现了

    * ``(2.0457,) * 3`` —— 元组重复（``ast.BinOp`` + ``ast.Mult``）；
    * ``math.pi`` 之类的属性访问（AREA 的 spread）。

    因此这里用一个**受限求值器**：只认字面量、元组/列表/字典、一元正负、
    以及 ``+ - * /`` 四则。任何函数调用、名称引用（除白名单常量）都直接拒绝，
    绝不 ``eval`` 任意表达式 —— 主脚本是仓库内文件，但方案层仍不该拥有执行权。
    """
    import ast

    _CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}

    def ev(n: Any) -> Any:
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Tuple):
            return tuple(ev(e) for e in n.elts)
        if isinstance(n, ast.List):
            return [ev(e) for e in n.elts]
        if isinstance(n, ast.Dict):
            return {ev(k): ev(v) for k, v in zip(n.keys, n.values)}
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand)
            return +v if isinstance(n.op, ast.UAdd) else -v
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            if n.value.id == "math" and n.attr in _CONSTS:
                return _CONSTS[n.attr]
        if isinstance(n, ast.Name) and n.id in _CONSTS:
            return _CONSTS[n.id]
        if isinstance(n, ast.BinOp):
            left, right = ev(n.left), ev(n.right)
            op = n.op
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, ast.Div):
                return left / right
        raise ValueError("不支持的常量表达式：%r" % (n,))

    return ev(node)


def _extract_tables(path: str) -> tuple[list[tuple], list[tuple]]:
    """用 AST 从主脚本里取出 ``LIGHTS_ACTIVE`` / ``LIGHTS_SPARE`` 的字面量。

    为什么用 AST 而不是 import：主脚本顶层写的是
    ``LIGHTING_PROFILE = os.environ.get(...)``、``import bpy`` 之类，
    在无 Blender 的进程里 import 会直接炸。而这两个表是纯字面量，
    AST 取出来与运行时**完全同值**，且不受任何环境变量影响 ——
    这正好满足「提取前后以自动测试锁定灯名、数量和关键参数」的要求。

    表里含有 ``(2.0457,) * 3`` 与 ``math.pi`` 这类常量表达式，
    因此走 :func:`_safe_literal`（受限求值），而不是 ``ast.literal_eval``。
    """
    import ast

    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    wanted = {"LIGHTS_ACTIVE", "LIGHTS_SPARE"}
    found: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                found[target.id] = _safe_literal(node.value)
    missing = wanted - set(found)
    if missing:
        raise RuntimeError(
            "主脚本里缺少灯光表 %s —— 经典方案的唯一来源必须存在" % sorted(missing)
        )
    return list(found["LIGHTS_ACTIVE"]), list(found["LIGHTS_SPARE"])


def apply_light_spec(scene: Any, spec_row: tuple) -> Any:
    """按**一行**灯光规格建灯 / 复用灯并归属集合。

    这是从 ``setup_legacy_lights`` 逐行搬过来的结构，**数值一律来自 spec_row**。
    函数保持与旧实现同语义，包括那条关键修正：复用分支也要按规格整体覆盖，
    否则场景自带的同名灯会以原样参与渲染，把有效灯数从 21 变成 22。
    """
    bpy = _bpy()
    name, ltype, energy, color, loc, rot, coll_name, extra = spec_row
    extra = dict(extra or {})

    existing = bpy.data.objects.get(name)
    if existing is not None and getattr(existing, "type", None) != "LIGHT":
        existing = None  # 同名但类型不符，另建

    if existing is not None:
        obj = existing
        data = obj.data
        if data is None or getattr(data, "type", None) != ltype:
            # 场景自带同名灯但类型不符（如 .blend 里的 POINT 撞上 SUN 名）→ 换灯数据
            data = bpy.data.lights.new(name, ltype)
            obj.data = data
    else:
        data = bpy.data.lights.new(name, ltype)
        obj = bpy.data.objects.new(name, data)

    # ★ 无论新建还是复用，都按规格覆盖（见旧实现里的同款注释）。
    data.energy = energy
    data.color = color
    data.use_shadow = True
    if ltype == "AREA" and "area" in extra:
        data.size, data.size_y = extra["area"]
        data.spread = math.pi
    if ltype == "SPOT":
        if "spot" in extra:
            data.spot_size, data.spot_blend = extra["spot"]
        if "soft" in extra:
            data.shadow_soft_size = extra["soft"]
    if ltype in ("SUN", "POINT") and "soft" in extra:
        data.shadow_soft_size = extra["soft"]
    if ltype == "SUN" and "angle" in extra:
        data.angle = extra["angle"]
    for key, attr in (("specular", "specular_factor"),
                      ("diffuse", "diffuse_factor"),
                      ("volume", "volume_factor")):
        if key in extra and hasattr(data, attr):
            setattr(data, attr, extra[key])

    obj.rotation_mode = "XYZ"
    obj.location = loc
    obj.rotation_euler = rot
    obj.scale = extra.get("scale", (1.0, 1.0, 1.0))
    obj.hide_render = bool(extra.get("hide", False))

    target_coll = _ensure_collection(bpy, scene, coll_name)
    for coll in list(getattr(obj, "users_collection", []) or []):
        if coll is not target_coll:
            try:
                coll.objects.unlink(obj)
            except Exception:
                pass
    if obj not in list(getattr(target_coll, "objects", []) or []):
        target_coll.objects.link(obj)
    return obj


def _ensure_collection(bpy: Any, scene: Any, name: str) -> Any:
    """取或建集合（同旧实现的 ensure_collection）。"""
    coll = bpy.data.collections.get(name)
    if coll is not None:
        return coll
    coll = bpy.data.collections.new(name)
    # 挂到场景根集合下。
    try:
        scene.collection.children.link(coll)
    except Exception:
        pass
    return coll


class ClassicScheme(Scheme):
    """经典 21+6 灯光方案（不含体积雾、不改世界环境）。"""

    id = "classic"
    version = 1
    label = "经典灯光"
    description = "源工程实测的 21 盏启用灯 + 6 盏备用灯，参数与源工程逐项一致"
    recommended_use = "追求与源工程一致的复刻效果；回退比较"
    has_volume_fog = False
    performance_note = "无体积雾；EEVEE 下开销最低"

    #: 第一版**无动态方案参数**（规格 §二）。空元组是刻意的：不是「还没做」，
    #: 而是经典方案的可调面就是零 —— 它的价值在于「与源工程逐项一致」，
    #: 一旦开放参数就不再是复刻了。
    PARAMS: tuple = ()

    #: 经典方案用到的集合名（快照要记录它们原本存不存在）。
    COLLECTIONS: tuple[str, ...] = (
        "Collection", "集合 4", "集合 4 2", "打光", "灯光",
        "换头替换模型", "卡通三点光",
    )

    def apply(
        self, scene: Any, targets: Sequence[Any], parameters: Mapping[str, Any]
    ) -> SchemeResult:
        bpy = _bpy()
        snapshot = SceneSnapshot.capture(scene, collection_names=self.COLLECTIONS)

        active, spare = build_light_specs()
        rows = list(active) + list(spare)

        made: list[Any] = []
        reused = 0
        for row in rows:
            before = bpy.data.objects.get(row[0])
            obj = apply_light_spec(scene, row)
            if before is not None and before is obj:
                reused += 1
            made.append(obj)

        # 快照只登记**本次新建**的对象 —— 复用的原名灯属于「原现场」，
        # 恢复时靠 light_flags 还原可见性，绝不能删。
        snapshot.scheme_objects = [
            o.name for o in made if o.name not in snapshot.preexisting_objects
        ]

        result = SchemeResult(
            scheme_id=self.id,
            version=self.version,
            parameters=dict(parameters),
            snapshot=snapshot,
            lights=made,
        )
        result.notes.append("灯光 %d 盏（复用 %d）" % (len(made), reused))
        result.notes.append("不建立体积雾；世界环境保持原样")
        return result

    def restore(self, snapshot: Any) -> None:
        if snapshot is None:
            return
        snapshot.restore()
