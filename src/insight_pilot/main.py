"""
main.py —— InsightPilot CLI 入口

【命令】
    insight-pilot query "..."    跑一个查询
    insight-pilot demo           跑默认演示场景
    insight-pilot version        看版本

【技术栈】
    Typer —— 比 argparse 现代的 CLI 框架，用装饰器定义命令
    Rich  —— 终端美化：彩色 / 进度条 / Markdown / 表格

【UX 原则】
    让 LLM 的思考过程对用户可见：
      - 每次工具调用都打印
      - SQL 单独高亮显示
      - 结果用表格渲染
    这不只是炫技，是调试友好 + 面试演示效果。
"""

from __future__ import annotations

from typing import Any

import typer
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from insight_pilot import __version__
from insight_pilot.graph import build_graph
from insight_pilot.state import create_initial_state

# ============================================================================
# Typer app 实例 + Rich console 实例
#
# Typer 用装饰器 @app.command() 注册子命令
# Console 是 rich 的输出入口，替代 print()
# ============================================================================
app = typer.Typer(
    name="insight-pilot",
    help="LangGraph-based multi-agent data analysis system.",
    no_args_is_help=True,  # 不带任何参数时显示帮助（比默认的错误提示友好）
)
console = Console()


# ============================================================================
# 工具：流式渲染 Agent 的消息
#
# graph.stream() 返回的是 (node_name, node_update) 字典流。
# 这个函数负责"把每个事件渲染成好看的输出"。
# ============================================================================
def _render_event(node_name: str, node_update: dict[str, Any]) -> None:
    """渲染单个图事件到终端。"""
    messages = node_update.get("messages", [])
    if not messages:
        return

    # 只渲染新增的消息（不是整个 message 列表）
    # 实际上 stream(updates) 返回的就是增量，所以 messages 就是本轮新增
    for msg in messages:
        _render_message(msg)


def _render_message(msg: Any) -> None:
    """
    渲染单条 LangChain Message。

    消息类型：
      - HumanMessage       用户输入
      - AIMessage          LLM 思考/回答（可能带 tool_calls）
      - ToolMessage        工具返回
    """
    if isinstance(msg, AIMessage):
        # LLM 发的消息：可能是思考 + 工具调用，也可能是最终答案
        if msg.tool_calls:
            # 有工具调用：渲染"调用信息"
            for tc in msg.tool_calls:
                tool_name = tc["name"]
                args = tc["args"]
                # 特殊处理：execute_sql 的 sql 参数高亮显示
                if tool_name == "execute_sql" and "sql" in args:
                    console.print(f"[bold cyan]🔧 {tool_name}[/]")
                    console.print(
                        Syntax(args["sql"], "sql", theme="monokai", line_numbers=False)
                    )
                else:
                    # 其他工具：紧凑一行显示
                    args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
                    console.print(f"[bold cyan]🔧 {tool_name}[/]([dim]{args_str}[/])")
        elif msg.content:
            # 无工具调用的 AI 消息 = 最终答案
            console.print()
            console.print(Panel(
                Markdown(str(msg.content)),
                title="[bold green]✅ 结论[/]",
                border_style="green",
            ))

    elif isinstance(msg, ToolMessage):
        # 工具返回：紧凑显示，长的截断
        content = str(msg.content)
        # 第一行当摘要
        first_line = content.split("\n", 1)[0]
        console.print(f"   [dim]↳ {first_line[:120]}[/]")


# ============================================================================
# 命令：version
# ============================================================================
@app.command()
def version() -> None:
    """打印版本号。"""
    console.print(f"[bold]InsightPilot[/] v{__version__}")


# ============================================================================
# 命令：query
#
# 【Typer 怎么解析参数】
#   函数参数变成 CLI 参数：
#     - 位置参数 → 位置参数
#     - 带默认值的 → 可选 --flag
#   类型注解驱动转换（str/int/bool/Path 都自动）
# ============================================================================
@app.command()
def query(
    # typer.Argument 是位置参数，... 表示必填
    question: str = typer.Argument(..., help="你的自然语言问题"),
    # typer.Option 是 --flag 可选参数，默认值 False
    verbose: bool = typer.Option(False, "--verbose", "-v", help="打印完整 messages"),
) -> None:
    """
    用自然语言问一个数据问题。

    例子：
      insight-pilot query "2017 年月度营收趋势"
      insight-pilot query "SP 州客户的平均订单金额"
    """
    console.print()
    console.print(Panel(
        f"[bold]{question}[/]",
        title="[cyan]问题[/]",
        border_style="cyan",
    ))

    # ---- 构造图 + 初始状态 ----
    # 用 Rich 的 status 显示"加载中"动画
    with console.status("[bold cyan]构建图...[/]", spinner="dots"):
        graph = build_graph()
        initial_state = create_initial_state(question)

    console.print("\n[dim]开始执行 ReAct 循环...[/]\n")

    # ---- 流式跑图 ----
    # stream_mode="updates"：每个节点返回时 yield 一个 (node_name, update) 事件
    # 相比 stream_mode="values"，updates 更适合展示"哪个节点刚跑完"
    final_state: dict[str, Any] = {}
    try:
        for event in graph.stream(initial_state, stream_mode="updates"):
            for node_name, node_update in event.items():
                _render_event(node_name, node_update)
                # 累积 final_state（updates 模式下需要手动合并）
                final_state.update(node_update)
    except Exception as e:
        console.print(f"\n[red]❌ 图执行失败：{e}[/]")
        raise typer.Exit(1)

    # ---- 渲染最终结果 ----
    _render_final_result(initial_state, final_state, verbose=verbose)


def _render_final_result(
    initial_state: dict[str, Any],
    final_state: dict[str, Any],
    verbose: bool = False,
) -> None:
    """用表格渲染最终的查询结果。"""
    # query_results 是累积的 QueryResult 列表
    query_results = final_state.get("query_results", [])

    if not query_results:
        console.print("\n[yellow]⚠️  未拿到任何 SQL 查询结果[/]")
        return

    console.print()
    console.print(f"[bold]📊 共执行 {len(query_results)} 条 SQL 查询[/]\n")

    # 展示最后一条（最相关的）结果为表格
    last_result = query_results[-1]
    _render_query_result_as_table(last_result)

    # verbose 模式：打印完整 messages
    if verbose:
        console.rule("[dim]Verbose: 所有 messages[/]")
        for msg in final_state.get("messages", []):
            console.print(f"[dim]{type(msg).__name__}:[/] {str(msg)[:200]}")


def _render_query_result_as_table(result: Any) -> None:
    """把 QueryResult 渲染成 Rich Table。"""
    if not result.success:
        console.print(f"[red]查询失败：{result.error}[/]")
        return

    # 表标题：显示 SQL（截断）+ 行数
    sql_preview = result.sql[:80] + ("..." if len(result.sql) > 80 else "")
    table = Table(
        title=f"[bold]结果（{result.row_count} 行{'，已截断' if result.truncated else ''}）[/]",
        caption=f"[dim]{sql_preview}[/]",
        show_lines=True,
    )

    # 加列
    for col in result.columns:
        table.add_column(col, overflow="fold")

    # 加行（最多展示 20 行给终端，全量存在 state 里）
    for row in result.rows[:20]:
        table.add_row(*[str(row.get(c, "")) for c in result.columns])

    console.print(table)

    if len(result.rows) > 20:
        console.print(f"[dim]... 还有 {len(result.rows) - 20} 行未显示（完整数据在 State 里）[/]")


# ============================================================================
# 命令：demo
# ============================================================================
@app.command()
def demo() -> None:
    """跑默认演示场景（2017 月度营收趋势）。"""
    query("2017 年月度营收趋势是什么？")


# ============================================================================
# 入口（被 pyproject.toml 的 [project.scripts] 调用）
# ============================================================================
if __name__ == "__main__":
    app()
