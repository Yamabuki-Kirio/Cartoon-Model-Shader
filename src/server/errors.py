"""稳定错误码与统一错误类型。

错误码面向界面展示，必须保持稳定；诊断详情（原始响应、traceback）单独放在
`details` 里，前端默认折叠，且不直接展示本机绝对路径。
"""

from __future__ import annotations

from typing import Any

# ---- 稳定错误码 ----------------------------------------------------------
BLENDER_CONNECTION_REFUSED = "BLENDER_CONNECTION_REFUSED"
BLENDER_TIMEOUT = "BLENDER_TIMEOUT"
BLENDER_PROTOCOL_ERROR = "BLENDER_PROTOCOL_ERROR"
BLENDER_SCRIPT_ERROR = "BLENDER_SCRIPT_ERROR"
BLENDER_UNEXPECTED_RESPONSE = "BLENDER_UNEXPECTED_RESPONSE"
CONFIG_INVALID = "CONFIG_INVALID"
INTERNAL_ERROR = "INTERNAL_ERROR"
# MVP-02
PARAM_INVALID = "PARAM_INVALID"
NO_BASELINE = "NO_BASELINE"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
PREVIEW_FAILED = "PREVIEW_FAILED"

# 错误码 -> (HTTP 状态码, 是否可重试, 面向用户的下一步建议)
_ERROR_META: dict[str, tuple[int, bool, str]] = {
    BLENDER_CONNECTION_REFUSED: (
        503,
        True,
        "请确认 Blender 已打开、已在插件里启动 MCP 服务，且端口正确；然后点「重新连接」。",
    ),
    BLENDER_TIMEOUT: (
        504,
        True,
        "Blender 未在超时时间内响应。若 Blender 正忙（例如正在渲染），稍候再点「重新连接」。",
    ),
    BLENDER_PROTOCOL_ERROR: (
        502,
        False,
        "与 Blender MCP 的通信格式异常。请确认 9876 端口是本工具的 Blender MCP，而非其他程序。",
    ),
    BLENDER_SCRIPT_ERROR: (
        502,
        True,
        "只读探针在 Blender 内执行失败。请在 Blender 里确认当前场景状态后重试。",
    ),
    BLENDER_UNEXPECTED_RESPONSE: (
        502,
        True,
        "Blender 返回了无法识别的结果。请确认 MCP 插件版本后重试。",
    ),
    CONFIG_INVALID: (
        500,
        False,
        "配置不合法，请检查 config.local.json 中的 host/port/超时设置。",
    ),
    INTERNAL_ERROR: (
        500,
        True,
        "本地控制服务内部错误，请查看服务日志后重试。",
    ),
    PARAM_INVALID: (
        400,
        False,
        "提交的参数不在白名单内或取值越界。请刷新页面后重试。",
    ),
    NO_BASELINE: (
        409,
        True,
        "尚未建立内存基线。请先在 Blender 连接正常时建立基线，再调整参数。",
    ),
    JOB_NOT_FOUND: (
        404,
        False,
        "任务 ID 不存在或已过期（旧任务会在新预览提交后作废）。",
    ),
    PREVIEW_FAILED: (
        502,
        True,
        "预览渲染失败。请确认 Blender 场景中存在相机与合成器节点组，然后重试。",
    ),
}


class ToonTunerError(Exception):
    """所有可预期失败的基类。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code if code in _ERROR_META else INTERNAL_ERROR
        self.message = message
        self._http_status, meta_retryable, self.hint = _ERROR_META[self.code]
        self.retryable = meta_retryable if retryable is None else retryable
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return self._http_status

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "hint": self.hint,
        }
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


class BlenderConnectionRefused(ToonTunerError):
    def __init__(self, target: str) -> None:
        super().__init__(
            BLENDER_CONNECTION_REFUSED,
            f"无法连接 Blender MCP {target}",
            details={"target": target},
        )


class BlenderTimeout(ToonTunerError):
    def __init__(self, target: str, phase: str, seconds: float) -> None:
        super().__init__(
            BLENDER_TIMEOUT,
            f"连接 Blender MCP {target} 超时（{phase}，{seconds:g}s）",
            details={"target": target, "phase": phase, "seconds": seconds},
        )


class BlenderProtocolError(ToonTunerError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(BLENDER_PROTOCOL_ERROR, message, details=details)


class BlenderScriptError(ToonTunerError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(BLENDER_SCRIPT_ERROR, message, details=details)


class BlenderUnexpectedResponse(ToonTunerError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(BLENDER_UNEXPECTED_RESPONSE, message, details=details)


# 供 /api/blender/status 使用的状态枚举语义
STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"
STATUS_TIMEOUT = "timeout"
STATUS_PROTOCOL_ERROR = "protocol_error"
STATUS_BLENDER_ERROR = "blender_error"

_CODE_TO_STATUS = {
    BLENDER_CONNECTION_REFUSED: STATUS_DISCONNECTED,
    BLENDER_TIMEOUT: STATUS_TIMEOUT,
    BLENDER_PROTOCOL_ERROR: STATUS_PROTOCOL_ERROR,
    BLENDER_SCRIPT_ERROR: STATUS_BLENDER_ERROR,
    BLENDER_UNEXPECTED_RESPONSE: STATUS_BLENDER_ERROR,
}


def status_for_error(code: str) -> str:
    return _CODE_TO_STATUS.get(code, STATUS_PROTOCOL_ERROR)
