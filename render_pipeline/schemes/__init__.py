# -*- coding: utf-8 -*-
"""
固定渲染方案注册表（PR #8 渲染方案系统）。

对外只暴露 :mod:`render_pipeline.schemes.registry`。这个包是**唯一**的方案入口：
调用方拿不到任何可变脚本路径、模块名或属性表达式，只能给一个**固定 id**
（``classic`` / ``volume_fog``）+ 一份**注册表定义的参数**。

设计红线（PR #8 规格 §一 / §五）：

* 方案 id 与实现类是**写死的映射**，不做动态导入、不按字符串反射。
  浏览器能提交的只有 id 与 JSON 参数值 —— 提交不了脚本路径、Python 代码、
  命令行片段、Blender 属性表达式或环境变量。
* 参数定义（名称 / 类型 / 范围 / 默认值）全部由服务端生成，
  客户端**不得**自定义参数定义，也不得提交未知参数。
* 未知 id 一律拒绝，稳定错误码 ``RENDER_SCHEME_INVALID``。
"""

from __future__ import annotations

from .base import (
    ParamSpec,
    SchemeDescription,
    SchemeError,
    SchemeResult,
    ERROR_INVALID_SCHEME,
    ERROR_INVALID_PARAM,
    ERROR_RESTORE_FAILED,
    ERROR_APPLY_FAILED,
)
from .registry import (
    RENDER_SCHEMES,
    DEFAULT_SCHEME_ID,
    apply_scheme,
    describe_scheme,
    get_scheme,
    list_schemes,
    normalize_parameters,
    restore_scheme,
    scheme_descriptions,
)

__all__ = [
    "RENDER_SCHEMES",
    "DEFAULT_SCHEME_ID",
    "ParamSpec",
    "SchemeDescription",
    "SchemeError",
    "SchemeResult",
    "ERROR_INVALID_SCHEME",
    "ERROR_INVALID_PARAM",
    "ERROR_RESTORE_FAILED",
    "ERROR_APPLY_FAILED",
    "apply_scheme",
    "describe_scheme",
    "get_scheme",
    "list_schemes",
    "normalize_parameters",
    "restore_scheme",
    "scheme_descriptions",
]
