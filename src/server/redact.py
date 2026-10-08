"""日志 / 诊断脱敏：隐藏本机绝对路径与用户名。

安全要求：任何对外返回的诊断信息都不得包含本机绝对路径。
"""

from __future__ import annotations

import re

# Windows 盘符路径、UNC 路径、类 Unix 用户目录
_PATTERNS = [
    re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]*"),   # 盘符路径，例如 <盘符>:\dir\file
    re.compile(r"\\\\[^\s\"'<>|]+"),              # \\server\share
    re.compile(r"/(?:Users|home)/[^\s\"'<>|]*"),  # /Users/xxx /home/xxx
]


def redact(text: str, *, replacement: str = "<path>") -> str:
    """把文本中的本机绝对路径替换为占位符。"""
    if not text:
        return text
    result = text
    for pattern in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_value(value):
    """递归脱敏嵌套结构中的字符串。"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    return value
