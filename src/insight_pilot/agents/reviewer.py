"""
agents/reviewer.py —— Reviewer 节点（人机协同的核心）

【职责】
    检查 Reporter 产出的报告是否敏感。
    敏感 → 触发 interrupt() 让人审批
    安全 → 直接通过

【这不是 LLM Agent —— 是逻辑节点】
    Reviewer 不调 LLM 综合（敏感性分类的小 LLM 调用在 tools/sensitivity.py 里）。
    它是个"判定 + 路由"节点，主要逻辑就这几行。

【interrupt() 用法关键】
    1. interrupt() 的参数会被 LangGraph 包装成 Interrupt 对象，
       通过 graph 返回值的 __interrupt__ 字段暴露给调用方
    2. 调用方（CLI）拿到这个值，渲染、收用户输入
    3. 调用方用 graph.invoke(Command(resume=user_input), config={...thread_id...})
       恢复，interrupt() 返回值就是 user_input
    4. 整个机制依赖 Checkpointer（SqliteSaver）持久化 state

【返回值约定】
    本节点返回的 dict 会被合并进 State：
      - approved: bool         审批结果
      - human_feedback: str    用户反馈文本
      - status: str            "complete" / "failed"
      - needs_human_review: bool  这次有没有触发审批
"""

from __future__ import annotations

from langgraph.types import interrupt

from insight_pilot.state import AgentState
from insight_pilot.tools.exemplar_store import save_exemplar
from insight_pilot.tools.sensitivity import classify_sensitivity


def _save_exemplar_from_state(state: AgentState, approved_by_reviewer: bool) -> None:
    """
    从当前 state 提取信息并存 exemplar。
    在 reviewer 决定通过时调用。

    【为什么独立成函数】
      reviewer_node 有两条"通过"路径（安全自动通过 + 敏感经审批通过），
      避免重复代码。
    """
    user_query = state.get("user_query", "")
    plan = state.get("execution_plan", []) or []
    query_results = state.get("query_results", []) or []

    # 抽出所有成功 SQL
    sqls = [qr.sql for qr in query_results if getattr(qr, "success", False)]
    if not sqls:
        # 没有成功 SQL，不值得存
        return

    save_exemplar(
        user_question=user_query,
        execution_plan=plan,        # ExecutionStep 对象列表
        sqls=sqls,
        approved_by_reviewer=approved_by_reviewer,
    )


# ============================================================================
# Reviewer 节点函数
#
# 【为什么不是 build_reviewer() 工厂？】
#   它没有需要构造时绑定的资源（不像 Analysis Agent 要捕获 query_results）。
#   直接写成节点函数，让 graph.py 直接 import。
# ============================================================================
def reviewer_node(state: AgentState) -> dict:
    """
    审批节点：判断报告是否敏感，敏感则触发 interrupt() 等人审批。
    """
    report = state.get("report_markdown", "")

    # 没有报告 → 无可审批，直接通过（防御性）
    if not report.strip():
        return {
            "needs_human_review": False,
            "human_feedback": None,
            "status": "complete",
        }

    # ---- 敏感性分类 ----
    sensitivity = classify_sensitivity(report)

    # ---- 安全：直接通过，不打扰人 ----
    if not sensitivity.is_sensitive:
        # 自动通过的也存 exemplar（approved_by_reviewer=False，标记是自动判定）
        _save_exemplar_from_state(state, approved_by_reviewer=False)
        return {
            "needs_human_review": False,
            "human_feedback": None,
            "status": "complete",
        }

    # ---- 敏感：触发 interrupt() ----
    # 给调用方传过去的字典里包含：
    #   - report：报告内容（让用户看）
    #   - reason：为什么判定为敏感（让用户理解）
    #   - options：可选的回答（教用户怎么回）
    #
    # 这一行执行的瞬间：
    #   1. LangGraph 序列化 state 写到 Checkpointer
    #   2. 把这个 dict 包成 Interrupt 对象
    #   3. graph.invoke 返回，把 Interrupt 暴露在 __interrupt__ 字段
    #
    # 用户用 Command(resume="approve") 恢复时：
    #   1. LangGraph 加载 state
    #   2. 跳到这一行
    #   3. interrupt() 返回 "approve"
    #   4. 函数继续执行下面的逻辑
    decision = interrupt({
        "report": report,
        "reason": sensitivity.reason,
        "options": [
            "approve  - 通过，按当前报告输出",
            "reject   - 驳回，不输出报告",
        ],
        "matched_layer": sensitivity.matched_layer,
    })

    # ---- 处理用户决定 ----
    # decision 是用户通过 Command(resume=...) 传进来的字符串
    decision_str = str(decision).strip().lower()

    if decision_str.startswith("a") or decision_str == "approve":
        # 经人工审批通过 → 高质量 exemplar
        _save_exemplar_from_state(state, approved_by_reviewer=True)
        return {
            "needs_human_review": True,
            "human_feedback": "approved",
            "status": "complete",
        }

    elif decision_str.startswith("r") or decision_str == "reject":
        return {
            "needs_human_review": True,
            "human_feedback": "rejected",
            "status": "failed",
            "error": "用户驳回了报告",
        }

    else:
        # 未识别的输入 —— 保守认为驳回（"我没说通过 = 别通过"）
        return {
            "needs_human_review": True,
            "human_feedback": f"unknown decision: {decision_str}",
            "status": "failed",
            "error": f"未识别的决定：{decision_str}（应为 approve / reject）",
        }


__all__ = ["reviewer_node"]
