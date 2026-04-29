"""
tools/lang_tools.py —— LangChain @tool 适配层（Hexagonal Architecture 外层）

【这个文件存在的意义】
    我们的核心工具（execute_sql / list_tables 等）返回 Python 对象，
    但 LangChain/LangGraph 的 Agent 要求工具：
      1. 是 BaseTool 实例（或 @tool 装饰的函数）
      2. 输入有 Pydantic schema，LLM 能看到参数格式
      3. 返回字符串（LLM 能直接读）

    把 @tool 装饰器写在核心工具文件里 → 核心代码被 LangChain 污染。
    新建一层适配器 → 核心代码保持框架无关，将来换 LlamaIndex / AutoGen 都好说。

【架构图（Hexagonal / Ports-and-Adapters）】

        ┌────────────────────────────────────┐
        │  LangGraph Agent (外层)            │
        │    用 Pydantic schema 的 BaseTool  │
        └───────────────┬────────────────────┘
                        ↓  通过 @tool 装饰器
        ┌────────────────────────────────────┐
        │  lang_tools.py (适配层 —— 本文件)  │
        │    负责：Pydantic schema 定义      │
        │          调用核心函数              │
        │          结果 → 字符串             │
        └───────────────┬────────────────────┘
                        ↓  直接调用
        ┌────────────────────────────────────┐
        │  核心工具（框架无关）              │
        │    duckdb_executor / metadata_     │
        │    explorer / ...                  │
        └────────────────────────────────────┘

【向外暴露的接口】
    QUERY_AGENT_TOOLS: list[BaseTool]
    把这个列表传给 create_react_agent(tools=...) 即可。
"""

from __future__ import annotations

from langchain_core.tools import tool

# 从核心工具导入"纯函数"版本
from insight_pilot.tools.duckdb_executor import execute_sql as _execute_sql_core
from insight_pilot.tools.metadata_explorer import (
    describe_table as _describe_table_core,
    list_tables as _list_tables_core,
    sample_rows as _sample_rows_core,
)


# ============================================================================
# 工具 1：list_tables
#
# 【@tool 装饰器做的事】
#   1. 从函数签名生成 Pydantic 输入 schema（LLM 能看到参数名、类型、描述）
#   2. 把 docstring 作为"工具说明"给 LLM 读
#   3. 注册成 LangChain BaseTool 实例
#
# 【关键：docstring 就是 prompt】
#   LLM 决定"什么时候调这个工具"完全靠 docstring。
#   写的越清晰，LLM 调度越准确。
#   把"什么时候该用"和"返回什么"都写进去。
# ============================================================================
@tool
def list_tables() -> str:
    """
    列出数据仓库中所有可用的表和视图。

    何时使用：
      - 会话一开始，不知道有什么数据时
      - 想确认某张表是否存在时

    返回：Markdown 格式的表/视图清单，含每张表的行数和业务说明。
    """
    return _list_tables_core()


# ============================================================================
# 工具 2：describe_table
#
# 注意函数参数 table_name 的类型注解 + docstring —— 这两样会自动合成
# LLM 看到的 schema：{"table_name": "string, 表或视图名称"}
# ============================================================================
@tool
def describe_table(table_name: str) -> str:
    """
    查看指定表或视图的字段 schema（字段名、类型、可空性）。

    何时使用：
      - 想写 SQL 前，确认字段名和类型
      - 看到字段不存在的错误后，重新查正确字段名

    Args:
        table_name: 表或视图的名称（必须是 list_tables 返回过的）。

    返回：Markdown 格式的字段表 + 业务说明（如有）。
    """
    return _describe_table_core(table_name)


# ============================================================================
# 工具 3：sample_rows
#
# 【参数设计：n 有默认值】
#   LLM 调用时可以省略 n，用默认的 5。这降低了 LLM 调度成本。
#   n: int = 5 被 @tool 解析成可选参数。
# ============================================================================
@tool
def sample_rows(table_name: str, n: int = 5) -> str:
    """
    看表的若干行样例数据（随机采样），帮助判断字段值的格式和分布。

    何时使用：
      - describe_table 之后，想了解字段值的实际形态
      - 判断字符串字段是大写还是小写（如 order_status）
      - 确认日期字段的格式

    Args:
        table_name: 表或视图名称。
        n: 返回行数，默认 5，上限 20。

    返回：Markdown 格式的数据表。
    """
    return _sample_rows_core(table_name, n)


# ============================================================================
# 工具 4：execute_sql
#
# 【这个工具的返回值要特别处理】
#   核心的 execute_sql 返回 QueryResult dataclass。
#   但 LangChain tool 必须返回字符串给 LLM 看。
#   所以这里调用 .to_llm_string() 做转换。
#
# 【但我们还要把原始 QueryResult 留给 graph 层用】
#   第二阶段先不做，graph 里通过解析 messages 拿到最后的 SQL 结果。
#   第三阶段做结构化 State 写入时，会在 query_node 里直接调核心函数 + 更新 State。
# ============================================================================
@tool
def execute_sql(sql: str) -> str:
    """
    对 DuckDB 执行一条只读 SQL 查询，返回结果预览。

    安全约束：
      - 只允许 SELECT 或 WITH 开头的只读查询
      - 行数上限 500（超过会截断）
      - 执行超时 30 秒

    何时使用：
      - 已经通过 list_tables / describe_table / sample_rows 了解 schema
      - 准备好要取的数据对应的 SQL

    错误处理：
      - 如果执行失败，返回值会包含分类好的错误信息和修复建议
      - 根据建议（如 "用 describe_table 查字段名"）调对应工具，再重写 SQL

    Args:
        sql: 合法的 DuckDB SELECT 语句（PostgreSQL 方言）。

    返回：成功时是带列名+样例的字符串；失败时是带修复建议的错误。
    """
    # 调核心函数，拿到 QueryResult dataclass
    result = _execute_sql_core(sql)
    # 转换成 LLM 友好的字符串（QueryResult.to_llm_string 已经实现过）
    return result.to_llm_string(preview_rows=10)


# ============================================================================
# 导出：Query Agent 使用的工具列表
#
# 【为什么集中导出一个列表？】
#   Phase 2 只有 Query Agent，但 Phase 3 起会有 Analysis Agent、Planner 等。
#   不同 Agent 用不同工具子集：
#     - Query Agent → 4 个数据探查/执行工具（本列表）
#     - Analysis Agent → python_sandbox + 数据读取工具
#     - Planner → 无工具（纯 LLM 结构化输出）
#
#   所以每个文件导出"它服务的 Agent 的工具列表"最清晰。
# ============================================================================
QUERY_AGENT_TOOLS = [
    list_tables,
    describe_table,
    sample_rows,
    execute_sql,
]
