"""
agents/planner.py —— Planner Agent 工厂

【职责】
    一次 LLM 调用，把用户的自然语言问题拆成 list[ExecutionStep]。
    无工具、无循环、不看数据，纯文本输入文本（结构化）输出。

【核心 API: llm.with_structured_output(schema)】
    LangChain 的杀手锏：把 Pydantic 模型变成 LLM 的输出契约。
    LLM 必须返回符合 schema 的 JSON，否则自动重试。

    底层链路：
      Pydantic ExecutionStep
          ↓ (langchain 自动转换)
      OpenAI function calling JSON schema
          ↓ (openai api 强制约束)
      LLM 输出严格合法的 JSON
          ↓ (langchain 反序列化)
      list[ExecutionStep] Python 对象

【为什么没有 build_planner_agent() 返回 CompiledStateGraph？】
    Planner 不是 ReAct Agent（不调工具、不循环），不需要 LangGraph 的图。
    它就是一个"输入文本 → 输出结构"的纯函数。
    所以工厂直接返回函数对象 plan(query: str) -> list[ExecutionStep]。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from insight_pilot.config import get_settings
from insight_pilot.prompts.planner import PLANNER_SYSTEM_PROMPT
from insight_pilot.state import ExecutionStep


# ============================================================================
# 输出 schema 的"包装类"
#
# 【为什么要包一层 ExecutionPlan，而不直接 with_structured_output(list[ExecutionStep])？】
#   OpenAI function calling 要求顶层是 object（不是 array）。
#   如果直接传 list[ExecutionStep]，langchain 会报错。
#   所以用一个 wrapper：{ "steps": [...] } 让顶层是 object。
#
#   这是和 OpenAI API 限制的妥协，不是设计选择。
# ============================================================================
from pydantic import BaseModel, Field


class ExecutionPlan(BaseModel):
    """
    执行计划的顶层 wrapper。

    LLM 实际返回的 JSON 长这样：
      { "steps": [
          { "step_id": 1, "step_type": "query", "description": "..." },
          { "step_id": 2, "step_type": "analysis", "description": "..." }
        ]
      }
    """

    steps: list[ExecutionStep] = Field(
        description=(
            "有序的执行步骤列表。step_id 从 1 开始连续递增。"
            "简单问题用 1 步；中等 2-3 步；复杂 3-5 步。超过 5 步通常是过度拆分。"
        ),
    )


# ============================================================================
# 工厂函数：build_planner
#
# 返回一个函数 plan(query: str) -> list[ExecutionStep]
# 每次调用 plan() 都用同一个 LLM 实例（在工厂里构造一次复用）。
# ============================================================================
def build_planner():
    """
    构造 Planner。

    Returns:
        plan(query: str) -> list[ExecutionStep]
        闭包函数，调用时执行实际的 LLM 调用并返回结构化计划。
    """
    settings = get_settings()

    # ---- LLM 实例 ----
    # temperature=0：拆步骤要确定性，不要创意
    llm = ChatOpenAI(
        model=settings.openai_model,
        temperature=0,
        api_key=settings.openai_api_key,
    )

    # ---- 关键：with_structured_output ----
    # 这一步把普通 ChatOpenAI 变成"必输出 ExecutionPlan 形状 JSON"的版本
    # method 参数选择：
    #   "function_calling"（默认）：用 OpenAI 的 tools 参数 —— 兼容性最好
    #   "json_mode"：用 OpenAI 的 response_format=json_object —— 不能保证 schema
    #   "json_schema"（gpt-4o 新功能）：原生支持 JSON Schema —— 最严格
    # 用默认的 function_calling 已经足够稳
    structured_llm = llm.with_structured_output(
        ExecutionPlan,
        method="function_calling",
    )

    # ---- 返回闭包 ----
    # 闭包捕获 structured_llm，每次 plan() 调用都复用同一个 LLM 实例
    def plan(user_query: str, business_context: str = "") -> list[ExecutionStep]:
        """
        把用户的自然语言问题拆成执行步骤。

        Args:
            user_query: 用户原始问题。
            business_context: 知识库检索到的业务上下文（可选）。
                如果非空，会作为额外 SystemMessage 注入，帮助 Planner 理解专业术语。

        Returns:
            list[ExecutionStep] —— 已经过 Pydantic 校验，下游可直接用。
        """
        # 构造消息：system prompt（教怎么拆）+ 可选业务上下文 + human message（用户问题）
        messages: list = [SystemMessage(content=PLANNER_SYSTEM_PROMPT)]

        # 如果有检索到的业务上下文，作为额外 system message 注入
        # 这样 Planner 就知道 "ROAS 怎么定义" "投资建议要看哪些维度" 等
        if business_context.strip():
            messages.append(SystemMessage(content=(
                "以下是从业务知识库检索到的相关上下文，"
                "在拆解步骤时请参考这些定义和口径：\n\n"
                + business_context
            )))

        messages.append(HumanMessage(content=user_query))

        # 调 LLM。返回的 result 已经是 ExecutionPlan 实例（Pydantic 对象）
        # 如果 LLM 返回的 JSON 不符合 schema，langchain-openai 会自动重试 3 次
        result: ExecutionPlan = structured_llm.invoke(messages)

        # 把 .steps 取出来作为 list[ExecutionStep] 返回
        return result.steps

    return plan


# ============================================================================
# 开发自检
#
# 用法：
#   uv run python -m insight_pilot.agents.planner "用户问题"
# ============================================================================
if __name__ == "__main__":
    import sys
    import json

    # 几个测试用例，覆盖不同复杂度
    test_queries = sys.argv[1:] if len(sys.argv) > 1 else [
        "2017 年总订单数？",
        "2017 年月度营收趋势？",
        "对比 Top 5 品类的营收和评分，给出投资建议",
    ]

    print("构建 Planner...")
    planner = build_planner()
    print()

    for q in test_queries:
        print("=" * 72)
        print(f"用户问题：{q}")
        print("-" * 72)

        try:
            steps = planner(q)
            print(f"拆解为 {len(steps)} 步：\n")
            # 用 model_dump 转 dict，json.dumps 美化输出
            print(json.dumps(
                [s.model_dump() for s in steps],
                ensure_ascii=False,
                indent=2,
            ))
        except Exception as e:
            print(f"[ERROR] {type(e).__name__}: {e}")
        print()
