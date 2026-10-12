# -*- coding: utf-8 -*-
"""
运行终态契约（v3）
=====================================================================================
这是「什么算成品」的唯一判定点。主脚本、驱动、以及任何成品消费端都必须从这里取，
不要各自写字符串比较。

四个终态：

    SUCCESS      严格准入通过 + 完成渲染  →  唯一可以进入成品流程的终态
    REJECTED     在【材质重建与正式渲染之前】被准入检查拦下，不保存 .blend、不出成品 PNG
    DIAGNOSTIC   出了诊断图（含洋红棋盘诊断材质 + 图上"诊断预览"标识），**不是成品**
    FAILED       执行过程异常

设计要点：token 刻意【不使用 OK 前缀】。历史上不少启动器用"是否以 OK 开头"判断成败，
如果诊断结果写成 `OK diagnostic`，那些启动器会把诊断图当成正式成功。现在写 `DIAGNOSTIC`，
老判断会将其视为失败 —— 失败方向是安全的。
"""

OUTCOME_SUCCESS = "success"
OUTCOME_REJECTED = "rejected"
OUTCOME_DIAGNOSTIC = "diagnostic"
OUTCOME_FAILED = "failed"
OUTCOME_SET = (OUTCOME_SUCCESS, OUTCOME_REJECTED, OUTCOME_DIAGNOSTIC, OUTCOME_FAILED)

#: 允许进入成品流程的终态（白名单，只有一个）
SHIPPABLE_OUTCOMES = (OUTCOME_SUCCESS,)

#: 中文说明，用于日志与报告
OUTCOME_LABEL = {
    OUTCOME_SUCCESS: "成功（正式成品）",
    OUTCOME_REJECTED: "已拒绝（渲染前拦下，未出图）",
    OUTCOME_DIAGNOSTIC: "诊断图（不是成品）",
    OUTCOME_FAILED: "失败（执行异常）",
}


def normalize(outcome):
    o = str(outcome or "").strip().lower()
    return o if o in OUTCOME_SET else OUTCOME_FAILED


def is_shippable(outcome):
    """唯一准入判据：只有 success 可被成品流程接受。"""
    return normalize(outcome) in SHIPPABLE_OUTCOMES


def done_token(outcome):
    """--done 文件里写的大写终态 token。"""
    return normalize(outcome).upper()


def assert_shippable(outcome, context=""):
    """
    成品流程入口调用：非 SUCCESS 直接抛异常，防止诊断图/未出图/异常结果被当成品消费。
    """
    if not is_shippable(outcome):
        raise RuntimeError(
            "%s结果非 SUCCESS（实际：%s），不允许进入成品流程。%s"
            % (context + "：" if context else "", normalize(outcome),
               OUTCOME_LABEL.get(normalize(outcome), "")))
    return True


def result_payload(outcome, extra=None):
    """写 run_result.json 用的结构化载荷。"""
    o = normalize(outcome)
    payload = {
        "schema": "toon-run-result/1",
        "outcome": o,
        "done_token": done_token(o),
        "shippable": is_shippable(o),
        "label": OUTCOME_LABEL.get(o, ""),
    }
    if extra:
        payload.update(extra)
    return payload
