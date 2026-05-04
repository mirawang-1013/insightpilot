"""
agents/reporter.py —— Reporter Agent 工厂

【职责】
    把完整的 State 综合成一篇 Markdown 报告。
    无工具调用、单次 LLM 调用、不循环。

【为什么不用 with_structured_output？】
    Markdown 是给人读的，不需要"机器可解析"约束。
    自由文本 LLM 写得更自然。
    我们靠 prompt 强制 Markdown 结构（标题层级、引用图表）。

【数据组装策略】
    State 里的字段散乱，需要先组装成 LLM 友好的"上下文包"再喂进去。
    见 _format_state_for_reporter 函数。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from insight_pilot.config import get_settings
from insight_pilot.prompts.reporter import REPORTER_SYSTEM_PROMPT
from insight_pilot.state import AgentState


# ============================================================================
# 辅助：把 State 组装成 LLM 输入
#
# 【为什么要这层组装？】
#   State 是工程数据结构（dataclass / TypedDict），LLM 不擅长读它们的 repr。
#   这里把字段渲染成"对 LLM 友好的 Markdown 上下文"。
# ============================================================================
def _format_state_for_reporter(state: AgentState) -> str:
    """
    把 AgentState 的关键内容渲染成 Markdown 上下文，供 Reporter LLM 读。
    """
    parts: list[str] = []

    # ---- 1. 用户问题 ----
    parts.append(f"# 用户问题\n\n{state['user_query']}\n")

    # ---- 2. 执行计划 ----
    plan = state.get("execution_plan", [])
    if plan:
        parts.append("# 执行计划\n")
        for s in plan:
            parts.append(f"- **Step {s.step_id}** [{s.step_type}]: {s.description}")
        parts.append("")

    # ---- 3. SQL 查询结果（每条单独成段）----
    query_results = state.get("query_results", [])
    if query_results:
        parts.append("# SQL 查询结果\n")
        for i, qr in enumerate(query_results, start=1):
            parts.append(f"## Query {i}\n")
            parts.append("**SQL：**")
            parts.append(f"```sql\n{qr.sql}\n```\n")
            parts.append(f"**返回 {qr.row_count} 行，列：{', '.join(qr.columns)}**\n")
            # 数据样例：前 10 行
            if qr.rows:
                parts.append("**数据样例（前 10 行）：**")
                parts.append("```")
                for row in qr.rows[:10]:
                    parts.append(str(row))
                if len(qr.rows) > 10:
                    parts.append(f"... 还有 {len(qr.rows) - 10} 行未显示")
                parts.append("```\n")

    # ---- 4. Python 分析结果（每条单独成段）----
    analysis_results = state.get("analysis_results", [])
    if analysis_results:
        parts.append("# Python 分析输出\n")
        for ar in analysis_results:
            if not ar.success:
                continue  # 跳过失败的，不让 Reporter 引用
            parts.append(f"## Analysis Step {ar.step_id}\n")
            if ar.stdout:
                parts.append("**stdout（关键数字在这里）：**")
                # stdout 可能很长，截到前 1500 字符
                preview = ar.stdout[:1500]
                if len(ar.stdout) > 1500:
                    preview += f"\n... (还有 {len(ar.stdout) - 1500} 字符)"
                parts.append(f"```\n{preview}\n```\n")
            if ar.chart_paths:
                parts.append(f"**生成图表：** {ar.chart_paths}\n")

    # ---- 5. 所有图表路径（汇总）----
    chart_paths = state.get("chart_paths", [])
    if chart_paths:
        parts.append("# 所有图表路径\n")
        for cp in chart_paths:
            parts.append(f"- `{cp}`")
        parts.append("")

    # ---- 6. 业务上下文（如果检索到）----
    biz_ctx = state.get("business_context", "")
    if biz_ctx.strip():
        parts.append("# 检索到的业务知识（参考用，不要直接引用到报告里）\n")
        parts.append(biz_ctx[:3000])  # 截断防超长
        parts.append("")

    return "\n".join(parts)


# ============================================================================
# 工厂函数：build_reporter
# ============================================================================
def build_reporter():
    """
    构造 Reporter。

    Returns:
        report(state: AgentState) -> str 的闭包函数。
        调用即返回 Markdown 字符串。
    """
    settings = get_settings()

    # ---- LLM ----
    # temperature 略高（0.3）让报告语言更自然
    # 但不能太高，否则瞎编数字
    llm = ChatOpenAI(
        model=settings.openai_model,
        temperature=0.3,
        api_key=settings.openai_api_key,
    )

    def report(state: AgentState) -> str:
        """
        把 State 综合成 Markdown 报告。

        Args:
            state: 完整的 AgentState（含执行结果）

        Returns:
            Markdown 字符串
        """
        # 组装上下文
        context = _format_state_for_reporter(state)

        # 调 LLM
        messages = [
            SystemMessage(content=REPORTER_SYSTEM_PROMPT),
            HumanMessage(content=context),
        ]
        response = llm.invoke(messages)

        # response.content 是字符串（标准 ChatOpenAI 响应）
        content = str(response.content).strip()

        # LLM 偶尔会把整个 Markdown 用 ```markdown ... ``` 代码块包起来
        # 这是 ChatGPT 的常见习惯，要剥掉，否则渲染时会显示成代码块
        if content.startswith("```markdown"):
            content = content[len("```markdown"):].lstrip("\n")
        elif content.startswith("```"):
            content = content[3:].lstrip("\n")
        if content.endswith("```"):
            content = content[:-3].rstrip()

        return content

    return report


# ============================================================================
# 开发自检
# ============================================================================
if __name__ == "__main__":
    from insight_pilot.state import AnalysisResult, ExecutionStep, QueryResult

    # 模拟一个完整 State
    fake_state: AgentState = {
        "user_query": "2017 年月度营收趋势是什么？",
        "execution_plan": [
            ExecutionStep(
                step_id=1, step_type="query",
                description="取 2017 年每月的总营收（按月 GROUP BY）",
            ),
            ExecutionStep(
                step_id=2, step_type="analysis",
                description="用 query_results[0] 画营收折线图",
            ),
        ],
        "current_step_index": 2,
        "business_context": "",
        "explored_schemas": [],
        "query_results": [
            QueryResult(
                sql="SELECT month, SUM(payment_total) FROM orders_full GROUP BY month",
                success=True,
                columns=["month", "revenue"],
                rows=[
                    {"month": "2017-01", "revenue": 130510},
                    {"month": "2017-06", "revenue": 496068},
                    {"month": "2017-11", "revenue": 1162150},
                    {"month": "2017-12", "revenue": 850702},
                ],
                row_count=4,
                truncated=False,
                execution_ms=36,
            ),
        ],
        "analysis_results": [
            AnalysisResult(
                step_id=2, success=True,
                code="plt.plot(...)",
                stdout="形状: (12, 2)\n2017 年总营收: 7,001,140 BRL\n11 月最高，黑色星期五效应",
                chart_paths=["outputs/chart_2_revenue_trend.png"],
                execution_ms=1247,
            ),
        ],
        "chart_paths": ["outputs/chart_2_revenue_trend.png"],
        "report_markdown": "",
        "messages": [],
        "iteration_count": 0,
        "max_iterations": 20,
        "needs_human_review": False,
        "human_feedback": None,
        "status": "reporting",
        "error": None,
    }

    print("构建 Reporter...")
    reporter = build_reporter()
    print("生成报告...\n")
    report = reporter(fake_state)
    print(report)
