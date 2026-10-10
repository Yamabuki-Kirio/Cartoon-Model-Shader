"""工程读写：固定 Blender 代码模板 + 服务端备份。

**Blender 侧只跑固定模板**：``build_project_state_code``（只读）与
``build_save_code``（写盘）。目标路径经 ``repr`` 量化后进入模板，客户端无法注入
Python / 命令 / 自由代码 —— 与 ``blender_ops``、``framing`` 同一套约束。

两个关键实现细节
----------------
* ``check_existing=False``：``bpy.ops.wm.save_as_mainfile`` 默认会在 UI 上弹「是否覆盖」
  确认框，在自动化链路上会卡住会话。二次确认与备份一律由**服务端**负责，因此这里显式关掉它。
* 保存后**必须回读** ``bpy.data.filepath`` 与 ``is_dirty``：Blender 对 ``save_mainfile``
  的语义是「存回当前文件」，只有回读才能证明落盘位置真的是我们期望的那个文件。

备份放在**服务端**（纯文件拷贝）而不是 Blender 侧：``.blend`` 在磁盘上就是普通文件，
拷贝与 Blender 无关，放在服务端才能做到「备份失败 → 绝不进入保存」这一步的确定性判定。
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
from pathlib import Path
from typing import Any

from . import errors
from .blender_ops import JSON_MARKER, extract_json
from .redact import redact

MODE_SAVE_AS = "save_as"
MODE_OVERWRITE = "overwrite"
SAVE_MODES: tuple[str, ...] = (MODE_SAVE_AS, MODE_OVERWRITE)
DEFAULT_SAVE_MODE = MODE_SAVE_AS

#: 工程文件扩展名（大小写不敏感）
BLEND_SUFFIX = ".blend"

#: 备份文件名里的时间戳格式：``model.bak-20261009-162530.blend``。
#: 刻意**保留 .blend 扩展名**，用户把它改名回去即可直接在 Blender 里打开。
BACKUP_STAMP_FORMAT = "%Y%m%d-%H%M%S"


# -- Blender 代码模板 -------------------------------------------------------


def build_project_state_code() -> str:
    """只读：当前工程路径 + 是否有未保存改动。不写任何 ``bpy`` 数据。"""
    return f"""
import bpy, json

_filepath = bpy.data.filepath or ""
print({JSON_MARKER!r} + json.dumps({{
    "filepath": _filepath,
    "is_dirty": bool(bpy.data.is_dirty),
    "is_saved": bool(_filepath) and not bool(bpy.data.is_dirty),
    "blender": bpy.app.version_string,
}}, ensure_ascii=False, default=str))
""".strip()


def build_save_code(target_path: str, mode: str) -> str:
    """写盘：``save_as`` 存到指定绝对路径；``overwrite`` 存回当前工程。

    ``target_path`` 一律以 ``repr`` 量化后拼进模板；失败不抛异常，而是回结构化
    ``failure``，由服务端翻译成稳定错误码。
    """
    if mode not in SAVE_MODES:
        raise errors.ToonTunerError(errors.SAVE_TARGET_INVALID, f"未知保存模式：{mode!r}")

    return f"""
import bpy, json, os

_target = {str(target_path)!r}
_mode = {str(mode)!r}
_before = bpy.data.filepath or ""
_saved = False
_failure = None

try:
    if _mode == "save_as":
        # check_existing=False：不要在 Blender UI 上弹覆盖确认框。
        # 「是否需要二次确认」「要不要先备份」由服务端判定，Blender 侧只负责写。
        bpy.ops.wm.save_as_mainfile(filepath=_target, check_existing=False)
    else:
        bpy.ops.wm.save_mainfile(check_existing=False)
    _saved = True
except Exception as exc:
    _failure = {{"type": type(exc).__name__, "message": str(exc)}}

_after = bpy.data.filepath or ""


def _norm(value):
    try:
        return os.path.normcase(os.path.abspath(value))
    except Exception:
        return value


print({JSON_MARKER!r} + json.dumps({{
    "saved": _saved,
    "failure": _failure,
    "mode": _mode,
    "path_before": _before,
    "path_after": _after,
    "target": _target,
    "path_matches": _norm(_after) == _norm(_target),
    "file_exists": bool(_after) and os.path.isfile(_after),
    "is_dirty_after": bool(bpy.data.is_dirty),
}}, ensure_ascii=False, default=str))
""".strip()


def normalize_project_state(payload: dict[str, Any]) -> dict[str, Any]:
    """把只读回读规整成稳定契约（容忍字段缺失）。"""
    filepath = payload.get("filepath")
    filepath = filepath if isinstance(filepath, str) else ""
    return {
        "filepath": filepath,
        "file_name": Path(filepath).name if filepath else None,
        "is_saved": bool(payload.get("is_saved")),
        "is_dirty": bool(payload.get("is_dirty")),
        "blender": str(payload.get("blender") or "unknown"),
    }


def parse_payload(captured_stdout: str) -> dict[str, Any]:
    """从捕获的 stdout 解析结构化结果（与其它模块同一标记）。"""
    return extract_json(captured_stdout)


# -- 备份 -------------------------------------------------------------------


def backup_path_for(target: Path, when: _dt.datetime | None = None) -> Path:
    """同目录下的备份路径：``<主干>.bak-<时间戳>.blend``。

    **只在 prepare 阶段调用一次**，得到的路径就是展示给用户并绑定进确认令牌的那一条。
    commit 阶段不得重新计算：跨过时间边界（哪怕只差 1 秒）算出来的名字就会不同，
    实际备份位置就与用户确认过的不一致了。
    """
    stamp = (when or _dt.datetime.now()).strftime(BACKUP_STAMP_FORMAT)
    return target.with_name(f"{target.stem}.bak-{stamp}{target.suffix}")


def make_backup(target: Path, backup_path: Path) -> Path:
    """把 ``target`` 复制到 ``backup_path``，返回备份文件路径。

    ``backup_path`` **必须是 prepare 阶段已确认的那一条**（来自确认令牌），
    本函数不重新计算、也不加序号回避：路径漂移会让「用户看到的备份位置」与
    「实际备份位置」不一致，这正是要避免的问题。

    因此若该路径已存在，说明备份会落在别人（或上一次）的文件上，直接拒绝：
    宁可让用户重来一次拿到新时间戳，也不销毁已有备份。
    """
    if not target.is_file():
        raise errors.ToonTunerError(
            errors.BACKUP_FAILED,
            "备份失败：原文件不存在，已拒绝覆盖。",
            details={"target_name": target.name},
        )

    try:
        if backup_path.exists():
            raise errors.ToonTunerError(
                errors.BACKUP_FAILED,
                "备份失败：已确认的备份路径上已存在文件，为避免覆盖已有备份，已拒绝继续。"
                "请重新准备一次（会取到新的时间戳）。",
                details={"target_name": target.name, "backup_name": backup_path.name},
            )
    except OSError as exc:
        raise errors.ToonTunerError(
            errors.BACKUP_FAILED,
            f"备份失败（无法确认备份路径状态：{exc.__class__.__name__}），已拒绝覆盖原文件。",
            details={"target_name": target.name},
        ) from exc

    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup_path)
    except OSError as exc:
        raise errors.ToonTunerError(
            errors.BACKUP_FAILED,
            f"备份失败（{exc.__class__.__name__}），已拒绝覆盖原文件。",
            details={"target_name": target.name, "reason": redact(str(exc))},
        ) from exc

    if not backup_path.is_file():
        raise errors.ToonTunerError(
            errors.BACKUP_FAILED,
            "备份失败：备份文件未落盘，已拒绝覆盖。",
            details={"target_name": target.name},
        )
    return backup_path


# -- 目标路径校验 -----------------------------------------------------------


def validate_save_target(raw: Any) -> Path:
    """校验「另存为」目标：必须是绝对路径且以 ``.blend`` 结尾。"""
    if not isinstance(raw, str) or not raw.strip():
        raise errors.ToonTunerError(
            errors.SAVE_TARGET_INVALID, "另存为必须提供目标路径（绝对路径，以 .blend 结尾）。"
        )
    text = raw.strip().strip('"')
    if "\x00" in text:
        raise errors.ToonTunerError(errors.SAVE_TARGET_INVALID, "目标路径包含非法字符。")

    path = Path(os.path.normpath(os.path.expanduser(text)))
    if not path.is_absolute():
        raise errors.ToonTunerError(
            errors.SAVE_TARGET_INVALID, "目标路径必须是绝对路径（不支持相对路径）。"
        )
    if path.suffix.lower() != BLEND_SUFFIX:
        raise errors.ToonTunerError(
            errors.SAVE_TARGET_INVALID, f"目标文件必须以 {BLEND_SUFFIX} 结尾。"
        )
    if path.is_dir():
        raise errors.ToonTunerError(errors.SAVE_TARGET_INVALID, "目标是目录，不是 .blend 文件。")
    parent = path.parent
    if not parent.is_dir():
        raise errors.ToonTunerError(
            errors.SAVE_TARGET_INVALID, "目标目录不存在，请先在资源管理器里创建它。"
        )
    return path
