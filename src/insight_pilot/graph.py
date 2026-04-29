"""
graph.py —— LangGraph StateGraph 定义

【Phase 2 的最小图】
    START → query_node → END

    只有一个业务节点，但骨架建好了。
    Phase 3 起会陆续加节点（Planner / Analysis / Reporter / Reviewer）。

【query_node 的职责】
    1. 从 AgentState 里拿 user_query
    2. 包装成 HumanMessage 喂给 Query Agent
    3. 跑完 Agent 后，把 Agent 的 messages 合并回 State.messages
    4. 解析 Agent 跑的结果（QueryResult）累积到 State.query_results

【为什么要 query_node 这层包装，不直接让 Agent 当节点？】
    Query Agent 内部只关心 messages，但 AgentState 有更多字段
    （query_results / chart_paths / status 等）。
    query_node 负责"从 Agent 的 messages 里抽取结构化数据，写进 State"。

    这就是之前讲的"graph 层做状态管理，Agent 层做 LLM 循环"的分工。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from insight_pilot.agents.query import build_query_agent
from insight_pilot.state import AgentState, QueryResult
from insight_pilot.tools.duckdb_executor import execute_sql as _execute_sql_core


# ============================================================================
# 节点 1：query_node
#
# 【数据流】
#   输入：state["user_query"], state["messages"] (可能是空)
#   输出：{"messages": [...], "query_results": [...], "status": "..."}
#         LangGraph 会把这个 dict 按字段 reducer 合并进 state
# ============================================================================
def query_node(state: AgentState) -> dict:
    """
    Query Agent 节点：把 user_query 喂给 ReAct Agent，返回 SQL + 结果。
    """
    # 懒构造 Agent（每次调用都构造？后面可以加 lru_cache 优化）
    agent = build_query_agent()

    # ---- 构造 Agent 的输入 ----
    # Agent 接受 messages 列表，至少要有一条 HumanMessage
    user_msg = HumanMessage(content=state["user_query"])

    # ---- 调 Agent ----
    # 用 invoke 而不是 stream —— 这个节点是同步节点，拿到最终结果再返回
    # （流式体验让 main.py 控制，不是节点的责任）
    agent_result = agent.invoke({"messages": [user_msg]})

    # agent_result 是 {"messages": [...]}，包含了整个 ReAct 循环的所有消息：
    #   HumanMessage → AIMessage(tool_calls=[...]) → ToolMessage → AIMessage → ... → AIMessage(最终答案)
    all_messages = agent_result["messages"]

    # ---- 从 messages 里抽取本次跑的 SQL 结果 ----
    # 我们需要找所有 execute_sql 工具调用及其结果
    # 这里简化：只保留成功的最后一次 SQL 执行的结构化数据
    #
    # 【为什么不直接让 execute_sql 工具写 State？】
    #   @tool 装饰的函数没法访问 LangGraph State（它们是独立函数）。
    #   只能事后从 messages 里"反推"。Phase 3 有更优雅的做法（Command 对象）。
    extracted_results = _extract_query_results(all_messages)

    return {
        # 把 Agent 跑的所有消息追加到 State.messages
        # add_messages reducer 会处理去重和 tool_call 配对
        "messages": all_messages,
        # 把提取到的 QueryResult 追加到 State.query_results
        # operator.add reducer 会做列表拼接
        "query_results": extracted_results,
        # 状态更新为"已执行"
        "status": "executing",
        # 循环计数 +1（Phase 2 只有一个节点，不会循环，但预埋字段更新逻辑）
        "iteration_count": state.get("iteration_count", 0) + 1,
    }


def _extract_query_results(messages: list) -> list[QueryResult]:
    """
    从 Agent 的完整消息列表里，提取所有成功的 SQL 执行结果。

    【实现策略】
      遍历所有 ToolMessage，看哪些是 execute_sql 的返回。
      然后根据前一条 AIMessage 的 tool_call 拿到原始 SQL，
      重新调用核心 execute_sql 函数拿 QueryResult dataclass。

    【为什么要重新调一次 execute_sql？】
      因为 @tool 包装返回的是字符串（LLM 看的预览），
      我们需要原始 QueryResult 结构化数据塞进 State。
      重新调一次的代价：DuckDB 查询 <100ms，可接受。

      更优的做法会在 Phase 3 讨论（LangGraph 的 Command 机制）。
    """
    results: list[QueryResult] = []

    # 遍历消息对：AIMessage(tool_calls) → ToolMessage(content)
    # 对每个 execute_sql 的调用，重放一次拿 QueryResult
    for i, msg in enumerate(messages):
        # 找 AIMessage 里 tool_calls 包含 execute_sql 的
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tool_call in msg.tool_calls:
                if tool_call["name"] == "execute_sql":
                    sql = tool_call["args"].get("sql", "")
                    if sql:
                        # 重新执行拿结构化结果
                        # 这里不怕重复跑：只读查询，幂等
                        result = _execute_sql_core(sql)
                        if result.success:
                            results.append(result)

    return results


# ============================================================================
# 图构造函数：build_graph
# ============================================================================
def build_graph() -> CompiledStateGraph:
    """
    构造 Phase 2 的最小图：START → query → END

    Returns:
        编译好的 StateGraph，可以 .invoke() 或 .stream() 跑。
    """
    # ---- 声明 StateGraph ----
    # 参数 AgentState：告诉 LangGraph 用哪个 TypedDict 作为状态模式
    # reducer 通过 Annotated 在 AgentState 定义里绑定（如 operator.add）
    builder = StateGraph(AgentState)

    # ---- 加节点 ----
    # 第一个参数是节点名（字符串），第二个是节点函数
    # 节点函数签名固定：接收 state (dict)，返回 dict（字段更新）
    builder.add_node("query", query_node)

    # ---- 加边 ----
    # START → query：图启动后第一个跑 query 节点
    # query → END：query 完就结束
    # Phase 3 会在这里加条件边：query → (还有步骤?) → query / END
    builder.add_edge(START, "query")
    builder.add_edge("query", END)

    # ---- 编译 ----
    # compile() 把 builder 变成可执行的 CompiledStateGraph
    # 这一步会做静态校验：
    #   - 所有节点被 START 可达？
    #   - 所有节点有出边？
    #   - 状态字段的 reducer 定义合法？
    graph = builder.compile()

    return graph


# ============================================================================
# 开发自检
# 用法：uv run python -m insight_pilot.graph "2017 年月度营收趋势"
# ============================================================================
if __name__ == "__main__":
    import sys
    from insight_pilot.state import create_initial_state

    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "2017 年月度营收趋势"

    print(f"问题：{query}\n")
    print("=" * 72)
    print("构建图 & 运行...\n")

    graph = build_graph()
    initial_state = create_initial_state(query)
    final_state = graph.invoke(initial_state)

    print("=" * 72)
    print("最终 State 摘要：\n")
    print(f"  iteration_count: {final_state['iteration_count']}")
    print(f"  query_results 条数: {len(final_state['query_results'])}")

    if final_state["query_results"]:
        last_result = final_state["query_results"][-1]
        print(f"\n  最后一条 SQL：\n    {last_result.sql}")
        print(f"\n  结果预览：")
        print(last_result.to_llm_string(preview_rows=5))

    print(f"\n  messages 条数: {len(final_state['messages'])}")
