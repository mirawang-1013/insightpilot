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
from insight_pilot.tools.python_sandbox import (
    SandboxInput,
    execute_python as _execute_python_core,
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
# Analysis Agent 的工具：run_python（工厂模式）
#
# 【为什么不能像 query 那样直接 @tool？】
#   run_python 需要把 LLM 的 code + 当前 State 里的 query_results 一起送进沙盒。
#   query_results 是动态的（每次跑图都不一样），不能写死在工具定义里。
#
# 【工厂模式：闭包捕获 query_results】
#   每次构建 Analysis Agent 时，把当前 query_results 传给工厂，
#   工厂返回一个"已经知道当前 query_results"的工具实例。
#
#   这是 LangChain 官方文档里"动态工具"的推荐模式。
# ============================================================================
def make_run_python_tool(
    query_results: list[dict],
    captures: list | None = None,
):
    """
    工厂函数：返回一个绑定了 query_results 的 run_python 工具。

    Args:
        query_results: 上游 query 步骤产出的 SQL 结果（QueryResult.to_dict() 列表）。
        captures: 可选的共享 list。每次工具调用产出的 AnalysisResult 会 append 进去。
                 graph 层用这个机制"在源头捕获结果"，避免事后重跑沙盒
                 （重跑会让 chart_paths 检测失效，因为图文件已经存在）。

    Returns:
        BaseTool 实例，可直接传给 create_react_agent(tools=[...])。
    """

    @tool
    def run_python(code: str, step_id: int = 0) -> str:
        """
        在隔离的 subprocess 沙盒里执行 Python 代码（pandas + matplotlib 已 import）。

        可用变量（自动注入，不用自己 import / load）：
          - query_results : list[dict] —— 上游 SQL 步骤的结果
                            形如 [{"sql": "...", "columns": [...], "rows": [{...}, ...]}, ...]
          - get_df(i)     : 把 query_results[i]["rows"] 转成 DataFrame 的便捷函数
          - step_id       : 当前步骤号（命名图表用）
          - pd            : pandas
          - plt           : matplotlib.pyplot
          - matplotlib    : matplotlib（已 use("Agg") 无 GUI 模式）

        画图保存约定：
          - 用 plt.savefig(f"chart_{step_id}_<name>.png") 命名
          - cwd 已是 outputs/，相对路径就行
          - 一定记得 plt.close() 释放内存

        何时使用：
          - 数据透视、相关性分析（pandas）
          - 画图（matplotlib）
          - 生成业务结论文字（用 print）

        Args:
            code: 完整的 Python 代码字符串。
            step_id: 当前步骤号，影响图表文件名。默认 0。

        返回：执行结果摘要（含 stdout 和图表路径）。
        """
        sandbox_input = SandboxInput(
            code=code,
            step_id=step_id,
            query_results=query_results,
        )
        result = _execute_python_core(sandbox_input)

        # 关键：在源头捕获结果到共享 list
        # 这样 graph 层不需要重跑沙盒就能拿到 AnalysisResult
        if captures is not None:
            captures.append(result)

        return result.to_llm_string()

    return run_python


# ============================================================================
# 导出：各 Agent 使用的工具列表
#
# 【为什么集中导出？】
#   不同 Agent 用不同工具子集：
#     - Query Agent → 4 个数据探查/执行工具（QUERY_AGENT_TOOLS，静态）
#     - Analysis Agent → run_python（动态工厂构建）
#     - Planner → 无工具（纯结构化输出）
# ============================================================================
QUERY_AGENT_TOOLS = [
    list_tables,
    describe_table,
    sample_rows,
    execute_sql,
]

# Analysis Agent 工具是动态的，导出工厂函数让 agents/analysis.py 调用
# 用法：tools = [make_run_python_tool(query_results)]
__all__ = [
    "QUERY_AGENT_TOOLS",
    "make_run_python_tool",
]
