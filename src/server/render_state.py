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
**正式渲染阶段（preparing / rendering / verifying）一律不可取消**（规格 §7.2~§7.3：
第一版不冒险扩大杀进程范围；避免在 Blender 写入正式产物时损坏工程）。
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
    # 服务重启后从摘要索引恢复出来的历史任务专用终态：上次进程被杀，
    # 任务没走到任何终态。它是终态（不可再流转、不可交付），但**不是 run_contract
    # 的正式终态** —— run_contract 只有四态，绝不把中断伪装成失败或成功。
    "interrupted",
)

# ---- 终止 / 可取消 / 可重试 --------------------------------------------
TERMINAL_STATES = {"success", "failed", "rejected", "diagnostic", "cancelled", "interrupted"}
#: 可交付白名单：**只有 success**。rejected / diagnostic / failed / cancelled 一律不可交付。
SHIPPABLE_STATES = {"success"}
CANCELABLE_STATES = {"queued", "analyzing_materials", "waiting_confirmation"}
#: 中断可重试：上次进程被杀，产物目录可能只写了一半，重跑是最自然的出路。
RETRYABLE_STATES = {"failed", "rejected", "diagnostic", "interrupted"}

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
    "interrupted": (5, "已中断"),
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
    # preparing / rendering / verifying：**不含 cancelled**。
    # `cancel()` 已由 `can_cancel()` 拦住，这里再从迁移表上删一次是纵深防御 ——
    # 即便将来有人绕过 `can_cancel` 直接 `set_state(..., "cancelled")`，
    # 也不会把正在写正式产物的 Blender 置成「已取消」（那会让用户误以为中断成功）。
    "preparing": {"rendering", "failed", "rejected", "diagnostic"},
    "rendering": {"verifying", "failed", "rejected", "diagnostic"},
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


def is_shippable(state: str) -> bool:
    """该状态是否可交付。**只有 success**（规格 §3.3 硬约束）。"""
    return state in SHIPPABLE_STATES


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
