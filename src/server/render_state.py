"""渲染任务状态机（HTTP 编排层使用，规格 §8）。

状态流转（成功路径）：
    queued -> preflight -> analyzing_materials -> waiting_confirmation
           -> preparing -> rendering -> verifying -> success

终止态（不再流转）：
    success      成功（按契约：必须确有产物文件才算成功）
    failed       渲染/预检失败
    rejected     用户/确认页拒绝
    diagnostic   仅产出诊断信息
    cancelled    用户取消

取消只允许在 queued / analyzing_materials / waiting_confirmation。
重试只允许在 failed / rejected / diagnostic。
"""

from __future__ import annotations

# ---- 全部状态 ----------------------------------------------------------
ALL_STATES = (
    "queued",
    "preflight",
    "analyzing_materials",
    "waiting_confirmation",
    "preparing",
    "rendering",
    "verifying",
    "success",
    "failed",
    "rejected",
    "diagnostic",
    "cancelled",
)

# ---- 终止 / 可取消 / 可重试 --------------------------------------------
TERMINAL_STATES = {"success", "failed", "rejected", "diagnostic", "cancelled"}
CANCELABLE_STATES = {"queued", "analyzing_materials", "waiting_confirmation"}
RETRYABLE_STATES = {"failed", "rejected", "diagnostic"}

# 规格 §5 的 6 步：选择模型→材质分析→材质确认→渲染设置→执行渲染→查看结果
STEP_COUNT = 6

_STEP_BY_STATE: dict[str, tuple[int, str]] = {
    "queued": (0, "选择模型"),
    "preflight": (1, "预检"),
    "analyzing_materials": (1, "材质分析"),
    "waiting_confirmation": (2, "材质确认"),
    "preparing": (3, "渲染设置"),
    "rendering": (4, "执行渲染"),
    "verifying": (5, "查看结果"),
    "success": (5, "完成"),
    "failed": (5, "失败"),
    "rejected": (5, "被拒"),
    "diagnostic": (5, "诊断"),
    "cancelled": (5, "已取消"),
}

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"preflight", "cancelled"},
    "preflight": {"analyzing_materials", "failed", "rejected", "diagnostic", "cancelled"},
    "analyzing_materials": {
        "waiting_confirmation",
        "preparing",
        "failed",
        "rejected",
        "diagnostic",
        "cancelled",
    },
    "waiting_confirmation": {"preparing", "failed", "rejected", "diagnostic", "cancelled"},
    "preparing": {"rendering", "failed", "rejected", "diagnostic", "cancelled"},
    "rendering": {"verifying", "failed", "rejected", "diagnostic", "cancelled"},
    "verifying": {"success", "failed", "rejected", "diagnostic"},
    "success": set(),
    "failed": set(),
    "rejected": set(),
    "diagnostic": set(),
    "cancelled": set(),
}


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def is_success(state: str) -> bool:
    return state == "success"


def can_cancel(state: str) -> bool:
    return state in CANCELABLE_STATES


def is_retryable(state: str) -> bool:
    return state in RETRYABLE_STATES


def stage_info(state: str) -> tuple[int, str]:
    """返回 (当前步索引 0-based, 该步标签)。"""
    return _STEP_BY_STATE.get(state, (0, state))


def can_transition(old: str, new: str) -> bool:
    if old == new:
        return True
    return new in _ALLOWED_TRANSITIONS.get(old, set())
