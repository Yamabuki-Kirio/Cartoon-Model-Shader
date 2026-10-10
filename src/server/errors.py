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
# 依赖枚举（view_transform → look）
INVALID_DEPENDENT_ENUM = "INVALID_DEPENDENT_ENUM"
# 取景（framing）
FRAMING_STALE = "FRAMING_STALE"
FRAMING_UNAVAILABLE = "FRAMING_UNAVAILABLE"
#: 预览必须输出 PNG，但工程的输出设置无法被安全切换过来（例如影片输出 FFMPEG）
PREVIEW_OUTPUT_UNAVAILABLE = "PREVIEW_OUTPUT_UNAVAILABLE"
# 会话令牌（所有写接口）
SESSION_TOKEN_INVALID = "SESSION_TOKEN_INVALID"
# 保存 / 应用到工程
SAVE_TARGET_INVALID = "SAVE_TARGET_INVALID"
SAVE_CONFIRM_REQUIRED = "SAVE_CONFIRM_REQUIRED"
PROJECT_NOT_SAVED = "PROJECT_NOT_SAVED"
BACKUP_FAILED = "BACKUP_FAILED"
COMMIT_TOKEN_INVALID = "COMMIT_TOKEN_INVALID"
COMMIT_TOKEN_EXPIRED = "COMMIT_TOKEN_EXPIRED"
COMMIT_TOKEN_USED = "COMMIT_TOKEN_USED"
COMMIT_TOKEN_MISMATCH = "COMMIT_TOKEN_MISMATCH"
BASELINE_STALE = "BASELINE_STALE"
APPLY_VERIFY_FAILED = "APPLY_VERIFY_FAILED"
SAVE_FAILED = "SAVE_FAILED"
#: 失败后回滚基线失败。不单独作为顶层错误码抛出（顶层保留**原始失败原因**，
#: 否则「为什么失败」就被盖掉了）；它出现在 ``error.details.status.rollback.code``，
#: 用来把「场景可能仍不是提交前的状态」这件事明确报出来。
ROLLBACK_FAILED = "ROLLBACK_FAILED"

# MVP-03 预设
PRESET_NOT_FOUND = "PRESET_NOT_FOUND"
PRESET_INVALID = "PRESET_INVALID"
PRESET_SCHEMA_UNSUPPORTED = "PRESET_SCHEMA_UNSUPPORTED"
PRESET_NAME_CONFLICT = "PRESET_NAME_CONFLICT"
PRESET_STORAGE_ERROR = "PRESET_STORAGE_ERROR"
# v4 参数面（递归 schema / 结构化 binding / 三层身份）
SCHEMA_INVALID = "SCHEMA_INVALID"
INVALID_BINDING = "INVALID_BINDING"
STRUCTURE_CHANGED = "STRUCTURE_CHANGED"
IDENTITY_MISSING = "IDENTITY_MISSING"
UNSUPPORTED_PARAM = "UNSUPPORTED_PARAM"
NOT_EDITABLE = "NOT_EDITABLE"
ASSET_UNKNOWN = "ASSET_UNKNOWN"
PRESET_INCOMPATIBLE = "PRESET_INCOMPATIBLE"
FRONTEND_NOT_BUILT = "FRONTEND_NOT_BUILT"
#: v4 通用执行器在 Blender 侧写入失败（该次写入已被执行器自己回滚）。
SURFACE_APPLY_FAILED = "SURFACE_APPLY_FAILED"
#: `/next` 的静态资源不存在（含路径越界；两种情况刻意不区分）。
ASSET_NOT_FOUND = "ASSET_NOT_FOUND"

#: 这些错误码在响应里把 ``details`` **平铺**到 error 顶层，
#: 便于调用方直接读到 ``parameter`` / ``value`` / ``depends_on`` / ``allowed``。
_FLATTEN_DETAILS_CODES = frozenset({INVALID_DEPENDENT_ENUM})

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
    INVALID_DEPENDENT_ENUM: (
        400,
        False,
        "该取值在当前「视图变换」下不合法（视图变换决定了 Look 的可选档位）。"
        "请换一个 Look，或先把「视图变换」调回原来的取值。",
    ),
    FRAMING_STALE: (
        409,
        True,
        "建立基线之后，当前帧或相机被外部改动，预览构图会失真。请点「建立 / 刷新基线」重新锁定，再继续调参。",
    ),
    FRAMING_UNAVAILABLE: (
        409,
        True,
        "无法自动取景：请确认场景里有可见的角色网格（已排除刚体代理与描边壳）以及一台活动相机。",
    ),
    PREVIEW_OUTPUT_UNAVAILABLE: (
        409,
        False,
        "预览只能输出 PNG，但当前工程的输出设置切不过去（常见原因：工程把渲染输出设为影片格式，"
        "如 FFMPEG）。预览已中止，工程未被改动。请把「输出属性 → 输出」改为图片格式后重试。",
    ),
    PRESET_NOT_FOUND: (
        404,
        False,
        "预设不存在或已被删除。请刷新预设列表后重试。",
    ),
    PRESET_INVALID: (
        400,
        False,
        "预设内容不合法：可能含未知参数、越界取值，或包含禁止保存的本机路径/凭据。"
        "请检查该预设后重试。",
    ),
    PRESET_SCHEMA_UNSUPPORTED: (
        400,
        False,
        "预设的 schema 版本不被支持。为避免静默改变取值，这里不会自动迁移；"
        "请用对应版本的工具打开后另存为新预设。",
    ),
    PRESET_NAME_CONFLICT: (
        409,
        False,
        "已存在同名预设（名称不区分大小写）。请换一个名字，或先重命名原有预设。",
    ),
    PRESET_STORAGE_ERROR: (
        500,
        True,
        "预设目录读写失败。请确认本地磁盘可写、目录未被其它程序占用，然后重试。",
    ),
    SESSION_TOKEN_INVALID: (
        401,
        False,
        "缺少或错误的本机会话令牌。请刷新页面重新取得令牌后重试；写接口一律拒绝无令牌请求。",
    ),
    SAVE_TARGET_INVALID: (
        400,
        False,
        "保存目标非法：必须是绝对路径且以 .blend 结尾；覆盖模式下目标由 Blender 当前工程决定，不接受客户端指定。",
    ),
    SAVE_CONFIRM_REQUIRED: (
        409,
        True,
        "覆盖已有文件需要二次确认。请核对绝对路径与预计备份路径后，再以确认标记重新提交。",
    ),
    PROJECT_NOT_SAVED: (
        409,
        True,
        "「覆盖当前工程」只能用于已保存过的工程。请先在 Blender 里保存一次，或改用「另存为」。",
    ),
    BACKUP_FAILED: (
        500,
        True,
        "备份失败，已拒绝覆盖。请确认工程所在目录可写、磁盘空间充足后重试。",
    ),
    COMMIT_TOKEN_INVALID: (
        409,
        False,
        "确认令牌无效（不存在或格式不对）。请重新点击「应用到工程」走一遍准备流程。",
    ),
    COMMIT_TOKEN_EXPIRED: (
        409,
        True,
        "确认令牌已过期（有效期很短）。请重新准备一次并尽快确认。",
    ),
    COMMIT_TOKEN_USED: (
        409,
        False,
        "确认令牌已被使用过；令牌是一次性的，不能复用。请重新准备。",
    ),
    COMMIT_TOKEN_MISMATCH: (
        409,
        False,
        "确认令牌与本次提交不匹配（基线 / 草稿 / 保存模式 / 目标路径必须与准备阶段完全一致）。",
    ),
    BASELINE_STALE: (
        409,
        True,
        "基线已失效（例如工程被重新保存或基线被刷新）。请先重新建立基线，再重新准备保存。",
    ),
    APPLY_VERIFY_FAILED: (
        502,
        True,
        "草稿写入后回读不一致，因此**没有保存**，草稿保留在界面上。请检查 Blender 场景状态后重试。",
    ),
    SAVE_FAILED: (
        502,
        True,
        "Blender 保存工程失败，草稿保留在界面上。请确认目标目录可写、工程未被其他程序占用后重试。",
    ),
    ROLLBACK_FAILED: (
        500,
        True,
        "失败后未能把 Blender 恢复到提交前的状态。请到 Blender 里人工确认曝光/辉光取值，"
        "必要时点「恢复基线」；磁盘上的工程文件未被写入。",
    ),
    SCHEMA_INVALID: (
        500,
        False,
        "参数 schema 自身不一致（重复 id 或声明了白名单外的字段）。这是服务端装配问题，"
        "不会写入 Blender；请更新到修正版本后重启服务。",
    ),
    INVALID_BINDING: (
        500,
        False,
        "参数绑定不合法（字段不在白名单内或 object_id 形态非法）。这是服务端装配问题，"
        "不会写入 Blender。",
    ),
    STRUCTURE_CHANGED: (
        409,
        True,
        "工程结构已变化（对象增删、节点拓扑、色标数量或材质槽变化）。草稿与保存确认令牌已作废，"
        "请重新建立基线后再继续。",
    ),
    IDENTITY_MISSING: (
        409,
        True,
        "受管对象被重命名或删除，无法再安全地写入（本工具不做猜测迁移）。"
        "请重新建立基线，并确认工程里的对象命名未被改动。",
    ),
    UNSUPPORTED_PARAM: (
        400,
        False,
        "该参数在当前工程中探测不到（supported=false），写入会被忽略或写错位置，因此已拒绝。"
        "请刷新基线，或确认工程里对应的节点/插座确实存在。",
    ),
    NOT_EDITABLE: (
        400,
        False,
        "该参数当前不可编辑（editable=false）。结构性改动需要先通过检查点恢复验收；"
        "详情见响应里的 readonly_reason。",
    ),
    ASSET_UNKNOWN: (
        400,
        False,
        "资源 id 不在当前基线的资源表内（或看起来像文件路径）。资源只能从服务端枚举出来的候选里选。",
    ),
    PRESET_INCOMPATIBLE: (
        409,
        False,
        "预设与当前工程不兼容，存在阻断项。请查看兼容性报告后决定是否强制应用（不会静默丢弃字段）。",
    ),
    FRONTEND_NOT_BUILT: (
        503,
        False,
        "前端尚未构建，无法提供页面。请在 web/ 目录执行 npm ci 与 npm run build 后重试"
        "（开发时可用 Vite dev server 并把 /api 代理到本服务）。",
    ),
    SURFACE_APPLY_FAILED: (
        502,
        True,
        "参数写入 Blender 失败（本次写入已被自动回滚，工程里的取值未改变）。"
        "请确认对应对象仍然存在后重试。",
    ),
    ASSET_NOT_FOUND: (
        404,
        False,
        "前端静态资源不存在。若页面白屏，请重新执行 npm run build 后再刷新。",
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
            # 依赖枚举类错误要求调用方能直接读到 parameter/value/depends_on/allowed，
            # 因此额外平铺到 error 顶层（需求 6 的稳定错误形状）。
            if self.code in _FLATTEN_DETAILS_CODES:
                for key, value in self.details.items():
                    error.setdefault(key, value)
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
