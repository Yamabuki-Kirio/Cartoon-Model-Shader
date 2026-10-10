"""日志 / 诊断脱敏：隐藏本机绝对路径与用户名。

安全要求：任何对外返回的诊断信息都不得包含本机绝对路径。

两个层次：

* ``redact`` / ``redact_value`` —— **抹掉**路径（替换为 ``<path>``），用于错误消息、
  日志、traceback 这类「路径本身没有信息量」的地方；
* ``display_path`` —— 把路径**换成逻辑路径**（``<OUTPUT_DIR>`` /
  ``%LOCALAPPDATA%\\…``）。用于「用户确实需要知道是哪个目录」的地方：输出目录、
  备份文件、渲染目标。展示层脱敏，服务端内部仍持有真实绝对路径。
"""

from __future__ import annotations

import os
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


#: 用户目录环境变量 -> 展示占位符。**顺序重要**：LOCALAPPDATA / APPDATA 都在
#: USERPROFILE 之下，先匹配更具体的那个，结果才不是 `%USERPROFILE%\AppData\Local\…`。
_PATH_PLACEHOLDERS = (
    ("LOCALAPPDATA", "%LOCALAPPDATA%"),
    ("APPDATA", "%APPDATA%"),
    ("USERPROFILE", "%USERPROFILE%"),
)


def display_path(path, root=None, root_placeholder: str = "<OUTPUT_DIR>"):
    """绝对路径 → **不含用户名**的逻辑路径（只用于展示，不影响任何读写）。

    规则（按优先级）：

      0. 给了 ``root`` 且 ``path`` 落在它之下 ⇒ ``<OUTPUT_DIR>[\\子路径]``
         —— 渲染输出目录常被建在系统临时目录下，业务语义比物理路径更有用；
      1. 落在 %LOCALAPPDATA% / %APPDATA% / %USERPROFILE% 之下 ⇒ 换成对应占位符；
      2. 其余路径若仍出现当前用户名 ⇒ 只保留文件名（兜底）；
      3. 其它情况原样返回（例如与用户名无关的项目盘素材路径）。

    注意：**保存确认流程的 ``target_path`` 不走这里** —— 覆盖 ``.blend`` 之前必须让
    用户看清真实路径，那是安全特性，不是泄漏。
    """
    if path in (None, ""):
        return path
    text = str(path)
    try:
        full = os.path.abspath(text)
    except Exception:
        return text

    if root not in (None, ""):
        root_abs = os.path.abspath(str(root))
        root_norm = os.path.normcase(root_abs)
        full_norm = os.path.normcase(full)
        if full_norm == root_norm:
            return root_placeholder
        if full_norm.startswith(root_norm + os.sep):
            rest = full[len(root_abs):].lstrip("\\/")
            return root_placeholder + os.sep + rest

    norm = os.path.normcase(full)
    for env_name, placeholder in _PATH_PLACEHOLDERS:
        base = os.environ.get(env_name)
        if not base:
            continue
        base_abs = os.path.abspath(base)
        base_norm = os.path.normcase(base_abs)
        if norm == base_norm or norm.startswith(base_norm + os.sep):
            rest = full[len(base_abs):].lstrip("\\/")
            return placeholder + (os.sep + rest if rest else "")

    username = (os.environ.get("USERNAME") or "").strip()
    if username and username.lower() in full.lower():
        return os.path.basename(full) or full
    return full
