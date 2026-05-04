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
      insight-pilot query "对比 Top 5 品类，给出投资建议"
    """
    from datetime import datetime
    from langgraph.types import Command

    console.print()
    console.print(Panel(
        f"[bold]{question}[/]",
        title="[cyan]问题[/]",
        border_style="cyan",
    ))

    # ---- 构造图 + 初始状态 ----
    with console.status("[bold cyan]构建图...[/]", spinner="dots"):
        graph = build_graph()
        initial_state = create_initial_state(question)

    # ---- thread_id：让 Checkpointer 能区分多次会话 ----
    # 时间戳 + 随机后缀，唯一可读
    thread_id = f"q_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    config = {"configurable": {"thread_id": thread_id}}

    console.print(f"\n[dim]thread_id: {thread_id}[/]")
    console.print("[dim]开始执行 ReAct 循环...[/]\n")

    # ---- 第一次跑图，可能触发 interrupt ----
    final_state = _run_graph_with_interrupt_handling(
        graph=graph,
        first_input=initial_state,
        config=config,
    )

    if final_state is None:
        # 用户中断或异常退出
        return

    # ---- 渲染最终结果 ----
    _render_final_result(initial_state, final_state, verbose=verbose)


def _run_graph_with_interrupt_handling(
    graph: Any,
    first_input: Any,
    config: dict,
) -> dict | None:
    """
    跑图，支持 interrupt 循环。

    流程：
      1. 第一次 invoke
      2. 检查返回值有没有 __interrupt__
      3. 有：渲染 + 收用户输入 + Command(resume=...) 再 invoke
      4. 没有：直接返回 final_state

    Args:
        graph: 编译好的 StateGraph
        first_input: 第一次调用的输入（initial_state）
        config: 含 thread_id 的配置

    Returns:
        final_state dict，或 None（用户取消）
    """
    from langgraph.types import Command

    current_input = first_input
    max_resume_loops = 5   # 最多 5 次 interrupt 循环（防御无限循环）
    loop_count = 0

    while True:
        loop_count += 1
        if loop_count > max_resume_loops:
            console.print(f"\n[red]❌ 达到最大 interrupt 循环次数 ({max_resume_loops})，放弃[/]")
            return None

        try:
            # 关键：用 stream 跑图能看到中间事件，最后一次的累积 state 就是 final
            # 但 stream 的 last value 不好拿 —— 用 invoke 简单
            result = graph.invoke(current_input, config=config)
        except Exception as e:
            console.print(f"\n[red]❌ 图执行失败：{e}[/]")
            return None

        # ---- 检查有没有 interrupt ----
        # LangGraph 把 interrupt 信息暴露在 result 的 __interrupt__ 字段
        interrupt_data = result.get("__interrupt__")

        if not interrupt_data:
            # 没有 interrupt，跑完了
            return result

        # ---- 有 interrupt：渲染 + 收输入 ----
        # interrupt_data 是 list[Interrupt]，通常一次只有一个
        interrupt_obj = interrupt_data[0]
        interrupt_value = interrupt_obj.value if hasattr(interrupt_obj, "value") else interrupt_obj

        decision = _handle_interrupt(interrupt_value)

        if decision is None:
            # 用户 Ctrl+C 取消
            console.print("\n[yellow]⚠️  用户取消[/]")
            return None

        # ---- 用 Command(resume=...) 恢复 ----
        # 下一轮 invoke 用 Command 而不是 initial_state
        current_input = Command(resume=decision)


def _handle_interrupt(interrupt_value: dict) -> str | None:
    """
    渲染敏感报告，让用户决定 approve / reject。

    Returns:
        "approve" / "reject"，或 None（用户取消）
    """
    console.print()
    console.rule("[bold yellow]⚠️  人工审批[/]")
    console.print()

    # interrupt_value 含 report、reason、options
    reason = interrupt_value.get("reason", "?")
    matched_layer = interrupt_value.get("matched_layer", "?")
    report = interrupt_value.get("report", "")

    console.print(Panel(
        f"[bold]触发原因：[/]{reason}\n[dim]检测层：{matched_layer}[/]",
        border_style="yellow",
        title="为什么需要审批",
    ))
    console.print()

    # 渲染报告本身
    console.print(Markdown(report))
    console.print()

    # 收输入
    console.print("[bold]请决定：[/]")
    console.print("  [green]a[/] 通过（按当前报告输出）")
    console.print("  [red]r[/]  驳回（不输出报告）")
    console.print()

    try:
        # rich Console 没有 input，直接用内置 input
        # 但要让光标可见，用 Console.input（rich 提供，会自动 flush）
        choice = console.input("[bold cyan]你的选择 (a/r): [/]").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return None

    if choice.startswith("a"):
        return "approve"
    elif choice.startswith("r"):
        return "reject"
    else:
        # 默认（保守）：如果用户输了奇怪的东西就当 reject
        console.print(f"[yellow]未识别 '{choice}'，按 reject 处理[/]")
        return "reject"


def _render_final_result(
    initial_state: dict[str, Any],
    final_state: dict[str, Any],
    verbose: bool = False,
) -> None:
    """渲染最终的查询结果 + 保存并展示 Markdown 报告。"""
    # query_results 是累积的 QueryResult 列表
    query_results = final_state.get("query_results", [])

    if query_results:
        console.print()
        console.print(f"[bold]📊 共执行 {len(query_results)} 条 SQL 查询[/]\n")
        # 展示最后一条结果为表格（最常是用户最关心的那条）
        last_result = query_results[-1]
        _render_query_result_as_table(last_result)

    # ---- Phase 4 新增：保存并渲染 Markdown 报告 ----
    report_md = final_state.get("report_markdown", "")
    if report_md:
        _save_and_render_report(report_md)
    else:
        console.print("\n[yellow]⚠️  未生成报告（report_markdown 字段为空）[/]")

    # verbose 模式：打印完整 messages
    if verbose:
        console.rule("[dim]Verbose: 所有 messages[/]")
        for msg in final_state.get("messages", []):
            console.print(f"[dim]{type(msg).__name__}:[/] {str(msg)[:200]}")


def _save_and_render_report(report_md: str) -> None:
    """
    把 Markdown 报告保存到 outputs/report_<timestamp>.md，
    并用 rich 在终端渲染（终端会显示带颜色的标题、表格等）。
    """
    from datetime import datetime
    from pathlib import Path
    from insight_pilot.config import get_settings

    settings = get_settings()
    outputs_dir = settings.outputs_dir
    outputs_dir.mkdir(exist_ok=True)

    # 时间戳文件名：方便追溯
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = outputs_dir / f"report_{ts}.md"
    report_path.write_text(report_md, encoding="utf-8")

    # ---- 终端渲染（用 rich 的 Markdown 解析器，支持标题/列表/代码块/链接）----
    console.rule(f"[bold green]📄 报告（已保存到 {report_path.relative_to(settings.project_root)}）[/]")
    console.print()
    console.print(Markdown(report_md))
    console.print()
    console.rule()


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
