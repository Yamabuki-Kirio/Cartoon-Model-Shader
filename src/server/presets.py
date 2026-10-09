"""预设（preset）：把一份完整草稿存成本地文件。

存盘位置
--------
``%LOCALAPPDATA%\\Cartoon-Model-Shader\\presets``（由 ``config.presets_dir`` 覆盖）。
**接口层不接受任何路径参数**，客户端也无法影响目录与文件名：

* 目录由服务端配置决定；
* 文件名由服务端**从名称派生**（ASCII 摘要 + 内容哈希），中文名照样支持；
* 名称只参与「显示」与「去重」，绝不参与路径拼接 —— 这样路径穿越在结构上就不可能发生，
  而不是靠事后过滤 ``..`` 来补救。

原子写入
--------
同目录建临时文件 → ``flush`` + ``fsync`` → ``os.replace``。任何失败路径都会清掉临时文件，
因此**不会留下半截 JSON**，也不会出现「读到一半的预设」。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import errors
from .redact import redact

PRESET_SCHEMA = "toon-tuner-preset/1"
PRESET_DIR_NAME = "Cartoon-Model-Shader"
PRESET_SUBDIR = "presets"

#: 名称长度上限（按字符数，中文名同样适用）
NAME_MAX_LENGTH = 64
#: 名称里不允许出现的字符：路径分隔符 + Windows 文件名保留字符
_FORBIDDEN_CHARS = set('<>:"/\\|?*') | {"\x00"}
#: Windows 设备名（不区分大小写，带扩展名也不算）
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def default_preset_dir() -> Path:
    """默认预设目录：``%LOCALAPPDATA%\\Cartoon-Model-Shader\\presets``。

    非 Windows（或环境变量缺失）时退回用户数据目录，保证在 CI / 其它平台上也能跑测试。
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / PRESET_DIR_NAME / PRESET_SUBDIR


def validate_name(raw: Any) -> str:
    """校验预设名称，返回规范化后的名称。

    允许中文、空格、常见符号；只拒绝**会造成歧义或跨出目录**的输入：
    空名、超长、路径分隔符、Windows 保留字符、控制字符、以及 ``.`` / ``..``。
    """
    if not isinstance(raw, str):
        raise errors.ToonTunerError(errors.PRESET_NAME_INVALID, "预设名称必须是字符串。")

    # 去掉首尾空白；同时拒绝 NBSP 之类的「看起来是空格」的不可见字符
    name = raw.strip().strip("\u200b\ufeff").strip()
    if not name:
        raise errors.ToonTunerError(errors.PRESET_NAME_INVALID, "预设名称不能为空。")
    if len(name) > NAME_MAX_LENGTH:
        raise errors.ToonTunerError(
            errors.PRESET_NAME_INVALID,
            f"预设名称过长（{len(name)} > {NAME_MAX_LENGTH} 字符）。",
        )
    if any(character in _FORBIDDEN_CHARS for character in name):
        raise errors.ToonTunerError(
            errors.PRESET_NAME_INVALID,
            "预设名称不能包含路径分隔符或 Windows 保留字符（< > : \" / \\ | ? *）。",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise errors.ToonTunerError(errors.PRESET_NAME_INVALID, "预设名称不能包含控制字符。")
    if name in (".", "..") or set(name) == {"."}:
        raise errors.ToonTunerError(errors.PRESET_NAME_INVALID, "预设名称不能是纯点号。")
    if name.startswith(".") or name.endswith("."):
        raise errors.ToonTunerError(errors.PRESET_NAME_INVALID, "预设名称不能以点号开头或结尾。")

    stem = name.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        raise errors.ToonTunerError(
            errors.PRESET_NAME_INVALID, f"预设名称不能使用 Windows 设备名：{stem}"
        )
    return name


def preset_id_for(name: str) -> str:
    """由名称派生**稳定**的文件名主干（不含扩展名）。

    同名 → 同 id（因此「保存同名预设」就是更新，而不是堆出重复文件）；
    不同名 → 不同 id（内容哈希兜底，避免 ASCII 摘要相同而互相覆盖）。
    """
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    ascii_part = "".join(
        character.lower() if (character.isascii() and character.isalnum()) else "-"
        for character in unicodedata.normalize("NFKC", name)
    )
    slug = re.sub(r"-{2,}", "-", ascii_part).strip("-")[:32].strip("-")
    return f"{slug}-{digest}" if slug else f"preset-{digest}"


def preset_path(directory: Path, name: str) -> Path:
    """预设文件路径。**唯一**的路径拼装点：只拼「服务端生成的 id + .json」。"""
    return directory / f"{preset_id_for(name)}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """同目录临时文件 + ``os.replace`` 原子替换。"""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    handle = None
    temp_name: str | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=str(directory)
        )
        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temp_name, path)
        temp_name = None
    except OSError as exc:
        raise errors.ToonTunerError(
            errors.PRESET_SAVE_FAILED,
            f"预设写入失败：{redact(str(exc))}",
            details={"reason": exc.__class__.__name__},
        ) from exc
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class PresetStore:
    """预设目录的唯一权威。"""

    directory: Path

    def ensure_dir(self) -> Path:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise errors.ToonTunerError(
                errors.PRESET_SAVE_FAILED,
                f"无法创建预设目录：{redact(str(exc))}",
                details={"reason": exc.__class__.__name__},
            ) from exc
        return self.directory

    # -- 读 ---------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        """列出全部预设（按更新时间倒序）。坏文件跳过，不影响其它预设。"""
        items: list[dict[str, Any]] = []
        if not self.directory.is_dir():
            return items
        for path in sorted(self.directory.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                continue
            items.append(
                {
                    "id": path.stem,
                    "name": name,
                    "created_at": raw.get("created_at"),
                    "updated_at": raw.get("updated_at"),
                    "blender": raw.get("blender"),
                    "baseline_id": raw.get("baseline_id"),
                    "parameter_count": raw.get("parameter_count"),
                    "framing": raw.get("framing") or {},
                }
            )
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items

    # -- 写 ---------------------------------------------------------------
    def save(
        self,
        name: str,
        draft: dict[str, Any],
        *,
        framing: dict[str, Any] | None = None,
        blender: str | None = None,
        baseline_id: str | None = None,
    ) -> dict[str, Any]:
        """保存（同名即更新）一份预设。``draft`` 必须已经过完整校验。"""
        self.ensure_dir()
        path = preset_path(self.directory, name)
        created_at = self._existing_created_at(path)
        now = _now_iso()
        payload = {
            "schema": PRESET_SCHEMA,
            "name": name,
            "created_at": created_at or now,
            "updated_at": now,
            "blender": blender,
            "baseline_id": baseline_id,
            "parameter_count": len(draft),
            "framing": framing or {},
            "draft": draft,
        }
        _atomic_write_json(path, payload)
        return {
            "id": preset_id_for(name),
            "name": name,
            "created_at": payload["created_at"],
            "updated_at": now,
            "parameter_count": len(draft),
            "updated": created_at is not None,
        }

    def _existing_created_at(self, path: Path) -> str | None:
        """更新时保留原始创建时间；坏文件按「新建」处理。"""
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(raw, dict) and isinstance(raw.get("created_at"), str):
            return str(raw["created_at"])
        return None
