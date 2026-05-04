"""
graph.py —— LangGraph StateGraph 定义（Phase 3 升级版）

【Phase 3 拓扑】
    START
      ↓
    [planner]              拆 user_query 成 list[ExecutionStep]
      ↓
    [step_router]          看当前 step_index 决定下一步
      ├─→ [query_node]     如果是 query 步骤
      ├─→ [analysis_node]  如果是 analysis 步骤
      └─→ END              所有步骤跑完

    query_node / analysis_node 跑完后回到 step_router 判断下一步。

【条件边的核心】
    add_conditional_edges(from_node, decide_fn, {ret_value: target_node})

    decide_fn 是纯函数，读 state 返回字符串。
    LangGraph 用字符串到字典里找下一个节点。
    这是显式状态机的精髓 —— 路由是可测试的代码，不是 LLM 决定。

【循环计数保护】
    iteration_count 每次进 step_router 都 +1，超过 max_iterations 强制 END。
    防止某个 step 反复失败导致死循环。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from insight_pilot.agents.analysis import build_analysis_agent
from insight_pilot.agents.planner import build_planner
from insight_pilot.agents.query import build_query_agent
from insight_pilot.state import AgentState, AnalysisResult, QueryResult
from insight_pilot.tools.duckdb_executor import execute_sql as _execute_sql_core


# ============================================================================
# 节点 1：planner_node
#
# 一次 LLM 调用产出 list[ExecutionStep]。
# 没有循环、没有工具，最简单的节点。
# ============================================================================
def planner_node(state: AgentState) -> dict:
    """
    Planner 节点：拆解 user_query 为执行步骤序列。
    """
    planner = build_planner()
    steps = planner(state["user_query"])

    return {
        "execution_plan": steps,
        "current_step_index": 0,
        "status": "executing",
        "iteration_count": state.get("iteration_count", 0) + 1,
    }


# ============================================================================
# 节点 2：query_node
#
# 处理一个 query 步骤：用 Query Agent 跑 ReAct 循环，最后产出 QueryResult。
# 把 description 当作"子任务"喂给 Agent。
# ============================================================================
def query_node(state: AgentState) -> dict:
    """
    执行当前的 query 步骤。
    """
    plan = state["execution_plan"]
    idx = state["current_step_index"]
    current_step = plan[idx]

    # ---- 调 Query Agent ----
    agent = build_query_agent()

    # 把当前 step 的 description 当作子任务喂进去
    user_msg = HumanMessage(content=f"任务：{current_step.description}")
    agent_result = agent.invoke({"messages": [user_msg]})
    all_messages = agent_result["messages"]

    # ---- 从 Agent 跑过的工具调用里抽出 SQL 结果 ----
    extracted_results = _extract_query_results(all_messages)

    return {
        "messages": all_messages,
        "query_results": extracted_results,
        # current_step_index +1 推进到下一步
        "current_step_index": idx + 1,
        "iteration_count": state.get("iteration_count", 0) + 1,
    }


# ============================================================================
# 节点 3：analysis_node
#
# 处理一个 analysis 步骤：用 Analysis Agent 跑 Python 代码 + 画图。
# Analysis Agent 是动态构建的（要传 query_results）。
# ============================================================================
def analysis_node(state: AgentState) -> dict:
    """
    执行当前的 analysis 步骤。

    【为什么用 captures 而不是事后从 messages 重跑？】
      run_python 工具产生 PNG 文件作为副作用。
      事后重跑会"覆盖文件"而非"创建新文件"，导致 chart_paths 检测失效。
      所以在工具调用时直接捕获 AnalysisResult 到 captures 列表。
    """
    plan = state["execution_plan"]
    idx = state["current_step_index"]
    current_step = plan[idx]

    # ---- 把 State 里累积的 query_results 转 dict 列表 ----
    query_results_dicts = [qr.to_dict() for qr in state["query_results"]]

    # ---- 在源头捕获结果 ----
    captures: list[AnalysisResult] = []
    agent = build_analysis_agent(query_results_dicts, captures=captures)

    # 给 Agent 的任务描述里附上 step_id，让 LLM 知道用哪个 id 命名图表
    user_msg = HumanMessage(content=(
        f"任务：{current_step.description}\n"
        f"step_id：{current_step.step_id}（请用这个数字命名图表文件）"
    ))
    agent_result = agent.invoke({"messages": [user_msg]})
    all_messages = agent_result["messages"]

    # ---- 从 captures 直接拿结果，不需要重跑 ----
    successful_results = [r for r in captures if r.success]
    chart_paths: list[str] = []
    for r in successful_results:
        chart_paths.extend(r.chart_paths)

    return {
        "messages": all_messages,
        "analysis_results": successful_results,
        "chart_paths": chart_paths,
        "current_step_index": idx + 1,
        "iteration_count": state.get("iteration_count", 0) + 1,
    }


# ============================================================================
# 路由函数：decide_next_step
#
# 【这个函数不是节点 —— 是条件边的判断器】
#   add_conditional_edges 接受这个函数，根据返回值决定下一个节点。
#   纯函数：读 state，返回字符串，无副作用。
#
# 【返回值的含义】
#   "query"     → 跑 query_node
#   "analysis"  → 跑 analysis_node
#   "done"      → 跳到 END
# ============================================================================
def decide_next_step(state: AgentState) -> str:
    """
    根据当前 step_index 和 max_iterations 判断下一步去哪。
    """
    # ---- 安全闸：超过最大迭代次数强制结束 ----
    if state["iteration_count"] >= state["max_iterations"]:
        return "done"

    plan = state.get("execution_plan", [])
    idx = state.get("current_step_index", 0)

    # ---- 所有步骤跑完了 → END ----
    if idx >= len(plan):
        return "done"

    # ---- 看当前 step 的 type ----
    current_step = plan[idx]
    if current_step.step_type == "query":
        return "query"
    elif current_step.step_type == "analysis":
        return "analysis"
    else:
        # 不应该发生（Pydantic 已经校验过 step_type 是合法 Literal）
        # 但留个兜底，万一未来加新类型时旧代码不挂
        return "done"


# ============================================================================
# 辅助：从 Query Agent 的 messages 里抽 QueryResult
#
# 跟 Phase 2 版本一致 —— 重新执行成功的 SQL 拿结构化结果。
# ============================================================================
def _extract_query_results(messages: list) -> list[QueryResult]:
    """从 Agent 的完整消息列表里，提取所有成功的 SQL 执行结果。"""
    results: list[QueryResult] = []
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tool_call in msg.tool_calls:
                if tool_call["name"] == "execute_sql":
                    sql = tool_call["args"].get("sql", "")
                    if sql:
                        result = _execute_sql_core(sql)
                        if result.success:
                            results.append(result)
    return results


# ============================================================================
# 图构造：build_graph
# ============================================================================
def build_graph() -> CompiledStateGraph:
    """
    构造 Phase 3 的多节点图。

    拓扑：
      START → planner → step_router → (query | analysis) → step_router → ... → END
    """
    builder = StateGraph(AgentState)

    # ---- 加节点 ----
    builder.add_node("planner", planner_node)
    builder.add_node("query", query_node)
    builder.add_node("analysis", analysis_node)

    # ---- 加边 ----
    # 入口：START → planner
    builder.add_edge(START, "planner")

    # planner 跑完 → 进入条件路由判断第一步去哪
    # 注意：我们没把 step_router 写成节点，而是直接用 add_conditional_edges
    # 把"路由"作为 planner 的出边来表达。这样图里少一个节点，结构更紧凑。
    builder.add_conditional_edges(
        "planner",          # 从 planner 出发
        decide_next_step,   # 调用这个函数判断去哪
        {
            "query": "query",
            "analysis": "analysis",
            "done": END,
        },
    )

    # query 跑完 → 同样进路由（决定下一步是再 query 还是 analysis 还是 END）
    builder.add_conditional_edges(
        "query",
        decide_next_step,
        {
            "query": "query",
            "analysis": "analysis",
            "done": END,
        },
    )

    # analysis 跑完 → 同样进路由
    builder.add_conditional_edges(
        "analysis",
        decide_next_step,
        {
            "query": "query",
            "analysis": "analysis",
            "done": END,
        },
    )

    return builder.compile()


# ============================================================================
# 开发自检
# ============================================================================
if __name__ == "__main__":
    import sys
    from insight_pilot.state import create_initial_state

    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "分析各州的配送表现，延迟和评分有什么关系？"
    )

    print(f"问题：{query}\n")
    print("=" * 72)
    print("构建图...")

    graph = build_graph()

    print(f"节点：{list(graph.nodes)}")
    print()
    print("跑图...\n")

    initial_state = create_initial_state(query)

    for event in graph.stream(initial_state, stream_mode="updates"):
        for node_name, node_update in event.items():
            print(f"--- 节点完成: {node_name} ---")
            # 打印关键字段更新
            if "execution_plan" in node_update:
                print(f"  [Plan] {len(node_update['execution_plan'])} 步")
                for s in node_update["execution_plan"]:
                    print(f"    Step {s.step_id}: [{s.step_type}] {s.description[:80]}")
            if "query_results" in node_update:
                print(f"  [+QueryResult] {len(node_update['query_results'])} 条新结果")
            if "analysis_results" in node_update:
                print(f"  [+AnalysisResult] {len(node_update['analysis_results'])} 条新结果")
            if "chart_paths" in node_update:
                print(f"  [+Charts] {node_update['chart_paths']}")
            print()
