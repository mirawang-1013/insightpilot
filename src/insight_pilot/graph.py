"""
graph.py —— LangGraph StateGraph 定义（Phase 4 升级版）

【Phase 4 拓扑】
    START
      ↓
    [knowledge_retrieval]  RAG 检索业务术语，写 State.business_context
      ↓
    [planner]              拆 user_query 成 list[ExecutionStep]（看到 business_context）
      ↓
    [step_router]          看当前 step_index 决定下一步
      ├─→ [query_node]     如果是 query 步骤（注入 business_context 到 prompt）
      ├─→ [analysis_node]  如果是 analysis 步骤（注入 business_context 到 prompt）
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

【RAG 注入策略】
    knowledge_retrieval_node 一次检索，写进 State.business_context。
    planner_node 把它作为额外 SystemMessage 喂给 Planner LLM。
    query_node / analysis_node 把它作为 prompt 前缀喂给对应 Agent。
    整图只检索一次 —— 多次检索没意义（business_context 是 query 级别的元信息）。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from insight_pilot.agents.analysis import build_analysis_agent
from insight_pilot.agents.planner import build_planner
from insight_pilot.agents.query import build_query_agent
from insight_pilot.agents.reporter import build_reporter
from insight_pilot.agents.reviewer import reviewer_node
from insight_pilot.state import AgentState, AnalysisResult, QueryResult
from insight_pilot.tools.duckdb_executor import execute_sql as _execute_sql_core
from insight_pilot.tools.exemplar_store import (
    Exemplar,
    retrieve_exemplars,
    save_exemplar,
)
from insight_pilot.tools.knowledge_base import retrieve_business_context


# ============================================================================
# 节点 0：knowledge_retrieval_node
#
# 第一个跑的节点。从 ChromaDB 检索和 user_query 相关的业务术语。
# 检索失败也不致命（retrieve_business_context 异常时返回空字符串）。
# ============================================================================
def knowledge_retrieval_node(state: AgentState) -> dict:
    """
    知识库检索节点：
      1. 检索业务知识 → State.business_context
      2. 检索历史 exemplar → State.retrieved_exemplars

    两个检索同时进行，都失败也不致命（返回空字符串/空列表）。
    """
    user_query = state["user_query"]

    # 业务知识库检索（第四阶段已有）
    context = retrieve_business_context(user_query, top_k=5)

    # 历史 exemplar 检索（新增）
    # only_approved=True：只用经审批通过的样本，保证质量
    exemplars = retrieve_exemplars(user_query, top_k=3, only_approved=True)
    # 转 dict 列表存进 state（方便序列化 + Pydantic 过校验）
    exemplar_dicts = [
        {
            "user_question": e.user_question,
            "execution_plan": e.execution_plan,
            "sqls": e.sqls,
            "timestamp": e.timestamp,
            "approved_by_reviewer": e.approved_by_reviewer,
            "exemplar_id": e.exemplar_id,
        }
        for e in exemplars
    ]

    return {
        "business_context": context,
        "retrieved_exemplars": exemplar_dicts,
        "iteration_count": state.get("iteration_count", 0) + 1,
    }


# ============================================================================
# 节点 1：planner_node
#
# 一次 LLM 调用产出 list[ExecutionStep]。
# 现在会读 State.business_context 给 Planner 注入业务术语。
# ============================================================================
def planner_node(state: AgentState) -> dict:
    """
    Planner 节点：拆解 user_query 为执行步骤序列。

    现在还会读 State.retrieved_exemplars，作为 few-shot 注入 Planner prompt。
    """
    planner = build_planner()

    # 把 dict 形式的 exemplar 转回 Exemplar 对象给 planner 用
    exemplar_dicts = state.get("retrieved_exemplars", []) or []
    exemplars = [
        Exemplar(
            user_question=d["user_question"],
            execution_plan=d.get("execution_plan", []),
            sqls=d.get("sqls", []),
            timestamp=d.get("timestamp", ""),
            approved_by_reviewer=d.get("approved_by_reviewer", False),
            exemplar_id=d.get("exemplar_id", ""),
        )
        for d in exemplar_dicts
    ]

    steps = planner(
        user_query=state["user_query"],
        business_context=state.get("business_context", ""),
        exemplars=exemplars,
    )

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

    # 构造消息：可选业务上下文 + 子任务
    # 业务上下文用 SystemMessage 注入，让 LLM 把它当成"环境信息"而非用户请求
    messages: list = []
    biz_ctx = state.get("business_context", "")
    if biz_ctx.strip():
        messages.append(SystemMessage(content=(
            "以下是从业务知识库检索到的相关上下文，"
            "在写 SQL 时请遵守这些定义和口径：\n\n" + biz_ctx
        )))
    messages.append(HumanMessage(content=f"任务：{current_step.description}"))

    agent_result = agent.invoke({"messages": messages})
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

    # 构造消息：可选业务上下文 + 子任务（含 step_id）
    messages: list = []
    biz_ctx = state.get("business_context", "")
    if biz_ctx.strip():
        messages.append(SystemMessage(content=(
            "以下是从业务知识库检索到的相关上下文，"
            "在写代码或给业务结论时请参考：\n\n" + biz_ctx
        )))
    messages.append(HumanMessage(content=(
        f"任务：{current_step.description}\n"
        f"step_id：{current_step.step_id}（请用这个数字命名图表文件）"
    )))

    agent_result = agent.invoke({"messages": messages})
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
# 节点 4：reporter_node
#
# 所有 plan 步骤跑完后，综合 State 生成 Markdown 报告。
# ============================================================================
def reporter_node(state: AgentState) -> dict:
    """
    Reporter 节点：把执行结果综合成 Markdown 报告。
    """
    reporter = build_reporter()
    report_md = reporter(state)

    return {
        "report_markdown": report_md,
        "status": "complete",
        "iteration_count": state.get("iteration_count", 0) + 1,
    }


# ============================================================================
# 路由函数：decide_next_step
#
# 【这个函数不是节点 —— 是条件边的判断器】
#   add_conditional_edges 接受这个函数，根据返回值决定下一个节点。
#   纯函数：读 state，返回字符串，无副作用。
#
# 【返回值的含义（Phase 4 更新）】
#   "query"     → 跑 query_node
#   "analysis"  → 跑 analysis_node
#   "report"    → 跑 reporter_node（所有步骤跑完时去这里）
# ============================================================================
def decide_next_step(state: AgentState) -> str:
    """
    根据当前 step_index 和 max_iterations 判断下一步去哪。
    """
    # ---- 安全闸：超过最大迭代次数强制去 reporter ----
    # 即使没跑完也写一份"半成品报告"，比直接 END 友好
    if state["iteration_count"] >= state["max_iterations"]:
        return "report"

    plan = state.get("execution_plan", [])
    idx = state.get("current_step_index", 0)

    # ---- 所有步骤跑完了 → 去写报告 ----
    if idx >= len(plan):
        return "report"

    # ---- 看当前 step 的 type ----
    current_step = plan[idx]
    if current_step.step_type == "query":
        return "query"
    elif current_step.step_type == "analysis":
        return "analysis"
    else:
        # 不应该发生（Pydantic 已校验），但留个兜底
        return "report"


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
def build_graph(checkpointer=None) -> CompiledStateGraph:
    """
    构造 Phase 5 完整图。

    Args:
        checkpointer: 可选的 Checkpointer 实例。
                     不传则默认用 SqliteSaver 写到 .checkpoints.db。
                     测试可以传 MemorySaver 避免污染磁盘。

    拓扑：
      START → knowledge_retrieval → planner
            → step_router →（query | analysis）→ step_router → ... → reporter
            → reviewer →（可能 interrupt）→ END
    """
    builder = StateGraph(AgentState)

    # ---- 加节点 ----
    # Phase 5 拓扑：knowledge_retrieval（入口）→ planner → 多步循环 → reporter → reviewer → END
    builder.add_node("knowledge_retrieval", knowledge_retrieval_node)
    builder.add_node("planner", planner_node)
    builder.add_node("query", query_node)
    builder.add_node("analysis", analysis_node)
    builder.add_node("reporter", reporter_node)
    builder.add_node("reviewer", reviewer_node)

    # ---- 加边 ----
    # 入口：START → knowledge_retrieval → planner
    builder.add_edge(START, "knowledge_retrieval")
    builder.add_edge("knowledge_retrieval", "planner")

    # planner / query / analysis 跑完后都进同一个路由
    # 路由结果：query / analysis / report
    routes = {
        "query": "query",
        "analysis": "analysis",
        "report": "reporter",
    }
    builder.add_conditional_edges("planner", decide_next_step, routes)
    builder.add_conditional_edges("query", decide_next_step, routes)
    builder.add_conditional_edges("analysis", decide_next_step, routes)

    # Phase 5 新增：reporter → reviewer → END
    # reviewer 节点内部可能触发 interrupt()
    # 不管 approve / reject，最终都到 END（reject 时 status="failed"）
    builder.add_edge("reporter", "reviewer")
    builder.add_edge("reviewer", END)

    # ---- 编译图 ----
    # 【Phase 5 关键】传 Checkpointer 才能让 interrupt() 工作
    # SqliteSaver 把 state 写到 .checkpoints.db 文件，跨进程持久化
    # 没有 Checkpointer → interrupt() 无法恢复（state 没地方存）
    if checkpointer is None:
        # 默认行为：用 SqliteSaver，文件在项目根的 .checkpoints.db
        from insight_pilot.config import get_settings
        from langgraph.checkpoint.sqlite import SqliteSaver
        import sqlite3

        settings = get_settings()
        db_path = settings.project_root / ".checkpoints.db"
        # check_same_thread=False：允许多线程访问（pytest 等场景）
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        checkpointer = SqliteSaver(conn)

    return builder.compile(checkpointer=checkpointer)


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
