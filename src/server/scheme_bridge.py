# -*- coding: utf-8 -*-
"""渲染方案注册表的服务端桥接（PR #8）。

**为什么要这一层**：``render_pipeline/schemes/`` 是渲染侧的包，服务端不该直接
``sys.path.insert`` 之后 import 它 —— 那会把「服务端依赖渲染侧代码」变成隐式事实，
以后渲染侧挪个文件服务端就静默崩。这里的做法是：显式定位仓库内的 pipeline 目录，
**只**通过一个薄封装调用注册表的公开函数，并且把「注册表不可用」明确报成服务端错误。

**安全边界**：本模块只暴露三个能力给学生调用方 —— 列方案、校验并归一化参数、
把新旧字段归一成一个方案 id。它**不**暴露任何「按名字找模块」「按路径执行」的接口，
因为规格明确禁止出现这类通道。
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from . import errors

#: 与服务端错误码共用一个命名空间。规格点名要求 ``RENDER_SCHEME_INVALID``。
ERROR_SCHEME_INVALID = "RENDER_SCHEME_INVALID"
#: 参数集合 / 类型 / 范围不合法。
ERROR_SCHEME_PARAM_INVALID = "RENDER_SCHEME_PARAM_INVALID"


def _pipeline_dir() -> Path:
    """仓库内 ``render_pipeline/`` 的绝对路径（不对外泄露）。"""
    return Path(__file__).resolve().parents[2] / "render_pipeline"


@lru_cache(maxsize=1)
def _registry():
    """加载固定方案注册表。

    只 import 一次并缓存。若加载失败，异常在调用点被翻译成服务端错误 ——
    而不是让 500 直接冒到用户面前。
    """
    root = str(_pipeline_dir())
    if root not in sys.path:
        sys.path.insert(0, root)
    from schemes import registry as registry  # noqa: PLC0415

    return registry


@lru_cache(maxsize=1)
def _base():
    root = str(_pipeline_dir())
    if root not in sys.path:
        sys.path.insert(0, root)
    from schemes import base as base  # noqa: PLC0415

    return base


def _scheme_error(exc: Exception) -> "errors.ToonTunerError":
    code = getattr(exc, "code", ERROR_SCHEME_INVALID) or ERROR_SCHEME_INVALID
    # 注册表的错误码直接透出（规格点名 RENDER_SCHEME_INVALID 与
    # RENDER_SCHEME_RESTORE_FAILED 都是前端要认的稳定码）。
    return errors.ToonTunerError(code, str(exc), retryable=False)


def default_scheme_id() -> str:
    try:
        return str(_registry().DEFAULT_SCHEME_ID)
    except Exception as exc:  # noqa: BLE001
        raise _scheme_error(exc) from exc


#: 兼容别名，便于直接 import。
DEFAULT_SCHEME_ID = "volume_fog"


def scheme_public_list() -> list[dict[str, Any]]:
    """``GET /api/render/schemes`` 的 ``schemes`` 数组。

    注册表已经保证不含本机路径 / 脚本路径 / 模块路径 / 可执行命令；
    这里再断言一次 —— 这是对外接口，值得多一道检查。
    """
    try:
        items = _registry().list_schemes()
    except Exception as exc:  # noqa: BLE001
        raise _scheme_error(exc) from exc
    _assert_public(items)
    return items


def schemes_payload() -> dict[str, Any]:
    """``GET /api/render/schemes`` 的完整响应体（规格 §五）。"""
    return {"default": default_scheme_id(), "schemes": scheme_public_list()}


def _assert_public(items: Any) -> None:
    """防回归：方案描述里绝不允许出现路径或可执行命令。"""
    blob = json.dumps(items, ensure_ascii=False).lower()
    for forbidden in (":\\", ":/", ".py", "/usr/", "\\\\"):
        if forbidden in blob:
            raise errors.ToonTunerError(
                errors.INTERNAL_ERROR,
                "方案描述疑似泄露路径（命中 %r），拒绝返回。" % forbidden,
                retryable=False,
            )


def normalize_scheme_parameters(scheme_id: str, raw: Any) -> dict[str, Any]:
    """按方案校验并归一化参数。未知参数、越界、只读项一律拒绝。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise errors.ToonTunerError(
            ERROR_SCHEME_PARAM_INVALID,
            "scheme_parameters 必须是对象，收到 %s" % type(raw).__name__,
            retryable=False,
        )
    try:
        return dict(_registry().normalize_parameters(str(scheme_id), dict(raw)))
    except Exception as exc:  # noqa: BLE001
        raise _scheme_error(exc) from exc


def normalize_registry_params(scheme_id: str, raw: Any) -> dict[str, Any]:
    """同 :func:`normalize_scheme_parameters` 的别名，语义更直白。"""
    return normalize_scheme_parameters(scheme_id, raw)


def resolve_scheme_request(settings: Mapping[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """把请求里的新旧灯光字段归一成 ``(scheme_id, scheme_parameters, deprecation)``。

    规格 §四的兼容规则在**服务端**再执行一遍（渲染侧也会执行，两边都用同一份
    注册表规则）：

    * ``scheme`` 优先；
    * 只给旧 ``lighting`` / ``fog_density`` 时翻译并标记弃用；
    * 新旧同时出现且语义冲突 → 拒绝，不静默选一个。
    """
    scheme = settings.get("scheme")
    lighting = settings.get("lighting")
    raw_params = settings.get("scheme_parameters")
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, Mapping):
        raise errors.ToonTunerError(
            ERROR_SCHEME_PARAM_INVALID,
            "scheme_parameters 必须是对象，收到 %s" % type(raw_params).__name__,
            retryable=False,
        )
    raw_params = dict(raw_params)

    deprecation: str | None = None
    if lighting is not None:
        # 旧字段在场：交给注册表统一翻译 + 冲突判定。
        try:
            sid, note = _registry().resolve_scheme_id(
                scheme=(None if scheme is None else str(scheme)),
                lighting=str(lighting),
            )
        except Exception as exc:  # noqa: BLE001
            raise _scheme_error(exc) from exc
        deprecation = note
        # lighting 只是「换个说法」：一旦归一到 scheme，就不允许它再单独改变参数。
        if scheme is None:
            scheme = sid
    if scheme is None:
        scheme = default_scheme_id()

    fog_density = settings.get("fog_density")
    if fog_density is not None:
        if str(scheme) != "volume_fog":
            raise errors.ToonTunerError(
                ERROR_SCHEME_PARAM_INVALID,
                "fog_density 只适用于 volume_fog 方案，当前是 %s；拒绝静默忽略。"
                % scheme,
                retryable=False,
            )
        if "fog_density" in raw_params and float(raw_params["fog_density"]) != float(fog_density):
            raise errors.ToonTunerError(
                ERROR_SCHEME_PARAM_INVALID,
                "新旧雾密度冲突：scheme_parameters.fog_density=%r 与 fog_density=%r 不一致。"
                % (raw_params["fog_density"], fog_density),
                retryable=False,
            )
        raw_params.setdefault("fog_density", float(fog_density))
        deprecation = (deprecation + "；" if deprecation else "") \
            + "fog_density 已弃用，请改用 scheme_parameters.fog_density"

    params = normalize_scheme_parameters(str(scheme), raw_params)
    return str(scheme), params, deprecation


def scheme_to_legacy_lighting(scheme_id: str) -> str:
    """方案 id → 旧 ``lighting`` 取值。

    仅用于**兼容写出**（job 文件里同时带旧字段，方便既有排查工具读），
    不参与任何判定。
    """
    return "legacy" if str(scheme_id) == "classic" else "volume_fog"


def scheme_version(scheme_id: str) -> int:
    """方案版本，进任务指纹。"""
    try:
        cls = _registry().get_scheme(str(scheme_id))
    except Exception as exc:  # noqa: BLE001
        raise _scheme_error(exc) from exc
    return int(cls.version)
