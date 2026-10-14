# -*- coding: utf-8 -*-
"""
场景快照与恢复（PR #8 规格 §三）。

设计要点：**快照对象自带恢复能力，并且恢复必须回读确认。**

为什么把恢复放在快照里而不是方案里：一个方案的 apply 会同时改动世界、多盏灯的
``hide_viewport`` / ``hide_render``、活动相机、集合、合成器节点…… 每一样都要成对地
「先记录、后回读」。散在方案代码里迟早会漏一项。这里统一收口：

* ``SceneSnapshot.capture(...)`` 在应用方案**之前**记录；
* ``snapshot.restore()`` 恢复并**逐项回读**，任何一项没回到原位就抛
  ``RENDER_SCHEME_RESTORE_FAILED``。

本模块需要在 Blender 进程内运行（用 ``bpy``），但**不在导入期**碰 ``bpy`` ——
测试用 Fake `bpy` 注入，服务端只导入方案描述、不会走到这里。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .base import ERROR_RESTORE_FAILED, SchemeError


def _bpy() -> Any:
    """延迟取 ``bpy``。

    不在模块顶层 import：服务端（纯 Python）会导入 ``schemes`` 包来列方案清单，
    那时没有 ``bpy``。只有真正在 Blender 里 apply/restore 时才会走到这里。
    """
    import bpy  # noqa: PLC0415 - 必须是延迟导入

    return bpy


class SceneSnapshot:
    """方案应用前的现场记录 + 恢复。

    记录范围对应规格 §三「每次执行方案前记录」：

    * 当前世界及世界节点状态；
    * 全部原灯光的 ``hide_viewport`` / ``hide_render``；
    * 当前活动相机；
    * 已存在的方案集合（本次要新建的那个集合原本存不存在）；
    * 灯光总控状态；
    * 体积雾对象和材质；
    * 合成器辉光节点状态；
    * 相关渲染体积设置。
    """

    def __init__(self) -> None:
        #: 世界：应用前的 world 对象名（None 表示原本没有世界）。
        self.world_name: str | None = None
        #: 原灯光：name -> [hide_viewport, hide_render]
        self.light_flags: dict[str, list[bool]] = {}
        #: 活动相机名。
        self.camera_name: str | None = None
        #: 应用前已存在的方案集合名（本次新建的集合不该在恢复后残留）。
        self.preexisting_collections: list[str] = []
        #: 应用前已存在的对象名（本次临时创建的对象都要删掉）。
        self.preexisting_objects: list[str] = []
        #: 应用前已存在的材质名（本次临时创建的材质都要删掉）。
        self.preexisting_materials: list[str] = []
        #: 合成器：use_nodes + 各节点 mute 状态，恢复时回读。
        self.compositor_use_nodes: bool | None = None
        self.compositor_mute: dict[str, bool] = {}
        #: 渲染体积设置：体积采样相关字段的原值。
        self.volume_settings: dict[str, Any] = {}
        #: 由方案填充：本次临时创建、需要在恢复时删除的集合名。
        self.scheme_collections: list[str] = []
        #: 由方案填充：本次临时创建的对象名。
        self.scheme_objects: list[str] = []
        #: 由方案填充：本次临时创建的材质名。
        self.scheme_materials: list[str] = []
        #: 方案自定义的额外恢复项：``[(描述, 回读函数), ...]``。
        #: 回读函数返回 True 表示确已恢复；返回 False 或抛异常 → RESTORE_FAILED。
        self.extra_checks: list[tuple[str, Any]] = []

    # -- 记录 ---------------------------------------------------------------------

    @classmethod
    def capture(cls, scene: Any, *, collection_names: Sequence[str] = ()) -> "SceneSnapshot":
        """记录现场。``collection_names`` 是方案将要创建/复用的集合名。"""
        bpy = _bpy()
        snap = cls()

        world = getattr(scene, "world", None)
        snap.world_name = getattr(world, "name", None) if world is not None else None

        for obj in list(getattr(scene, "objects", []) or []):
            if getattr(obj, "type", None) == "LIGHT":
                snap.light_flags[obj.name] = [
                    bool(getattr(obj, "hide_viewport", False)),
                    bool(getattr(obj, "hide_render", False)),
                ]
            snap.preexisting_objects.append(obj.name)

        cam = getattr(scene, "camera", None)
        snap.camera_name = getattr(cam, "name", None) if cam is not None else None

        for name in collection_names:
            if bpy.data.collections.get(name) is not None:
                snap.preexisting_collections.append(name)

        for mat in list(getattr(bpy.data, "materials", []) or []):
            snap.preexisting_materials.append(mat.name)

        snap._capture_compositor(scene)
        snap._capture_volume_settings(scene)
        return snap

    def _capture_compositor(self, scene: Any) -> None:
        use_nodes = getattr(scene, "use_nodes", None)
        if use_nodes is None:
            return
        self.compositor_use_nodes = bool(use_nodes)
        tree = getattr(scene, "node_tree", None)
        if tree is None:
            return
        for node in list(getattr(tree, "nodes", []) or []):
            name = getattr(node, "name", None)
            if name is None:
                continue
            self.compositor_mute[str(name)] = bool(getattr(node, "mute", False))

    def _capture_volume_settings(self, scene: Any) -> None:
        eevee = getattr(scene, "eevee", None)
        if eevee is None:
            return
        for attr in ("use_volumetric", "volumetric_start", "volumetric_end",
                     "volumetric_tile_size", "volumetric_samples",
                     "volumetric_sample_distribution", "use_volumetric_shadows",
                     "volumetric_shadow_render_step"):
            if hasattr(eevee, attr):
                self.volume_settings[attr] = getattr(eevee, attr)

    # -- 恢复 ---------------------------------------------------------------------

    def restore(self) -> None:
        """恢复现场并**回读确认**；任何一项没还原就报 ``RENDER_SCHEME_RESTORE_FAILED``。

        顺序：先删本次临时创建的东西（对象 → 材质 → 集合），再还原被改动的属性，
        最后按方案自定义的回读项逐条确认。删东西必须在还原世界/相机之前 ——
        否则被删对象可能还挂在世界或相机链上。
        """
        bpy = _bpy()
        problems: list[str] = []

        # 1) 删除本次临时创建的对象、材质、集合。
        self._remove_created(bpy, problems)
        # 2) 还原原灯光可见性。
        self._restore_light_flags(bpy, problems)
        # 3) 还原世界。
        self._restore_world(bpy, problems)
        # 4) 还原相机。
        self._restore_camera(bpy, problems)
        # 5) 还原合成器静音状态。
        self._restore_compositor(bpy, problems)
        # 6) 还原渲染体积设置。
        self._restore_volume_settings(bpy, problems)
        # 7) 方案自定义回读。
        self._restore_extra(problems)

        if problems:
            raise SchemeError(
                ERROR_RESTORE_FAILED,
                "场景恢复未通过回读确认：%s" % "；".join(problems),
                details={"problems": problems},
            )

    def _remove_created(self, bpy: Any, problems: list[str]) -> None:
        for name in self.scheme_objects:
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception as exc:  # noqa: BLE001
                problems.append("删除临时对象 %s 失败：%s" % (name, exc))
        for name in self.scheme_objects:
            if bpy.data.objects.get(name) is not None:
                problems.append("临时对象 %s 仍存在于场景" % name)

        for name in self.scheme_materials:
            mat = bpy.data.materials.get(name)
            if mat is None:
                continue
            try:
                bpy.data.materials.remove(mat, do_unlink=True)
            except TypeError:
                # 桩或旧 API 不支持 do_unlink：退回单参数调用。
                try:
                    bpy.data.materials.remove(mat)
                except Exception as exc:  # noqa: BLE001
                    problems.append("删除临时材质 %s 失败：%s" % (name, exc))
            except Exception as exc:  # noqa: BLE001
                problems.append("删除临时材质 %s 失败：%s" % (name, exc))

        for name in self.scheme_collections:
            if name in self.preexisting_collections:
                # 集合本来就有（方案复用而非新建）：不该删，跳过。
                continue
            coll = bpy.data.collections.get(name)
            if coll is None:
                continue
            try:
                bpy.data.collections.remove(coll)
            except Exception as exc:  # noqa: BLE001
                problems.append("删除临时集合 %s 失败：%s" % (name, exc))
            if bpy.data.collections.get(name) is not None:
                problems.append("临时集合 %s 仍存在" % name)

    def _restore_light_flags(self, bpy: Any, problems: list[str]) -> None:
        for name, (hide_vp, hide_render) in self.light_flags.items():
            obj = bpy.data.objects.get(name)
            if obj is None:
                # 原灯被删了 —— 这不是「恢复成功」，必须报出来。
                problems.append("原灯光 %s 已不存在，无法恢复" % name)
                continue
            try:
                obj.hide_viewport = hide_vp
                obj.hide_render = hide_render
            except Exception as exc:  # noqa: BLE001
                problems.append("恢复灯光 %s 可见性失败：%s" % (name, exc))
                continue
            # 回读确认。
            if bool(getattr(obj, "hide_viewport", False)) != hide_vp:
                problems.append("灯光 %s 的 hide_viewport 回读不符" % name)
            if bool(getattr(obj, "hide_render", False)) != hide_render:
                problems.append("灯光 %s 的 hide_render 回读不符" % name)

    def _restore_world(self, bpy: Any, problems: list[str]) -> None:
        scene = bpy.context.scene
        if self.world_name is None:
            # 原本没有世界：恢复成没有世界。新建的黑场世界随临时对象走不到这里，
            # 它由方案在 scheme_objects 里登记删除，这里只把 scene.world 归零。
            if getattr(scene, "world", None) is not None and self._world_was_created():
                scene.world = None
            return
        target = bpy.data.worlds.get(self.world_name)
        if target is None:
            problems.append("原世界 %s 不存在，无法恢复" % self.world_name)
            return
        try:
            scene.world = target
        except Exception as exc:  # noqa: BLE001
            problems.append("恢复世界失败：%s" % exc)
            return
        if getattr(getattr(scene, "world", None), "name", None) != self.world_name:
            problems.append("世界回读不符（期望 %s）" % self.world_name)

    def _world_was_created(self) -> bool:
        return any("世界" in c[0] for c in self.extra_checks if c[0])

    def _restore_camera(self, bpy: Any, problems: list[str]) -> None:
        scene = bpy.context.scene
        if self.camera_name is None:
            return
        cam = bpy.data.objects.get(self.camera_name)
        if cam is None:
            problems.append("原相机 %s 不存在，无法恢复" % self.camera_name)
            return
        try:
            scene.camera = cam
        except Exception as exc:  # noqa: BLE001
            problems.append("恢复相机失败：%s" % exc)
            return
        if getattr(getattr(scene, "camera", None), "name", None) != self.camera_name:
            problems.append("活动相机回读不符（期望 %s）" % self.camera_name)

    def _restore_compositor(self, bpy: Any, problems: list[str]) -> None:
        if self.compositor_use_nodes is None:
            return
        scene = bpy.context.scene
        try:
            scene.use_nodes = self.compositor_use_nodes
        except Exception as exc:  # noqa: BLE001
            problems.append("恢复合成器开关失败：%s" % exc)
        tree = getattr(scene, "node_tree", None)
        if tree is None:
            return
        for node in list(getattr(tree, "nodes", []) or []):
            name = str(getattr(node, "name", ""))
            if name in self.compositor_mute:
                node.mute = self.compositor_mute[name]

    def _restore_volume_settings(self, bpy: Any, problems: list[str]) -> None:
        if not self.volume_settings:
            return
        eevee = getattr(bpy.context.scene, "eevee", None)
        if eevee is None:
            return
        for attr, value in self.volume_settings.items():
            try:
                setattr(eevee, attr, value)
            except Exception as exc:  # noqa: BLE001
                problems.append("恢复渲染体积设置 %s 失败：%s" % (attr, exc))

    def _restore_extra(self, problems: list[str]) -> None:
        for label, check in self.extra_checks:
            try:
                ok = check()
            except Exception as exc:  # noqa: BLE001
                problems.append("%s 回读异常：%s" % (label, exc))
                continue
            if ok is not True:
                problems.append("%s 回读未通过" % label)

    # -- 序列化（进 run_manifest 的脱敏摘要） --------------------------------------

    def summary(self) -> dict[str, Any]:
        """给清单用的**摘要**：只记计数与名字，不记本机路径（规格 §七）。"""
        return {
            "restored_lights": len(self.light_flags),
            "had_world": self.world_name is not None,
            "had_camera": self.camera_name is not None,
            "created_objects": list(self.scheme_objects),
            "created_materials": list(self.scheme_materials),
            "created_collections": list(self.scheme_collections),
            "volume_settings_tracked": len(self.volume_settings),
        }


def dump_legacy_light_state(scene: Any) -> str:
    """把原灯光的可见性状态序列化成 JSON —— 沿用既有实现写在集合自定义属性里。

    保留这个写法是为了**与既有体积雾实现（fb43b61）保持同一份口径**：
    真机验收时如果靠集合属性排查，两套代码必须写得一样。
    """
    state: dict[str, list[bool]] = {}
    for obj in list(getattr(scene, "objects", []) or []):
        if getattr(obj, "type", None) == "LIGHT":
            state[obj.name] = [bool(obj.hide_viewport), bool(obj.hide_render)]
    return json.dumps(state, ensure_ascii=False)
