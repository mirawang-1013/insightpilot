"""
state.py —— 全图共享状态的契约（整个项目最关键的文件）

【这个文件定义了什么】
    1. AgentState           —— LangGraph StateGraph 读写的"黑板"
    2. ExecutionStep        —— Planner 产出的单个步骤（Pydantic，LLM 用）
    3. QueryResult          —— SQL 执行结果（dataclass）
    4. AnalysisResult       —— Python 分析结果（dataclass）
    5. create_initial_state —— 构造合法初始状态的工厂函数

【三种类型系统的分工】
    - TypedDict            → AgentState（LangGraph 原生支持，合并语义靠 reducer）
    - dataclass            → QueryResult / AnalysisResult（内部生产，无需运行时校验）
    - Pydantic BaseModel   → ExecutionStep（LLM 结构化输出，必须运行时校验）

【修改这个文件的影响】
    改字段会波及 5 个 Agent + graph.py + main.py。
    改动前请读一遍 docs/design-decisions.md §4（State 设计）。
"""

from __future__ import annotations

import operator                              # operator.add 做列表合并 reducer
from dataclasses import dataclass, field, asdict
from typing import Annotated, Any, Literal, TypedDict

# LangChain 的 BaseMessage 是 LLM 对话历史的基础类型（HumanMessage / AIMessage 等都继承它）
from langchain_core.messages import BaseMessage

# LangGraph 官方的 messages reducer —— 比 operator.add 智能
# 能正确处理：去重（基于消息 ID）、tool_call 和 tool_response 配对
from langgraph.graph.message import add_messages

# Pydantic 用于 LLM 结构化输出的 schema
from pydantic import BaseModel, Field


# ============================================================================
# Part 1：ExecutionStep —— Planner 的产出单元
#
# 【为什么用 Pydantic BaseModel 而不是 dataclass？】
#   Planner 会这样调用 LLM：
#     structured_llm = llm.with_structured_output(list[ExecutionStep])
#     plan = structured_llm.invoke(user_query)
#
#   LLM 返回的是 JSON，Pydantic 在反序列化时做运行时校验：
#     - 字段类型不对？拒绝
#     - 必填字段缺失？拒绝
#     - step_type 不在 Literal 列表里？拒绝
#
#   dataclass 不做运行时校验，LLM 糟糕的输出会在下游崩。
# ============================================================================
class ExecutionStep(BaseModel):
    """
    规划器产出的单个执行步骤。一个 user_query 会被拆成 1-5 个 step。

    【字段 description 的妙用】
      Pydantic 的 Field(description=...) 会被 with_structured_output 自动
      转成给 LLM 看的 schema 注释。写得越清楚，LLM 产出越准。
    """

    # 步骤编号，从 1 开始。ge=1 是 "greater or equal to 1"，pydantic 自动校验
    step_id: int = Field(
        ge=1,
        description="步骤编号，从 1 开始连续递增。",
    )

    # step_type 决定路由到哪个 Agent：
    #   "query"    → Query Agent（SQL 取数）
    #   "analysis" → Analysis Agent（Python 分析 + 画图）
    # Literal 限定只能是这两个值，LLM 返回别的会被 Pydantic 拒绝
    step_type: Literal["query", "analysis"] = Field(
        description=(
            "步骤类型。'query' 表示需要写 SQL 从数据库取数；"
            "'analysis' 表示需要写 Python 代码对前序取数结果做分析或画图。"
        ),
    )

    # 具体描述。min_length=10 防止 LLM 偷懒写"取数"两个字
    description: str = Field(
        min_length=10,
        description=(
            "这一步要做什么的详细描述（中文即可）。"
            "例如：'取 2017 年每月的订单数和总营收' 或"
            "'把 query_results[0] 画成折线图，x 轴月份 y 轴营收'。"
        ),
    )

    # 预留：未来可以用来做 DAG 依赖。第一版不用，LLM 也不需要产出。
    # 设默认值 = [] 让它变成可选字段
    depends_on: list[int] = Field(
        default_factory=list,
        description="依赖的步骤编号列表。默认为空（顺序执行）。",
    )


# ============================================================================
# Part 2：QueryResult —— SQL 执行结果
#
# 【为什么放这里不放 tools/duckdb_executor.py？】
#   这是跨模块的"领域契约"：executor 生产、state 存储、analysis 消费、reporter 读。
#   放中央定义处，所有消费者 import 同一个类。
#   tools/ 是实现细节，不该独占契约。
#
# 【为什么用 dataclass 不用 Pydantic？】
#   QueryResult 是由我们的 Python 代码内部构造的（execute_sql 函数），输入可控。
#   dataclass 够用 + 零运行时开销 + 有 asdict() 方便序列化。
# ============================================================================
@dataclass
class QueryResult:
    """单次 SQL 执行的完整结果。"""

    sql: str                                              # 原始 SQL（未包装 LIMIT 前）
    success: bool                                         # True = 执行成功
    columns: list[str] = field(default_factory=list)      # 列名列表
    rows: list[dict[str, Any]] = field(default_factory=list)  # 数据行（JSON 友好）
    row_count: int = 0                                    # 返回行数（截断后）
    truncated: bool = False                               # 是否因 max_rows 被截断
    error: str | None = None                              # 失败时的错误信息（已分类）
    execution_ms: int = 0                                 # 执行耗时

    def to_dict(self) -> dict[str, Any]:
        """转 dict，方便 JSON 序列化或存入 LangGraph State。"""
        return asdict(self)

    def to_llm_string(self, preview_rows: int = 10) -> str:
        """
        渲染成 LLM 友好的字符串（给工具返回值用）。

        【为什么要这个方法？】
          LangChain tool 调用后会把返回值 str() 塞进 LLM message，
          直接 str(dataclass) 会打印全部行导致 token 爆炸。
          这个方法给出 top-N 预览 + 截断提示。
        """
        if not self.success:
            return f"[SQL 执行失败] {self.error}"

        lines = [
            f"[SQL 执行成功] 返回 {self.row_count} 行，耗时 {self.execution_ms}ms"
            + (" (已截断)" if self.truncated else ""),
            f"列：{', '.join(self.columns)}",
        ]

        if self.rows:
            n_show = min(preview_rows, len(self.rows))
            lines.append(f"样例（前 {n_show} 行）：")
            for row in self.rows[:preview_rows]:
                lines.append(f"  {row}")
            if self.row_count > preview_rows:
                lines.append(f"  ... 还有 {self.row_count - preview_rows} 行未显示")

        return "\n".join(lines)


# ============================================================================
# Part 3：AnalysisResult —— Python 分析执行结果
#
# Analysis Agent 产出的内容比 QueryResult 多一维：
#   - 代码本身（LLM 写的）
#   - stdout 输出（print 的东西）
#   - 图表文件路径（matplotlib 产出的 PNG）
#   - dataframe 摘要（可选，给 Reporter 参考）
# ============================================================================
@dataclass
class AnalysisResult:
    """单次 Python 分析执行的完整结果。"""

    step_id: int                                              # 对应 ExecutionStep.step_id
    success: bool                                             # True = 执行成功
    code: str = ""                                            # LLM 生成的 Python 代码
    stdout: str = ""                                          # 捕获的标准输出
    chart_paths: list[str] = field(default_factory=list)      # 生成的图表文件路径
    dataframe_summary: str | None = None                      # DataFrame 的 describe() 摘要
    error: str | None = None                                  # 失败时的错误信息
    execution_ms: int = 0                                     # 执行耗时

    def to_dict(self) -> dict[str, Any]:
        """转 dict 方便序列化。"""
        return asdict(self)

    def to_llm_string(self) -> str:
        """LLM 友好渲染。"""
        if not self.success:
            return f"[分析执行失败] step_{self.step_id}: {self.error}"

        lines = [f"[分析执行成功] step_{self.step_id}，耗时 {self.execution_ms}ms"]
        if self.stdout:
            # stdout 可能很长，截断显示
            preview = self.stdout[:500]
            if len(self.stdout) > 500:
                preview += f"... (还有 {len(self.stdout) - 500} 字符)"
            lines.append(f"输出：\n{preview}")
        if self.chart_paths:
            lines.append(f"生成图表：{self.chart_paths}")
        if self.dataframe_summary:
            lines.append(f"数据摘要：\n{self.dataframe_summary}")
        return "\n".join(lines)


# ============================================================================
# Part 4：AgentState —— LangGraph 的共享黑板
#
# 【为什么是 TypedDict 而不是 dataclass / Pydantic？】
#   LangGraph 内部把 state 当成 dict 处理，多个节点返回的 dict 靠 reducer 合并。
#   TypedDict 是 dict 的"类型提示壳子"，和 LangGraph 的合并机制完美兼容。
#   dataclass / Pydantic 会和 LangGraph 内部合并逻辑打架。
#
# 【Annotated[list[X], operator.add] 的核心作用】
#   默认：节点返回 {"field": new_value} → state["field"] 被覆盖
#   有 Annotated reducer：→ state["field"] = reducer(old, new)
#
#   对 list 用 operator.add 就是列表拼接（Python 的 a + b 语义）。
#   这让循环里每次迭代能"追加"而不是"覆盖"。
#
# 【字段加减的原则】
#   加：只加"跨节点共享"的数据
#   不加：节点内部的临时变量（放函数局部）
#   一个字段只要有一个节点读它又有另一个节点写它，就放 state
# ============================================================================
class AgentState(TypedDict):
    """
    全图共享状态。所有 Agent 都读写它。

    字段按"生命周期阶段"分组注释。
    """

    # ========== 原始输入 ==========
    # 用户的自然语言问题，全程只读，入口节点写入后不再改
    user_query: str

    # ========== Planner 产出 ==========
    # 执行计划：有序的 ExecutionStep 列表
    # 不加 reducer，默认覆盖语义 —— Planner 只运行一次（除非重新规划）
    execution_plan: list[ExecutionStep]

    # 当前执行到第几步（0-indexed）。路由节点靠它决定"再循环还是结束"
    current_step_index: int

    # ========== 知识库检索（第四阶段）==========
    # RAG 从 ChromaDB 检索出的业务上下文，插进 Agent 的 prompt
    business_context: str

    # ========== 历史 exemplar 检索（自我改善飞轮）==========
    # 从 exemplar_store 检索到的 Top-K 历史成功查询
    # 每条是 dict 格式（Exemplar.to_metadata 的反序列化），便于序列化进 LangGraph state
    # 一次性写入（在 knowledge_retrieval_node 里），无 reducer
    retrieved_exemplars: list[dict[str, Any]]

    # ========== Query Agent 产出（累积）==========
    # 已探查过的表名。防止 Agent 在 ReAct 循环里重复探查同一张表
    explored_schemas: Annotated[list[str], operator.add]

    # SQL 查询结果列表 —— 每个 query 步骤追加一条
    # 这是为什么必须用 operator.add：循环里每次迭代都在累积，不能覆盖
    query_results: Annotated[list[QueryResult], operator.add]

    # ========== Analysis Agent 产出（累积）==========
    # Python 分析结果列表 —— 每个 analysis 步骤追加一条
    analysis_results: Annotated[list[AnalysisResult], operator.add]

    # 所有生成的图表文件路径（从 analysis_results 抽取的快捷访问）
    # 冗余存储：既在 AnalysisResult.chart_paths 里又在这里 —— 因为 Reporter
    # 要一次性拿到所有图表，遍历 analysis_results 提取有点啰嗦
    chart_paths: Annotated[list[str], operator.add]

    # ========== Reporter 产出 ==========
    # 最终的 Markdown 报告。覆盖语义（Reporter 只运行一次，或重写）
    report_markdown: str

    # ========== LLM 对话历史 ==========
    # add_messages 是 LangGraph 官方推荐的 messages reducer：
    #   - 基于消息 ID 去重
    #   - 正确处理 tool_call / tool_response 配对
    # 注意：不要用 operator.add 代替 add_messages，会丢 id 合并能力
    messages: Annotated[list[BaseMessage], add_messages]

    # ========== 循环保护（安全机制）==========
    # ReAct 循环计数。每次节点进入时 +1，超过 max_iterations 就强制退出
    # 防止 LLM 在 ReAct 里打转（工具一直调不出有意义的结果）
    iteration_count: int
    max_iterations: int

    # ========== 人机协同（第五阶段）==========
    # Reviewer 节点判断"是否需要人工审批"后设这两个字段
    # interrupt() 触发时暂停，等 Command(resume=...) 传入 human_feedback
    needs_human_review: bool
    human_feedback: str | None

    # ========== 通用状态 ==========
    # status 字段用 Literal 限定合法值，路由时更安全
    # Literal 不阻止赋值时写错（TypedDict 无运行时校验），但 IDE/mypy 会警告
    status: Literal[
        "planning",     # Planner 正在规划
        "executing",    # 正在跑某个 query step
        "analyzing",    # 正在跑某个 analysis step
        "reporting",    # Reporter 正在综合报告
        "reviewing",    # 等待人工审批中
        "complete",     # 全流程结束
        "failed",       # 失败终止
    ]

    # 错误信息。非 None 表示图跑失败了，终止条件路由会检查它
    error: str | None


# ============================================================================
# Part 5：create_initial_state —— 工厂函数
#
# 【为什么要工厂函数？】
#   TypedDict 没有构造器（不像 dataclass 有 __init__），
#   让使用者手动写 `{"user_query": ..., "execution_plan": [], ...}`
#   既啰嗦又容易漏字段。
#
#   工厂函数集中了"合法初始状态"的定义，main.py 只需传 user_query 一个参数。
# ============================================================================
def create_initial_state(
    user_query: str,
    max_iterations: int | None = None,
) -> AgentState:
    """
    构造一个合法的初始 AgentState。

    Args:
        user_query: 用户的自然语言问题。
        max_iterations: 可选的循环上限。None 则从 settings 读默认值。

    Returns:
        字段齐全的 AgentState dict，可以直接喂给 graph.invoke()。
    """
    # 懒导入 config：避免 state.py import 时就触发 .env 加载
    # （某些测试场景会在 import 之后 monkeypatch 环境变量）
    from insight_pilot.config import get_settings

    if max_iterations is None:
        max_iterations = get_settings().max_iterations

    return AgentState(
        user_query=user_query,
        execution_plan=[],
        current_step_index=0,
        business_context="",
        retrieved_exemplars=[],
        explored_schemas=[],
        query_results=[],
        analysis_results=[],
        chart_paths=[],
        report_markdown="",
        messages=[],
        iteration_count=0,
        max_iterations=max_iterations,
        needs_human_review=False,
        human_feedback=None,
        status="planning",
        error=None,
    )


# ============================================================================
# Part 6：开发自检
#
# 运行：python -m insight_pilot.state
# 验证所有类定义合法、初始状态能正确构造。
# ============================================================================
if __name__ == "__main__":
    # 1. 测试 ExecutionStep 的 Pydantic 校验
    step = ExecutionStep(
        step_id=1,
        step_type="query",
        description="取 2017 年每月订单数和总营收",
    )
    print(f"ExecutionStep 构造成功：{step.model_dump()}")

    # 2. 测试 QueryResult
    qr = QueryResult(
        sql="SELECT 1",
        success=True,
        columns=["one"],
        rows=[{"one": 1}],
        row_count=1,
    )
    print(f"\nQueryResult to_llm_string:\n{qr.to_llm_string()}")

    # 3. 测试 AnalysisResult
    ar = AnalysisResult(
        step_id=2,
        success=True,
        code="print('hi')",
        stdout="hi\n",
        chart_paths=["outputs/chart_1.png"],
    )
    print(f"\nAnalysisResult to_llm_string:\n{ar.to_llm_string()}")

    # 4. 测试初始状态构造
    # 注意：这步会触发 config 加载，需要 .env 存在
    try:
        state = create_initial_state("2017 年月度营收趋势")
        print("\n初始 AgentState 字段：")
        for k, v in state.items():
            # 把值截断显示，避免 messages / plan 这种 list 刷屏
            preview = str(v)[:60]
            print(f"  {k}: {preview}{'...' if len(str(v)) > 60 else ''}")
    except Exception as e:
        print(f"\n[跳过] 初始状态构造需要 .env：{e}")
