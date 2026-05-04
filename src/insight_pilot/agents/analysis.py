"""
agents/analysis.py —— Analysis Agent 工厂

【职责】
    构造一个能"读 query_results → 写 Python → 跑沙盒 → 拿结果"的 ReAct Agent。

【关键差异 vs Query Agent】
    Query Agent 是静态的：tools 列表固定，构造一次能复用。
    Analysis Agent 是动态的：tools 里 run_python 必须知道当前的 query_results。

    所以 build_analysis_agent 接受 query_results 参数，每次构建新 Agent。
    （别担心，构建一个 Agent 在 100ms 以内，开销可忽略。）
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.graph.state import CompiledStateGraph

from insight_pilot.config import get_settings
from insight_pilot.prompts.analysis import ANALYSIS_AGENT_SYSTEM_PROMPT
from insight_pilot.tools.lang_tools import make_run_python_tool


# ============================================================================
# 工厂函数：build_analysis_agent
#
# 【为什么传 query_results 而不是整个 State？】
#   Analysis Agent 只关心数据，不需要看 messages / status / 等其他 State 字段。
#   把工厂参数收紧到必需的部分，未来重构时影响范围小。
# ============================================================================
def build_analysis_agent(
    query_results: list[dict],
    captures: list | None = None,
) -> CompiledStateGraph:
    """
    构造一个 Analysis Agent。

    Args:
        query_results: 上游 query 步骤产出的 SQL 结果列表（QueryResult.to_dict() 形式）。
                      会被注入沙盒，作为 LLM 写 Python 代码时的可用数据。
        captures: 可选的共享 list。每次 run_python 工具被调用时，产出的
                 AnalysisResult 会被 append 进去。graph 层用这个机制
                 在源头捕获结果，避免事后重跑沙盒。

    Returns:
        CompiledStateGraph，可以 .invoke() 或 .stream() 跑。
    """
    settings = get_settings()

    # ---- LLM ----
    # temperature=0：写代码要确定性，不要创意（避免每次跑出不同的图）
    llm = ChatOpenAI(
        model=settings.openai_model,
        temperature=0,
        api_key=settings.openai_api_key,
    )

    # ---- 动态构建 run_python 工具 ----
    # 工厂函数把 query_results 闭包捕获到工具内部
    # captures 也通过闭包传进去 —— 工具调用时直接 append AnalysisResult
    run_python = make_run_python_tool(query_results, captures=captures)

    # ---- 组装 Agent ----
    # LangChain V1.0 把 create_react_agent 改名为 create_agent 并移到 langchain.agents
    agent = create_agent(
        model=llm,
        tools=[run_python],
        system_prompt=ANALYSIS_AGENT_SYSTEM_PROMPT,
    )

    return agent


# ============================================================================
# 开发自检
#
# 用法：uv run python -m insight_pilot.agents.analysis
# 会模拟一个 query_results，让 Analysis Agent 跑一次画图任务。
# ============================================================================
if __name__ == "__main__":
    from langchain_core.messages import HumanMessage

    # 模拟"上一步 query 取了 2017 年月度营收"
    fake_query_results = [
        {
            "sql": "SELECT month, revenue FROM ...",
            "columns": ["month", "revenue"],
            "rows": [
                {"month": "2017-01", "revenue": 130510},
                {"month": "2017-02", "revenue": 275562},
                {"month": "2017-03", "revenue": 418978},
                {"month": "2017-04", "revenue": 397419},
                {"month": "2017-05", "revenue": 576106},
                {"month": "2017-06", "revenue": 496068},
                {"month": "2017-07", "revenue": 575042},
                {"month": "2017-08", "revenue": 652419},
                {"month": "2017-09", "revenue": 708621},
                {"month": "2017-10", "revenue": 757563},
                {"month": "2017-11", "revenue": 1162150},
                {"month": "2017-12", "revenue": 850702},
            ],
            "row_count": 12,
            "truncated": False,
            "success": True,
            "error": None,
            "execution_ms": 36,
        }
    ]

    print("=" * 72)
    print("构建 Analysis Agent...")
    agent = build_analysis_agent(fake_query_results)

    task = (
        "用 query_results[0] 的数据画 2017 年月度营收折线图。"
        "x 轴月份 y 轴营收。step_id 设为 88。"
        "图保存到 outputs/。最后打印一句总营收摘要。"
    )

    print(f"任务：{task}\n")
    print("调 Agent...\n")

    # 流式跑
    for event in agent.stream(
        {"messages": [HumanMessage(content=task)]},
        stream_mode="updates",
    ):
        for node_name, node_output in event.items():
            print(f"--- Node: {node_name} ---")
            for msg in node_output.get("messages", []):
                content = getattr(msg, "content", "")
                tool_calls = getattr(msg, "tool_calls", None)

                if tool_calls:
                    for tc in tool_calls:
                        print(f"[调用工具] {tc['name']}")
                        # 显示代码的前几行
                        code = tc["args"].get("code", "")
                        for line in code.split("\n")[:10]:
                            print(f"   | {line}")
                        if len(code.split("\n")) > 10:
                            print(f"   | ... ({len(code.split(chr(10)))} 行总共)")
                elif content:
                    preview = str(content)[:300]
                    print(preview)
                    if len(str(content)) > 300:
                        print(f"... (还有 {len(str(content)) - 300} 字符)")
            print()
