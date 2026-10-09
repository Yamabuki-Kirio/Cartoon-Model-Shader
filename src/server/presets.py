"""MVP-03：本地预设存储与校验。

存储位置（**不进仓库**，也不把浏览器 ``localStorage`` 当唯一存储）::

    Windows : %LOCALAPPDATA%\\CartoonModelShader\\presets\\
    兜底     : ~/.local/share/CartoonModelShader/presets/   （仅当 LOCALAPPDATA 缺失时）

可用 ``TOON_TUNER_PRESET_DIR`` 环境变量整体覆盖（测试与自定义部署用）。

安全边界
--------
* 预设**只**保存参数取值与运行设置。禁止出现：``.blend`` 绝对路径、用户目录、
  模型名或贴图路径、MCP Token、临时预览路径。写入前逐项扫描，命中即**拒绝**，
  不做静默清洗（清洗会让用户以为存了、其实被改了）。
* 校验失败一律抛稳定错误码，绝不「尽力而为」地部分应用；旧 schema 明确报错，
  不静默迁移、不静默丢弃。
* 文件名由预设名派生（方便人工浏览 ``presets`` 目录），但**稳定身份**是文件内的
  ``preset_id``：重命名只改名字与文件名，身份不变，前端引用不会失效。
* 对外响应只给**可展示**的存储位置（``%LOCALAPPDATA%\\...``），不给本机绝对路径。
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from . import color_looks, errors, framing, params

# ---- schema ---------------------------------------------------------------

PRESET_SCHEMA = "toon-tuner-preset/1"
#: 只接受列出的版本。旧版/未知版本一律显式报错（``PRESET_SCHEMA_UNSUPPORTED``）。
SUPPORTED_SCHEMAS: tuple[str, ...] = (PRESET_SCHEMA,)
PRESET_PROTOCOL = "toon-tuner-presets/1"

#: 预设文件里允许出现的顶层字段（多余字段一律拒绝）
PRESET_FIELDS: tuple[str, ...] = (
    "schema",
    "preset_id",
    "name",
    "created_at",
    "updated_at",
    "pipeline_mode",
    "framing_mode",
    "framing_margin",
    "preview_quality",
    "parameters",
)
#: 单个参数条目允许出现的字段
PARAMETER_FIELDS: tuple[str, ...] = ("configured_value", "effective_value", "active")

# ---- 存储位置 -------------------------------------------------------------

ENV_PRESET_DIR = "TOON_TUNER_PRESET_DIR"
APP_DIR_NAME = "CartoonModelShader"
PRESET_DIR_NAME = "presets"

# ---- 运行设置白名单 -------------------------------------------------------

DEFAULT_PIPELINE_MODE = "faithful"
PIPELINE_MODES: tuple[str, ...] = ("faithful", "enhanced")

DEFAULT_PREVIEW_QUALITY = "standard"
#: 预览质量档位（工具**运行设置**，不写进 Blender 工程）。
#:
#: * ``nominal_resolution`` —— 需求书给的名义分辨率（按 9:16.5 竖幅给出）；
#: * ``long_side``         —— 实际渲染时使用的长边：预览必须与取景诊断共用同一画面形状，
#:   所以按工程纵横比等比缩放到这个长边，而不强行套用名义宽高。
#: * ``samples``           —— 渲染采样数。
PREVIEW_QUALITY_TIERS: dict[str, dict[str, Any]] = {
    "fast": {
        "label": "快速",
        "nominal_resolution": [360, 660],
        "long_side": 660,
        "samples": 4,
    },
    "standard": {
        "label": "标准",
        "nominal_resolution": [540, 990],
        "long_side": 990,
        "samples": 8,
    },
    "high": {
        "label": "高质量",
        "nominal_resolution": [1080, 1980],
        "long_side": 1980,
        "samples": 16,
    },
}

NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 80
_VALUE_TEXT_MAX_LENGTH = 200


def quality_tiers() -> dict[str, dict[str, Any]]:
    """档位表的深拷贝（调用方改不动常量）。"""
    return {name: json.loads(json.dumps(tier)) for name, tier in PREVIEW_QUALITY_TIERS.items()}


# =============================================================================
#  存储位置
# =============================================================================


def presets_dir() -> Path:
    """预设目录。优先环境变量覆盖，其次 ``%LOCALAPPDATA%``，最后兜底到用户目录。"""
    override = os.environ.get(ENV_PRESET_DIR)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_DIR_NAME / PRESET_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME / PRESET_DIR_NAME


CUSTOM_DIR_DISPLAY = "<自定义预设目录>"


def _default_display() -> str:
    """默认位置的展示写法：只用占位符，**不展开**本机路径。"""
    if os.environ.get(ENV_PRESET_DIR):
        return "%TOON_TUNER_PRESET_DIR%"
    if os.environ.get("LOCALAPPDATA"):
        return "%LOCALAPPDATA%\\" + APP_DIR_NAME + "\\" + PRESET_DIR_NAME
    return "~/.local/share/" + APP_DIR_NAME + "/" + PRESET_DIR_NAME


def storage_display() -> str:
    """默认预设目录的展示写法。"""
    return _default_display()


def display_for(directory: Path | str) -> str:
    """任意预设目录的展示写法。

    **只有**等于默认位置时才给出占位符写法；其余一律回中性文案。
    宁可少说，也不把 ``%LOCALAPPDATA%`` 之外的相对部分抖出来 —— 那些部分
    可能嵌套着用户名（例如系统临时目录），拼出来就是一条本机路径线索。
    """
    try:
        if Path(directory).resolve() == presets_dir().resolve():
            return _default_display()
    except OSError:  # pragma: no cover - 目录不可解析时按自定义处理
        pass
    return CUSTOM_DIR_DISPLAY


# =============================================================================
#  敏感内容扫描（禁止入库的东西）
# =============================================================================

_FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[A-Za-z]:[\\/]"), "本机盘符绝对路径"),
    (re.compile(r"\\\\[^\s]"), "UNC 网络路径"),
    (re.compile(r"(?i)(?:^|[\s\"'=(,])/(?:Users|home)/"), "用户主目录路径"),
    (re.compile(r"(?i)\.blend\b"), "Blender 工程文件引用"),
    (re.compile(r"(?i)\bAppData\b"), "本机 AppData 路径"),
    (re.compile(r"(?i)%(?:TEMP|TMP|USERPROFILE|APPDATA|LOCALAPPDATA)%"), "本机环境变量路径"),
    (re.compile(r"(?i)\btoon-tuner-previews\b"), "临时预览目录"),
    (
        re.compile(
            r"(?i)[\\/][^\s\"']*\.(?:png|jpe?g|tga|exr|psd|tiff?|bmp|webp|vmd|vpd|pmx)\b"
        ),
        "贴图/模型文件路径",
    ),
    # 凭据：出现在**取值**里也要拦住（MCP Token 绝不该写进预设）
    (re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}"), "疑似访问令牌"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "疑似 API 密钥"),
    (re.compile(r"(?i)\b(?:bearer|token|api[_-]?key)\s*[:=]\s*[A-Za-z0-9._\-]{16,}"), "疑似凭据赋值"),
)

#: 疑似凭据的字段名（只看键名，避免把正常取值当密钥）
_FORBIDDEN_KEY = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|credential|auth)")


def scan_forbidden(preset: Any) -> list[dict[str, str]]:
    """扫描预设里不允许出现的内容，返回命中清单（空列表 = 干净）。"""
    findings: list[dict[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if isinstance(key, str) else f"{path}[{key!r}]"
                if isinstance(key, str) and _FORBIDDEN_KEY.search(key):
                    findings.append({"path": child, "reason": "疑似凭据字段名"})
                walk(value, child)
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            for pattern, reason in _FORBIDDEN_PATTERNS:
                if pattern.search(node):
                    findings.append({"path": path, "reason": reason})
                    break

    walk(preset, "$")
    return findings


# =============================================================================
#  文件名（由预设名派生）
# =============================================================================

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_SLUG_MAX_LENGTH = 60


def slugify(name: str, *, max_length: int = _SLUG_MAX_LENGTH) -> str:
    """把预设名净化为安全文件名词干。

    保留中文（Windows / macOS / Linux 的文件系统都支持 UTF-8 文件名），
    只处理真正危险的部分：Windows 保留字符、控制字符、结尾的点与空格、
    Windows 设备名。**净化的只是文件名，预设里的 ``name`` 原样保留。**
    """
    cleaned = _INVALID_FILENAME_CHARS.sub("-", str(name))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-. ")
    if not cleaned:
        cleaned = "preset"
    if cleaned.upper() in _RESERVED_STEMS:
        cleaned = f"{cleaned}-preset"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip("-. ")
        if not cleaned:
            cleaned = "preset"
    return cleaned


# =============================================================================
#  校验
# =============================================================================


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _invalid(message: str, **details: Any) -> errors.ToonTunerError:
    return errors.ToonTunerError(errors.PRESET_INVALID, message, details=details or None)


def _as_text(
    value: Any, field: str, *, max_length: int | None = None, allow_empty: bool = False
) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field} 必须是字符串。")
    text = value.strip()
    if not text and not allow_empty:
        raise _invalid(f"{field} 不能为空。")
    if max_length is not None and len(text) > max_length:
        raise _invalid(f"{field} 超长（最多 {max_length} 个字符，收到 {len(text)} 个）。")
    return text


def _as_timestamp(value: Any, field: str) -> str:
    if value in (None, ""):
        return _now_iso()
    text = _as_text(value, field)
    try:
        _dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise _invalid(f"{field} 不是合法的 ISO 时间：{text!r}") from exc
    return text


def _as_choice(value: Any, field: str, allowed: tuple[str, ...], default: str) -> str:
    if value in (None, ""):
        return default
    text = _as_text(value, field)
    if text not in allowed:
        raise _invalid(f"{field} 必须是 {list(allowed)} 之一，收到 {text!r}。")
    return text


def _as_margin(value: Any) -> float:
    if value in (None, ""):
        return framing.DEFAULT_MARGIN
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid("framing_margin 必须是数字。")
    margin = float(value)
    if not math.isfinite(margin):
        raise _invalid("framing_margin 必须是有限数值。")
    if margin < framing.MARGIN_MIN or margin > framing.MARGIN_MAX:
        raise _invalid(
            f"framing_margin 需在 {framing.MARGIN_MIN:g}–{framing.MARGIN_MAX:g} 之间，"
            f"收到 {margin:g}。"
        )
    return margin


def _check_value(spec: params.ParamSpec, value: Any, field: str) -> Any:
    """按参数声明校验单个取值。

    * ``None`` 一律允许（表示基线里该参数本来就不可读，例如没有辉光节点组）。
    * 浮点：类型 + 有限性 + ``minimum``/``maximum``（范围来自 ``ParamSpec``，与提交预览
      时用的是同一份声明）。
    * 枚举：只做**结构性**校验（非空字符串、长度上限），**不**比对本机 Blender 的枚举。
      预设要能跨 Blender 版本/OCIO 配置使用，把本机枚举写死进预设反而是错的；
      真正的合法性在提交预览时由服务端对着**当时的**能力表判定。
    """
    if value is None:
        return None
    if spec.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _invalid(f"{spec.id} 的 {field} 必须是数字。")
        number = float(value)
        if not math.isfinite(number):
            raise _invalid(f"{spec.id} 的 {field} 必须是有限数值。")
        if spec.minimum is not None and number < spec.minimum:
            raise _invalid(f"{spec.id} 的 {field} = {number} 低于下限 {spec.minimum}。")
        if spec.maximum is not None and number > spec.maximum:
            raise _invalid(f"{spec.id} 的 {field} = {number} 高于上限 {spec.maximum}。")
        return number
    if not isinstance(value, str):
        raise _invalid(f"{spec.id} 的 {field} 必须是字符串。")
    if not value.strip():
        raise _invalid(f"{spec.id} 的 {field} 不能为空字符串。")
    if len(value) > _VALUE_TEXT_MAX_LENGTH:
        raise _invalid(f"{spec.id} 的 {field} 超长（最多 {_VALUE_TEXT_MAX_LENGTH} 个字符）。")
    return value


def _normalize_parameter(param_id: Any, raw: Any) -> dict[str, Any]:
    if not isinstance(param_id, str):
        raise _invalid("参数 id 必须是字符串。")
    spec = params.get(param_id)
    if spec is None:
        raise _invalid(
            f"参数 {param_id!r} 不在白名单内。预设只允许保存已开放的参数。",
            parameter=param_id,
        )

    if isinstance(raw, dict):
        unknown = sorted(set(raw) - set(PARAMETER_FIELDS))
        if unknown:
            raise _invalid(f"参数 {param_id} 含未知字段：{unknown}。", parameter=param_id)
        configured = raw.get("configured_value")
        effective = raw.get("effective_value", configured)
        active = raw.get("active", True)
    else:
        # 简写：直接给一个取值 —— 视作 configured = effective，且 active。
        configured = raw
        effective = raw
        active = True

    if not isinstance(active, bool):
        raise _invalid(f"参数 {param_id} 的 active 必须是布尔值。", parameter=param_id)

    return {
        "configured_value": _check_value(spec, configured, "configured_value"),
        "effective_value": _check_value(spec, effective, "effective_value"),
        "active": active,
    }


def normalize_preset(
    raw: Any,
    *,
    preset_id: str | None = None,
    new_id: bool = False,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """校验并规范成**唯一**的预设结构。

    身份优先级：``new_id=True``（**总是**生成新身份，复制用）> 显式 ``preset_id``
    > ``raw`` 里的 ``preset_id`` > 新生成。``created_at`` / ``updated_at`` 同理，
    显式传入优先于 ``raw``。

    注意：``preset_id=None`` 是「未指定」，会在 ``raw`` 里找回原身份 —— 这与
    「生成新身份」是两件事，复制必须显式传 ``new_id=True``，否则会复制出同身份的
    两个文件（读目录时被判为重复而丢弃，表现为「复制出来的预设莫名消失」）。
    """
    if not isinstance(raw, dict):
        raise _invalid("预设必须是 JSON 对象。")

    schema = raw.get("schema")
    if schema is None:
        # 字段缺失或显式 null 都按当前版本处理；空串/非字符串一律判为不支持，
        # 不做「猜一个版本」的兜底 —— 猜错就等于静默改变取值。
        schema = PRESET_SCHEMA
    if not isinstance(schema, str) or schema not in SUPPORTED_SCHEMAS:
        raise errors.ToonTunerError(
            errors.PRESET_SCHEMA_UNSUPPORTED,
            f"不支持的预设 schema：{schema!r}。当前只接受 {list(SUPPORTED_SCHEMAS)}。"
            "为避免静默改变取值，这里不会自动迁移；请用对应版本的工具打开后另存。",
            details={"schema": schema, "supported": list(SUPPORTED_SCHEMAS)},
        )

    unknown = sorted(set(raw) - set(PRESET_FIELDS))
    if unknown:
        raise _invalid(f"预设含未知字段：{unknown}。", unknown=unknown)

    name = _as_text(raw.get("name"), "name", max_length=NAME_MAX_LENGTH)
    if len(name) < NAME_MIN_LENGTH:  # pragma: no cover - _as_text 已挡空串
        raise _invalid(f"name 至少需要 {NAME_MIN_LENGTH} 个字符。")

    parameters_raw = raw.get("parameters", {})
    if not isinstance(parameters_raw, dict):
        raise _invalid("parameters 必须是对象（参数 id -> 取值）。")
    parameters = {pid: _normalize_parameter(pid, value) for pid, value in parameters_raw.items()}

    if new_id:
        resolved_id = uuid.uuid4().hex[:12]
    else:
        candidate = preset_id if preset_id is not None else raw.get("preset_id")
        resolved_id = uuid.uuid4().hex[:12] if candidate in (None, "") else _as_text(
            candidate, "preset_id", max_length=64
        )

    preset = {
        "schema": PRESET_SCHEMA,
        "preset_id": resolved_id,
        "name": name,
        "created_at": _as_timestamp(
            created_at if created_at is not None else raw.get("created_at"), "created_at"
        ),
        "updated_at": _as_timestamp(
            updated_at if updated_at is not None else raw.get("updated_at"), "updated_at"
        ),
        "pipeline_mode": _as_choice(
            raw.get("pipeline_mode"), "pipeline_mode", PIPELINE_MODES, DEFAULT_PIPELINE_MODE
        ),
        "framing_mode": _as_choice(
            raw.get("framing_mode"), "framing_mode", tuple(framing.FRAMING_MODES), framing.DEFAULT_MODE
        ),
        "framing_margin": _as_margin(raw.get("framing_margin")),
        "preview_quality": _as_choice(
            raw.get("preview_quality"),
            "preview_quality",
            tuple(PREVIEW_QUALITY_TIERS),
            DEFAULT_PREVIEW_QUALITY,
        ),
        "parameters": parameters,
    }

    findings = scan_forbidden(preset)
    if findings:
        reasons = sorted({item["reason"] for item in findings})
        raise errors.ToonTunerError(
            errors.PRESET_INVALID,
            "预设包含禁止保存的内容（" + "、".join(reasons) + "）。"
            "预设立场是不携带任何本机路径、模型名与凭据。",
            details={"findings": findings[:20], "count": len(findings)},
        )
    return preset


def summary_of(preset: dict[str, Any]) -> dict[str, Any]:
    """列表用的摘要（不含逐参数明细）。"""
    parameters = preset.get("parameters") or {}
    return {
        "preset_id": preset["preset_id"],
        "name": preset["name"],
        "schema": preset["schema"],
        "created_at": preset["created_at"],
        "updated_at": preset["updated_at"],
        "pipeline_mode": preset["pipeline_mode"],
        "framing_mode": preset["framing_mode"],
        "framing_margin": preset["framing_margin"],
        "preview_quality": preset["preview_quality"],
        "parameter_count": len(parameters),
        "active_count": sum(1 for entry in parameters.values() if entry.get("active")),
    }


# =============================================================================
#  加载 → 草稿
# =============================================================================


def materialize_draft(
    preset: dict[str, Any],
    *,
    baseline_values: dict[str, Any] | None = None,
    look_map: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    """把预设整理成一份「可直接提交的草稿」。

    * 只取 ``active`` 的参数 —— ``active: false`` 表示只作记录、不参与应用；
    * 优先用 ``effective_value``（真正写进过 Blender 的真实 identifier），
      缺省时退回 ``configured_value``（旧预设里可能只有这个，且可能是显示标签）；
    校验强度取决于**能否判定视图变换**：

    * 预设自带 ``color.view_transform``，或调用方给了 ``baseline_values`` → 拿得到视图，
      且该视图在 ``look_map`` 里有记录 → **严格校验**，没有等价项就抛
      ``INVALID_DEPENDENT_ENUM``（列出可选档位），绝不静默套用非法值；
    * 拿不到视图（预设没设 + 没给基线）、``look_map`` 为空、或该视图不在能力表里
      → 判不了就是判不了，**带出原值并记一条 ``note`` 说明延后校验**。
      这不是静默放行：提交预览时服务端仍会对着**当时的**能力表硬校验并报错。
    """
    baseline_values = baseline_values or {}
    draft: dict[str, Any] = {}
    notes: list[dict[str, Any]] = []

    for param_id, entry in (preset.get("parameters") or {}).items():
        if not entry.get("active"):
            notes.append(
                {
                    "parameter": param_id,
                    "reason": "inactive",
                    "detail": "预设中标记为 active=false，不参与应用。",
                }
            )
            continue
        effective = entry.get("effective_value")
        configured = entry.get("configured_value")
        value = effective if effective is not None else configured
        if value is None:
            notes.append(
                {
                    "parameter": param_id,
                    "reason": "no_value",
                    "detail": "预设中该参数没有可用取值，已跳过。",
                }
            )
            continue
        if configured is not None and effective is not None and configured != effective:
            notes.append(
                {
                    "parameter": param_id,
                    "reason": "migrated_on_save",
                    "from": configured,
                    "to": effective,
                    "detail": "预设保存时记录的配置值与实际生效值不同，按实际生效值加载。",
                }
            )
        draft[param_id] = value

    if "color.look" in draft:
        view_transform = draft.get("color.view_transform")
        if view_transform is None:
            view_transform = baseline_values.get("color.view_transform")
        view_transform = str(view_transform) if view_transform is not None else None

        defer_reason: str | None = None
        if not look_map:
            defer_reason = "look_map_unavailable"
            detail = "尚无 look 能力表（未建立基线），无法判定该档位是否合法，提交预览时再校验。"
        elif view_transform is None:
            defer_reason = "view_transform_unknown"
            detail = (
                "预设与基线都没有给出「视图变换」，无法判定该 Look 是否合法，"
                "提交预览时再校验。"
            )
        elif view_transform not in look_map:
            defer_reason = "view_transform_not_in_capability"
            detail = (
                f"能力表里没有视图 {view_transform!r} 的记录，无法判定该 Look 是否合法，"
                "提交预览时再校验。"
            )

        if defer_reason is not None:
            notes.append(
                {
                    "parameter": "color.look",
                    "reason": defer_reason,
                    "value": draft["color.look"],
                    "view_transform": view_transform,
                    "detail": detail,
                }
            )
        else:
            outcome = color_looks.resolve_look(draft["color.look"], view_transform, look_map)
            if not outcome["ok"]:
                raise color_looks.invalid_dependent_enum(
                    "color.look",
                    draft["color.look"],
                    {"color.view_transform": view_transform},
                    outcome.get("allowed") or [],
                    view_transform=view_transform,
                )
            if outcome["migrated"]:
                notes.append(
                    {
                        "parameter": "color.look",
                        "reason": outcome["reason"],
                        "from": draft["color.look"],
                        "to": outcome["value"],
                        "view_transform": view_transform,
                    }
                )
            draft["color.look"] = outcome["value"]

    return {"draft": draft, "notes": notes}


# =============================================================================
#  存储
# =============================================================================


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + ``os.replace``：写入过程崩溃也不会留下半个 JSON。"""
    directory = path.parent
    handle, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover - 清理失败不再掩盖原始异常
            pass
        raise


class PresetStore:
    """预设目录的唯一读写入口。

    同步实现（本地小文件），由调用方放进线程池；内部用 ``threading.Lock`` 串行化写入。
    """

    def __init__(self, directory: Path | str | None = None, *, display: str | None = None) -> None:
        self._directory = Path(directory) if directory is not None else presets_dir()
        self._display = display if display is not None else display_for(self._directory)
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def display(self) -> str:
        """可展示的存储位置（**不含**本机绝对路径）。"""
        return self._display

    # -- 基础 -----------------------------------------------------------
    def ensure(self) -> Path:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise errors.ToonTunerError(
                errors.PRESET_STORAGE_ERROR,
                f"无法创建预设目录（{self._display}）：{exc.__class__.__name__}",
            ) from exc
        return self._directory

    def _files(self) -> list[Path]:
        """目录里的预设文件。

        显式排除点开头文件：``Path.glob("*.json")`` **会**匹配 ``.tmp-xxx.json``
        （与 shell 不同），若不排除，一次写入中途崩溃留下的临时文件就会被当成
        「一个坏预设」摆到界面上。
        """
        if not self._directory.is_dir():
            return []
        return sorted(
            path
            for path in self._directory.glob("*.json")
            if path.is_file() and not path.name.startswith(".")
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise errors.ToonTunerError(
                errors.PRESET_STORAGE_ERROR, f"无法读取预设文件 {path.name}。"
            ) from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise errors.ToonTunerError(
                errors.PRESET_INVALID, f"预设文件 {path.name} 不是合法 JSON。"
            ) from exc
        if not isinstance(data, dict):
            raise errors.ToonTunerError(
                errors.PRESET_INVALID, f"预设文件 {path.name} 的顶层必须是 JSON 对象。"
            )
        return data

    def _write(self, preset: dict[str, Any], *, replacing: Path | None = None) -> Path:
        self.ensure()
        target = self._directory / f"{slugify(preset['name'])}.json"
        target = self._unique_path(target, preset_id=preset["preset_id"], replacing=replacing)
        payload = json.dumps(preset, ensure_ascii=False, indent=2) + "\n"
        try:
            _atomic_write_text(target, payload)
        except OSError as exc:
            raise errors.ToonTunerError(
                errors.PRESET_STORAGE_ERROR,
                f"写入预设失败（{self._display}）：{exc.__class__.__name__}",
            ) from exc
        if replacing is not None and replacing != target and replacing.is_file():
            try:
                replacing.unlink()
            except OSError as exc:  # pragma: no cover - 旧文件删不掉不影响新文件已落盘
                raise errors.ToonTunerError(
                    errors.PRESET_STORAGE_ERROR,
                    f"预设已写入 {target.name}，但旧文件 {replacing.name} 删除失败。",
                ) from exc
        return target

    def _unique_path(
        self, target: Path, *, preset_id: str, replacing: Path | None = None
    ) -> Path:
        """避免「净化后文件名相同」的预设互相覆盖。"""
        if not target.exists() or target == replacing:
            return target
        try:
            existing = self._read_json(target)
        except errors.ToonTunerError:
            existing = {}
        if str(existing.get("preset_id")) == preset_id:
            return target
        stem = target.stem
        for index in range(2, 1000):
            candidate = target.with_name(f"{stem}-{index}.json")
            if not candidate.exists() or candidate == replacing:
                return candidate
        raise errors.ToonTunerError(
            errors.PRESET_STORAGE_ERROR, f"预设目录里同名文件过多，无法为 {stem} 找到可用文件名。"
        )

    def _find(self, preset_id: str) -> tuple[Path, dict[str, Any]] | None:
        wanted = str(preset_id)
        for path in self._files():
            try:
                raw = self._read_json(path)
            except errors.ToonTunerError:
                continue
            if str(raw.get("preset_id")) == wanted:
                return path, raw
        return None

    def _require(self, preset_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        found = self._find(preset_id)
        if found is None:
            raise errors.ToonTunerError(
                errors.PRESET_NOT_FOUND, f"预设不存在：{preset_id}"
            )
        path, raw = found
        return path, raw, normalize_preset(raw)

    @staticmethod
    def _name_taken(name: str, existing: list[dict[str, Any]], *, skip_id: str | None) -> bool:
        target = name.strip().casefold()
        for preset in existing:
            if skip_id is not None and preset["preset_id"] == skip_id:
                continue
            if preset["name"].casefold() == target:
                return True
        return False

    def _valid_presets(self) -> list[dict[str, Any]]:
        return [item["preset"] for item in self._scan()["valid"]]

    def _scan(self) -> dict[str, Any]:
        """读全目录：合法的按 id 去重，非法的收进 ``skipped``（附原因，绝不静默丢）。"""
        valid: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        seen: dict[str, str] = {}
        for path in self._files():
            try:
                raw = self._read_json(path)
                preset = normalize_preset(raw)
            except errors.ToonTunerError as exc:
                skipped.append({"file": path.name, "code": exc.code, "message": exc.message})
                continue
            preset_id = preset["preset_id"]
            if preset_id in seen:
                skipped.append(
                    {
                        "file": path.name,
                        "code": errors.PRESET_INVALID,
                        "message": f"preset_id 与 {seen[preset_id]} 重复，已忽略本文件。",
                    }
                )
                continue
            seen[preset_id] = path.name
            valid.append({"file": path.name, "preset": preset})
        return {"valid": valid, "skipped": skipped}

    # -- 对外动作 -------------------------------------------------------
    def list(self) -> dict[str, Any]:
        scan = self._scan()
        presets = [summary_of(item["preset"]) for item in scan["valid"]]
        presets.sort(key=lambda item: item["name"].casefold())
        return {
            "storage": self._display,
            "presets": presets,
            "skipped": scan["skipped"],
            "quality_tiers": quality_tiers(),
            "pipeline_modes": list(PIPELINE_MODES),
            "framing_modes": list(framing.FRAMING_MODES),
            "defaults": {
                "pipeline_mode": DEFAULT_PIPELINE_MODE,
                "framing_mode": framing.DEFAULT_MODE,
                "framing_margin": framing.DEFAULT_MARGIN,
                "preview_quality": DEFAULT_PREVIEW_QUALITY,
            },
        }

    def get(self, preset_id: str) -> dict[str, Any]:
        _, _, preset = self._require(preset_id)
        return preset

    def save(self, payload: Any, *, preset_id: str | None = None) -> dict[str, Any]:
        """新建或覆盖保存。``preset_id`` 给定时为覆盖（身份与 created_at 保留）。"""
        with self._lock:
            existing = self._valid_presets()
            replacing: Path | None = None
            created_at: str | None = None
            target_id = preset_id
            if preset_id is not None:
                path, _, current = self._require(preset_id)
                replacing = path
                created_at = current["created_at"]
            preset = normalize_preset(
                payload, preset_id=target_id, created_at=created_at, updated_at=_now_iso()
            )
            if self._name_taken(preset["name"], existing, skip_id=preset["preset_id"]):
                raise errors.ToonTunerError(
                    errors.PRESET_NAME_CONFLICT,
                    f"已存在同名预设 {preset['name']!r}，请换一个名字或先重命名原有预设。",
                    details={"name": preset["name"]},
                )
            self._write(preset, replacing=replacing)
            return preset

    def rename(self, preset_id: str, name: Any) -> dict[str, Any]:
        """重命名：**身份不变**，只改 name 与文件名。"""
        with self._lock:
            path, raw, current = self._require(preset_id)
            new_name = _as_text(name, "name", max_length=NAME_MAX_LENGTH)
            others = [p for p in self._valid_presets() if p["preset_id"] != current["preset_id"]]
            if self._name_taken(new_name, others, skip_id=None):
                raise errors.ToonTunerError(
                    errors.PRESET_NAME_CONFLICT,
                    f"已存在同名预设 {new_name!r}，请换一个名字。",
                    details={"name": new_name},
                )
            updated = normalize_preset(
                {**raw, "name": new_name},
                preset_id=current["preset_id"],
                created_at=current["created_at"],
                updated_at=_now_iso(),
            )
            self._write(updated, replacing=path)
            return updated

    def duplicate(self, preset_id: str, name: Any = None) -> dict[str, Any]:
        """复制成**新**身份的新预设；默认名字加「副本」后缀。"""
        with self._lock:
            _, _, current = self._require(preset_id)
            existing = self._valid_presets()
            if name in (None, ""):
                base = _copy_name(current["name"])
                candidate = base
                index = 2
                while self._name_taken(candidate, existing, skip_id=None):
                    candidate = f"{base} {index}"
                    index += 1
                    if index > 999:  # pragma: no cover - 防御性上限
                        raise errors.ToonTunerError(
                            errors.PRESET_NAME_CONFLICT, "副本名字冲突过多，请显式指定名称。"
                        )
                new_name = candidate
            else:
                new_name = _as_text(name, "name", max_length=NAME_MAX_LENGTH)
                if self._name_taken(new_name, existing, skip_id=None):
                    raise errors.ToonTunerError(
                        errors.PRESET_NAME_CONFLICT,
                        f"已存在同名预设 {new_name!r}，请换一个名字。",
                        details={"name": new_name},
                    )
            now = _now_iso()
            copy = normalize_preset(
                {**current, "name": new_name},
                new_id=True,
                created_at=now,
                updated_at=now,
            )
            self._write(copy)
            return copy

    def delete(self, preset_id: str) -> dict[str, Any]:
        with self._lock:
            path, _, preset = self._require(preset_id)
            try:
                path.unlink()
            except OSError as exc:
                raise errors.ToonTunerError(
                    errors.PRESET_STORAGE_ERROR, f"删除预设失败：{path.name}"
                ) from exc
            return summary_of(preset)


def _copy_name(name: str, *, suffix: str = "副本") -> str:
    """复制时的默认名，且**不会**撞破长度上限。"""
    candidate = f"{name} {suffix}"
    if len(candidate) <= NAME_MAX_LENGTH:
        return candidate
    keep = max(1, NAME_MAX_LENGTH - len(suffix) - 1)
    return f"{name[:keep].rstrip()} {suffix}"
