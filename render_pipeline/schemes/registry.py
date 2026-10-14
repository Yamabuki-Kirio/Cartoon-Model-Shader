# -*- coding: utf-8 -*-
"""
唯一固定方案注册表（PR #8 规格 §一）。

``RENDER_SCHEMES`` 是全系统**唯一**的方案来源。它是一张写死的 id → 实现类映射：

.. code-block:: python

   RENDER_SCHEMES = {
       "classic": ClassicScheme,
       "volume_fog": VolumeFogScheme,
   }

**为什么不做动态发现**：规格要求「禁止浏览器提交脚本路径 / Python 代码 / 命令行片段 /
Blender 属性表达式 / 任意环境变量」。只要方案类是按字符串动态 import 或按名字反射得到的，
就存在「让服务端加载别的模块」的路径。写死两个键之后，浏览器能影响的只有
「选哪个固定方案」和「在注册表声明过的参数范围内填什么值」，这是结构性的，不靠过滤黑名单。

注册表对外提供（与规格一字对应）：

* ``get_scheme(scheme_id)``
* ``list_schemes()``
* ``normalize_parameters(scheme_id, raw)``
* ``apply_scheme(scene, targets, parameters)``
* ``restore_scheme(snapshot)``
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .base import (
    ERROR_INVALID_SCHEME,
    Scheme,
    SchemeDescription,
    SchemeError,
    SchemeResult,
)
from .classic import ClassicScheme
from .volume_fog import VolumeFogScheme

# -------------------------------------------------------------------------------------
#  ★ 固定注册表（唯一）
# -------------------------------------------------------------------------------------

#: 固定方案注册表。键 = 方案 id，值 = 实现类。**只有这两个。**
RENDER_SCHEMES: dict[str, type[Scheme]] = {
    "classic": ClassicScheme,
    "volume_fog": VolumeFogScheme,
}

#: 默认方案。由服务端决定，前端**不得**硬编码（规格 §六）。
DEFAULT_SCHEME_ID = "volume_fog"

#: 旧 ``--lighting`` 取值 → 新方案 id 的**弃用映射**（规格 §四）。
#:
#: 保留兼容但标记弃用：``legacy → classic``、``volume_fog → volume_fog``。
#: 注意键与值刻意分开写：将来若把 ``volume_fog`` 方案改名，这里必须显式改，
#: 不允许写成 ``{k: k for k in RENDER_SCHEMES}`` 那种「看起来能自洽」的恒等映射。
LEGACY_LIGHTING_TO_SCHEME: dict[str, str] = {
    "legacy": "classic",
    "volume_fog": "volume_fog",
}


def _assert_registry_integrity() -> None:
    """导入即自检：**重复 ID / 键类不符 / 版本非法必须启动失败**（规格 §八）。"""
    if not RENDER_SCHEMES:
        raise SchemeError(ERROR_INVALID_SCHEME, "方案注册表为空，拒绝启动")
    seen_ids: set[str] = set()
    for key, cls in RENDER_SCHEMES.items():
        if not isinstance(key, str) or not key:
            raise SchemeError(ERROR_INVALID_SCHEME, "注册表键必须是非空字符串：%r" % (key,))
        if not (isinstance(cls, type) and issubclass(cls, Scheme)):
            raise SchemeError(
                ERROR_INVALID_SCHEME,
                "注册表 %r 的值必须是 Scheme 子类，收到 %r" % (key, cls),
            )
        if cls.id != key:
            # 键与类内 id 不一致，是「改了一处忘了另一处」的典型来源。
            raise SchemeError(
                ERROR_INVALID_SCHEME,
                "注册表键 %r 与实现类的 id %r 不一致" % (key, cls.id),
            )
        if key in seen_ids:
            raise SchemeError(ERROR_INVALID_SCHEME, "方案 id 重复：%r" % key)
        seen_ids.add(key)
        if not isinstance(cls.version, int) or cls.version < 1:
            raise SchemeError(
                ERROR_INVALID_SCHEME, "方案 %s 的 version 必须是 >=1 的整数" % key
            )
        # 参数定义自检（重复 id / 类型 / 范围 / 默认值）。
        cls.param_index()
    if DEFAULT_SCHEME_ID not in RENDER_SCHEMES:
        raise SchemeError(
            ERROR_INVALID_SCHEME, "默认方案 %r 不在注册表中" % DEFAULT_SCHEME_ID
        )


_assert_registry_integrity()


# -------------------------------------------------------------------------------------
#  对外接口
# -------------------------------------------------------------------------------------


def get_scheme(scheme_id: str) -> type[Scheme]:
    """按 id 取方案类。未知 id 抛 ``RENDER_SCHEME_INVALID``。"""
    key = str(scheme_id or "").strip()
    cls = RENDER_SCHEMES.get(key)
    if cls is None:
        raise SchemeError(
            ERROR_INVALID_SCHEME,
            "未知渲染方案 %r；可用方案：%s" % (scheme_id, sorted(RENDER_SCHEMES)),
            details={"scheme": scheme_id, "available": sorted(RENDER_SCHEMES)},
        )
    return cls


def list_schemes() -> list[dict[str, Any]]:
    """全部方案的公开描述（给 ``GET /api/render/schemes``）。

    顺序稳定：默认方案排在最前，其余按 id 排序 —— 前端卡片顺序不该随字典插入序抖动。
    """
    ids = sorted(RENDER_SCHEMES)
    if DEFAULT_SCHEME_ID in ids:
        ids.remove(DEFAULT_SCHEME_ID)
        ids.insert(0, DEFAULT_SCHEME_ID)
    return [
        RENDER_SCHEMES[sid].describe(recommended=(sid == DEFAULT_SCHEME_ID)).to_public()
        for sid in ids
    ]


def scheme_descriptions() -> list[SchemeDescription]:
    """``list_schemes`` 的结构化版本（服务端内部用）。"""
    return [
        RENDER_SCHEMES[sid].describe(recommended=(sid == DEFAULT_SCHEME_ID))
        for sid in sorted(RENDER_SCHEMES)
    ]


def describe_scheme(scheme_id: str) -> dict[str, Any]:
    """单个方案的公开描述。未知 id 同样拒绝。"""
    return get_scheme(scheme_id).describe(
        recommended=(str(scheme_id).strip() == DEFAULT_SCHEME_ID)
    ).to_public()


def normalize_parameters(scheme_id: str, raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """按方案校验并归一化参数（未知参数一律拒绝，缺省填默认值）。"""
    return get_scheme(scheme_id).normalize(raw)


def resolve_scheme_id(
    *,
    scheme: str | None = None,
    lighting: str | None = None,
) -> tuple[str, str | None]:
    """把新旧两套入口统一成一个方案 id，返回 ``(scheme_id, deprecation_note)``。

    规格 §四的兼容规则：

    * 新字段 ``scheme`` 直接用；
    * 旧字段 ``--lighting legacy|volume_fog`` 按弃用映射翻译，并给出弃用提示；
    * **两者同时出现且语义冲突时必须拒绝** —— 不允许静默选一个。
    """
    new_id = None if scheme is None else str(scheme).strip()
    old_raw = None if lighting is None else str(lighting).strip().lower()

    note: str | None = None
    if old_raw is not None:
        mapped = LEGACY_LIGHTING_TO_SCHEME.get(old_raw)
        if mapped is None:
            raise SchemeError(
                ERROR_INVALID_SCHEME,
                "旧参数 --lighting 只接受 %s，收到 %r"
                % (sorted(LEGACY_LIGHTING_TO_SCHEME), lighting),
            )
        note = "--lighting %s 已弃用，请改用 --scheme %s" % (old_raw, mapped)
        if new_id is None:
            # 只给了旧字段：翻译后用，但保留弃用提示。
            get_scheme(mapped)  # 保证映射目标真实存在
            return mapped, note
        if new_id and new_id != mapped:
            # 新旧冲突：明确拒绝，不做「以新为准」的静默选择。
            raise SchemeError(
                ERROR_INVALID_SCHEME,
                "新旧灯光参数冲突：scheme=%r 与 lighting=%r（等价于 %r）不是同一个方案，"
                "拒绝执行；请只提交其中一个。" % (new_id, lighting, mapped),
                details={"scheme": new_id, "lighting": lighting, "mapped": mapped},
            )
        get_scheme(new_id)
        return new_id, note

    if new_id is None:
        return DEFAULT_SCHEME_ID, None
    get_scheme(new_id)
    return new_id, None


def apply_scheme(
    scene: Any,
    targets: Sequence[Any],
    parameters: Mapping[str, Any],
    *,
    scheme_id: str,
    snapshot: Any = None,
) -> SchemeResult:
    """在场景上临时应用方案，返回带快照的 :class:`SchemeResult`。

    ``parameters`` 会被**再归一化一次**（幂等）：即便调用方已经校验过，也不信任
    上游传来的字典 —— 这里是最后一道关口。
    """
    cls = get_scheme(scheme_id)
    normalized = cls.normalize(parameters)
    scheme = cls()
    try:
        result = scheme.apply(scene, list(targets), normalized)
    except SchemeError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一成稳定码，不让裸异常冒到 HTTP 层
        raise SchemeError(
            "RENDER_SCHEME_APPLY_FAILED",
            "应用方案 %s 失败：%s" % (scheme_id, exc),
            details={"scheme": scheme_id},
        ) from exc
    if result is None:
        raise SchemeError(
            "RENDER_SCHEME_APPLY_FAILED", "方案 %s 未返回执行结果" % scheme_id
        )
    # 结果里的 id/version/参数以注册表为准，避免实现类写错导致指纹漂移。
    result.scheme_id = cls.id
    result.version = int(cls.version)
    result.parameters = dict(normalized)
    if snapshot is not None and result.snapshot is None:
        result.snapshot = snapshot
    return result


def restore_scheme(snapshot: Any) -> None:
    """按快照恢复方案造成的现场改动。

    规格 §三要求：恢复**必须回读确认**，失败时报 ``RENDER_SCHEME_RESTORE_FAILED``。
    恢复逻辑在快照自身的 ``restore``（构造于 :mod:`render_pipeline.schemes.snapshot`），
    因为不同方案要恢复的东西不同，而快照最清楚自己动了哪些属性。
    """
    if snapshot is None:
        return
    restore = getattr(snapshot, "restore", None)
    if restore is None:
        raise SchemeError(
            ERROR_RESTORE_FAILED,
            "快照对象缺少 restore()，无法确认场景已恢复",
        )
    try:
        restore()
    except SchemeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SchemeError(
            ERROR_RESTORE_FAILED, "恢复场景失败：%s" % exc
        ) from exc
