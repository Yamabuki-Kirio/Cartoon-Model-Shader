# -*- coding: utf-8 -*-
"""运行终态契约的服务端桥接（PR #6 规格 §3 / P0）。

**唯一事实来源**是 ``render_pipeline/run_contract.py``：固定脚本、通用驱动、确认页
与一切成品消费端都必须从那里取终态词表。本模块只负责按路径把它加载进来，
**不复制、不镜像任何常量**。

为什么不复制：一旦复制出第二份词表就会漂移，而漂移的后果正是本次要修的病灶 ——
适配层过去把 ``REJECTED`` / ``DIAGNOSTIC`` 一律压成 ``failed``，于是
「需要材质确认」被显示成「渲染失败」、诊断图被显示成普通失败，
用户得不到正确的后续操作，清理与重试策略也无从区分。

加载失败（文件缺失 / 不可读 / 语法错误）时**立刻抛错**，绝不静默退化成本地词表：
静默退化等于把「契约对不上」伪装成「看起来正常」，比启动失败危险得多。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

#: ``render_pipeline/run_contract.py`` —— 相对本文件（src/server/render_contract.py）
#: 上溯三级即仓库根。不依赖 config，避免导入环。
_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "render_pipeline" / "run_contract.py"

_cached: ModuleType | None = None


def contract_path() -> Path:
    """契约文件路径（供测试与诊断展示）。"""
    return _CONTRACT_PATH


def load() -> ModuleType:
    """加载并缓存终态契约模块。重复调用返回同一实例。"""
    global _cached
    if _cached is not None:
        return _cached
    if not _CONTRACT_PATH.is_file():
        raise RuntimeError(
            "运行终态契约缺失：%s（render_pipeline/run_contract.py 是唯一判定点，"
            "不得以本地词表替代）" % _CONTRACT_PATH
        )
    spec = importlib.util.spec_from_file_location("toon_render_contract", _CONTRACT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - 仅在文件被并发替换时出现
        raise RuntimeError("无法加载运行终态契约：%s" % _CONTRACT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _cached = module
    return module


def _reset_cache() -> None:
    """仅供测试使用：清除缓存以便换路径复载。"""
    global _cached
    _cached = None


def outcome_set() -> tuple[str, ...]:
    """四个正式终态（小写词表）。"""
    return tuple(load().OUTCOME_SET)


def shippable_outcomes() -> tuple[str, ...]:
    """允许进入成品流程的终态白名单（只有 success）。"""
    return tuple(load().SHIPPABLE_OUTCOMES)


def normalize(outcome: Any) -> str:
    """把任意 token 归一化成四个正式终态之一；**未知 token 一律 failed**。

    绝不返回 ``None`` 或原样透传未知值：调用方据此落终态时必须得到一个确定结果，
    否则「未知终态」会被当成「还没结束」而让任务永远停在非终止态。
    """
    return str(load().normalize(outcome))


def is_shippable(outcome: Any) -> bool:
    """只有 success 可交付。"""
    return bool(load().is_shippable(outcome))


def done_token(outcome: Any) -> str:
    """终态对应的 ``render.done`` 首行 token（大写）。"""
    return str(load().done_token(outcome))


def outcome_label(outcome: Any) -> str:
    """中文说明（用于日志与前端结果区）。"""
    return str(load().OUTCOME_LABEL.get(normalize(outcome), normalize(outcome)))


def result_payload(outcome: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """结构化终态载荷（与固定脚本写 ``run_result.json`` 用同一函数）。"""
    return dict(load().result_payload(outcome, extra))


def token_to_outcome(token: str) -> str:
    """``render.done`` 首行 token（大写）→ 归一化终态。未知 token 归为 failed。"""
    return normalize(str(token).strip())


def valid_tokens() -> frozenset[str]:
    """合法 token 集合（``SUCCESS`` / ``REJECTED`` / ``DIAGNOSTIC`` / ``FAILED``）。

    用于在归一化**之前**识别「未知终态 token」——归一化会把未知值一并压成
    ``failed``，从而分不清「脚本明确说失败」和「脚本说了听不懂的话」。
    后者必须单独标注，否则契约升级时会被静默当成普通失败。
    """
    module = load()
    return frozenset(module.done_token(o) for o in module.OUTCOME_SET)


def is_known_token(token: str) -> bool:
    return str(token).strip().upper() in valid_tokens()
