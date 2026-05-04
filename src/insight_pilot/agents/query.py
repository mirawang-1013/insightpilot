"""
agents/query.py —— Query Agent 工厂

【职责】
    组装一个能"自然语言 → SQL → 结果"的 ReAct Agent：
      LLM (GPT-4o)
      + 4 个工具 (list_tables / describe_table / sample_rows / execute_sql)
      + system prompt (业务知识 + 工具使用协议)
      = create_react_agent(...) → Agent

【为什么是工厂函数而不是单例？】
    LLM 实例、工具绑定要在运行时构造（需要先加载配置）。
    工厂函数让调用方控制时机、也方便测试时替换 mock LLM。

【核心依赖：langgraph.prebuilt.create_react_agent】
    这是 LangGraph 官方的 prebuilt Agent，帮我们处理：
      - LLM.bind_tools(...) 把工具的 schema 喂给 LLM
      - 检测 LLM 输出里的 tool_calls
      - 执行工具并把结果塞回 messages
      - 循环直到 LLM 输出不再含 tool_calls（判定完成）
      - 防死循环的内置机制
    不自己写这个循环 —— 没必要重造轮子。
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.graph.state import CompiledStateGraph

from insight_pilot.config import get_settings
from insight_pilot.prompts.query import QUERY_AGENT_SYSTEM_PROMPT
from insight_pilot.tools.lang_tools import QUERY_AGENT_TOOLS


# ============================================================================
# 工厂函数：build_query_agent
# ============================================================================
def build_query_agent() -> CompiledStateGraph:
    """
    构造一个 Query Agent。

    Returns:
        CompiledStateGraph —— 可以直接 .invoke() 或 .stream() 的 Agent 实例。
        它本身也是一个 LangGraph 图，可以作为子图嵌入到主图的一个节点里。

    【为什么返回 CompiledStateGraph 而不是 AgentExecutor？】
        LangGraph 的 create_react_agent 返回 CompiledStateGraph，
        这是 LangGraph 原生的"已编译的图"。它可以被当成普通图调用，
        也可以被当成 StateGraph 的子节点嵌入进来 —— Phase 3 会用到。
    """
    settings = get_settings()

    # ---- 构造 LLM ----
    # ChatOpenAI 是 langchain-openai 封装的 OpenAI Chat API
    # 关键参数：
    #   - model: 用 settings.openai_model（默认 gpt-4o）
    #   - temperature: 0 让输出尽量确定（取数场景不需要创造性）
    #   - api_key: 从配置读
    llm = ChatOpenAI(
        model=settings.openai_model,
        temperature=0,
        api_key=settings.openai_api_key,
    )

    # ---- 用 create_react_agent 组装 ----
    # 底层发生的事（简化版）：
    #   1. llm.bind_tools(tools) —— 把工具 schema 喂给 LLM
    #   2. 返回一个图：
    #        START → agent (调 LLM)
    #             → conditional: 有 tool_calls? → tools (执行工具) → agent
    #                          : 没 tool_calls? → END
    #   3. 整个图被 compile() 成 CompiledGraph
    # LangChain V1.0 把 create_react_agent 改名为 create_agent 并移到 langchain.agents
    # 它内部仍然是 ReAct 模式，只是命名更通用
    agent = create_agent(
        model=llm,
        tools=QUERY_AGENT_TOOLS,
        system_prompt=QUERY_AGENT_SYSTEM_PROMPT,
    )

    return agent


# ============================================================================
# 开发自检
#
# 用法：
#   uv run python -m insight_pilot.agents.query "2017 年月度营收趋势"
#
# 会实际调 OpenAI API，消耗 token。第一次跑前确认 .env 里 OPENAI_API_KEY 正确。
# ============================================================================
if __name__ == "__main__":
    import sys
    from langchain_core.messages import HumanMessage

    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "2017 年月度营收趋势"

    print(f"问题：{query}\n")
    print("=" * 72)
    print("构建 Agent...")
    agent = build_query_agent()
    print("调用 Agent（这一步会实际调 OpenAI API）...\n")

    # 用 stream 模式看每一步
    # stream 返回 (event_type, state_update) 的元组
    # mode="updates" 只返回每个节点的增量更新
    for event in agent.stream(
        {"messages": [HumanMessage(content=query)]},
        stream_mode="updates",
    ):
        # event 形如 {"agent": {"messages": [...]}} 或 {"tools": {"messages": [...]}}
        for node_name, node_output in event.items():
            print(f"--- Node: {node_name} ---")
            if "messages" in node_output:
                for msg in node_output["messages"]:
                    # 每条消息取 content 的前 500 字符显示
                    content = getattr(msg, "content", "")
                    if isinstance(content, str) and content:
                        preview = content[:500]
                        print(preview)
                        if len(content) > 500:
                            print(f"... (还有 {len(content) - 500} 字符)")
                    # 如果是工具调用请求，显示工具名和参数
                    tool_calls = getattr(msg, "tool_calls", None)
                    if tool_calls:
                        for tc in tool_calls:
                            print(f"[调用工具] {tc['name']}({tc['args']})")
            print()
