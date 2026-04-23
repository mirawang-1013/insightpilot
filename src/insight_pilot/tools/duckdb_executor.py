"""
tools/duckdb_executor.py —— SQL 执行工具（Query Agent 的核心工具）

【职责】
    接受 SQL 字符串，返回结构化的 QueryResult。
    这是 LLM 通过 ReAct 循环调用的第一个工具。

【设计原则】
    1. 只读：正则白名单 + DuckDB read_only 双层防御
    2. 有界：行数上限（默认 500）+ 超时上限（默认 30 秒）
    3. LLM 友好：错误信息分类并附修复建议，让 Agent 能自我修正
    4. 结构化返回：dataclass 字段齐全，方便下游消费

【调用链】
    Query Agent (LLM) → execute_sql(sql) → QueryResult → LLM 读取并决策下一步
"""

from __future__ import annotations

import re                              # 正则做 SELECT 白名单校验
import threading                       # 子线程执行 + 超时控制
import time                            # 计时（execution_ms）
from typing import Any

import duckdb

from insight_pilot.config import get_settings

# QueryResult 定义在 state.py —— 作为跨模块契约的中央定义处。
# 详见 state.py 注释：executor 生产、state 存储、analysis/reporter 消费。
from insight_pilot.state import QueryResult


# ============================================================================
# 只读校验：正则白名单
#
# 【逻辑】
#   允许的 SQL 必须以 SELECT / WITH 开头（忽略前导空白和注释）。
#   WITH 也要允许，因为 CTE（WITH ... SELECT）是分析师常用的模式。
#
# 【这个正则的陷阱】
#   LLM 可能写出：
#     1) 前导注释：-- this is a comment\nSELECT ...
#     2) 前导空行：\n\n  SELECT ...
#     3) 小写：select * from orders
#   都要能通过。下面正则覆盖这三种情况。
# ============================================================================
# re.IGNORECASE：忽略大小写（select/SELECT）
# re.DOTALL：让 . 匹配换行，这样注释可以跨行
# ^\s*(--[^\n]*\n\s*)*：零或多个前导注释行 + 空白
# (SELECT|WITH)\b：SELECT 或 WITH，后面接词边界（防止 SELECTED 被错接受）
_READONLY_PATTERN = re.compile(
    r"^\s*(--[^\n]*\n\s*)*(SELECT|WITH)\b",
    re.IGNORECASE | re.DOTALL,
)


def _is_readonly_sql(sql: str) -> bool:
    """判断 SQL 是否是只读（SELECT 或 WITH 开头）。"""
    return bool(_READONLY_PATTERN.match(sql.strip()))


# ============================================================================
# 错误分类器：把 DuckDB 异常转成 LLM 可修复的提示
#
# 【核心理念】
#   ReAct Agent 的自我修复能力，本质上取决于"错误信息能不能被 LLM 看懂"。
#   我们不让 LLM 看原始 Python 异常（那是给人看的），而是返回带修复建议的
#   错误字符串，教 LLM 下一步该怎么做。
# ============================================================================
def _format_error(sql: str, exc: Exception) -> str:
    """
    把 DuckDB 异常转成 LLM 友好的错误信息。
    包含：错误分类 + 原始信息 + 修复建议。
    """
    msg = str(exc)

    # ---- 字段不存在 ----
    # DuckDB 错误信息例：'Referenced column "total_price" not found in FROM clause!'
    if "Referenced column" in msg or "does not have a column" in msg:
        return (
            f"字段不存在：{msg}\n"
            f"修复建议：用 describe_table 工具查一下目标表的字段名，"
            f"常见情况是字段名拼写不同（如 price vs total_price）。"
        )

    # ---- 表不存在 ----
    # DuckDB 错误信息例：'Catalog Error: Table with name xxx does not exist!'
    if "Catalog Error" in msg and ("Table" in msg or "does not exist" in msg):
        return (
            f"表不存在：{msg}\n"
            f"修复建议：用 list_tables 工具查可用表名。注意本项目表名已简化"
            f"（如 customers 而不是 olist_customers_dataset）。"
        )

    # ---- SQL 语法错误 ----
    if "Parser Error" in msg or "syntax error" in msg.lower():
        return (
            f"SQL 语法错误：{msg}\n"
            f"修复建议：检查逗号、括号、引号是否匹配；注意 DuckDB 使用"
            f"PostgreSQL 方言，字符串用单引号。"
        )

    # ---- 类型错误 ----
    # 例：'Binder Error: Cannot cast X to Y'
    if "Binder Error" in msg and "cast" in msg.lower():
        return (
            f"类型转换错误：{msg}\n"
            f"修复建议：检查字段类型。用 describe_table 查字段类型，"
            f"必要时用 CAST(x AS DOUBLE) 显式转换。"
        )

    # ---- 聚合错误 ----
    # 例：'column ... must appear in the GROUP BY clause'
    if "GROUP BY" in msg:
        return (
            f"GROUP BY 错误：{msg}\n"
            f"修复建议：SELECT 中非聚合字段必须出现在 GROUP BY 中。"
        )

    # ---- 超时（自己抛的）----
    if "timeout" in msg.lower():
        return (
            f"查询超时：{msg}\n"
            f"修复建议：SQL 可能扫描了过大的数据。尝试加 WHERE 过滤、"
            f"或用更小的日期范围。"
        )

    # ---- 其他未分类错误 ----
    return f"SQL 执行失败：{msg}"


# ============================================================================
# 超时包装器：子线程执行 + 超时取消
#
# 【为什么不用 signal.SIGALRM？】
#   - 只能在主线程用，LangGraph 会在非主线程调用
#   - 不跨平台（Windows 不支持）
#
# 【为什么不用 multiprocessing？】
#   - 进程创建开销大（每次查询多 100ms+）
#   - DuckDB 连接不能跨进程传递
#
# 【我们的方案：threading + con.interrupt()】
#   DuckDB 1.0+ 提供了 Connection.interrupt() 方法，可以中断正在执行的查询。
#   我们用一个子线程跑 SQL，主线程等 timeout 秒；超时就 interrupt()。
# ============================================================================
def _execute_with_timeout(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    timeout_seconds: int,
) -> tuple[list[str], list[tuple]]:
    """
    在给定超时内执行 SQL，返回 (columns, rows_as_tuples)。
    超时则抛 TimeoutError。
    """
    # 子线程的执行结果用可变容器传递（Python 闭包无法直接修改外层变量）
    result_container: dict[str, Any] = {}
    exception_container: dict[str, Exception] = {}

    def _runner():
        """子线程执行体。"""
        try:
            # execute 返回 Relation，fetchall() 拉回所有数据
            rel = con.execute(sql)
            # description 返回 [(col_name, col_type, ...), ...] 的列元数据
            columns = [desc[0] for desc in rel.description] if rel.description else []
            rows = rel.fetchall()
            result_container["columns"] = columns
            result_container["rows"] = rows
        except Exception as e:
            exception_container["error"] = e

    # 启动子线程
    # daemon=True：主线程退出时子线程自动结束，防止卡住
    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    # 超时判断
    if thread.is_alive():
        # 线程还在跑 → 超时了
        try:
            con.interrupt()  # 告诉 DuckDB 取消查询
        except Exception:
            pass  # interrupt 本身失败不致命，继续抛超时
        thread.join(timeout=2)  # 再给 2 秒让线程优雅退出
        raise TimeoutError(f"SQL execution exceeded {timeout_seconds}s timeout")

    # 子线程内部抛异常 → 重新抛到主线程
    if "error" in exception_container:
        raise exception_container["error"]

    return result_container["columns"], result_container["rows"]


# ============================================================================
# 主函数：execute_sql
#
# 这是对外暴露的唯一接口。Query Agent 通过 @tool 装饰器把它包成 LangChain tool，
# LLM 以 `execute_sql(sql="SELECT ...")` 的形式调用。
# ============================================================================
def execute_sql(
    sql: str,
    max_rows: int | None = None,
    timeout_seconds: int | None = None,
) -> QueryResult:
    """
    对 DuckDB 执行一条只读 SQL，返回结构化结果。

    Args:
        sql: SQL 字符串。必须以 SELECT 或 WITH 开头。
        max_rows: 行数上限。None 则用 settings.max_sql_rows（默认 500）。
        timeout_seconds: 超时秒数。None 则用 settings.sql_timeout（默认 30）。

    Returns:
        QueryResult 实例。检查 .success 字段判断成败。

    【为什么 max_rows / timeout 允许参数覆盖？】
      大多数场景用 settings 默认值就好，但单元测试里我们想用 100ms 超时
      快速测失败路径。参数覆盖让这种场景不用改全局 settings。
    """
    # ---- 加载配置默认值 ----
    settings = get_settings()
    if max_rows is None:
        max_rows = settings.max_sql_rows
    if timeout_seconds is None:
        timeout_seconds = settings.sql_timeout

    # ---- 去除末尾分号（包装 LIMIT 时会成 SELECT ... FROM (SELECT ...;) LIMIT 501 语法错误）----
    sql_stripped = sql.strip().rstrip(";").strip()

    # ---- 只读校验 ----
    if not _is_readonly_sql(sql_stripped):
        return QueryResult(
            sql=sql,
            success=False,
            error=(
                "只允许 SELECT 或 WITH 开头的只读查询。"
                "本工具禁止 INSERT/UPDATE/DELETE/DROP/CREATE 等写操作。"
            ),
        )

    # ---- 包一层 LIMIT ----
    # +1 的技巧：多取一行，用来判断原查询是否有更多数据
    # 如果返回 max_rows+1 行，说明原查询 ≥ max_rows+1 行，我们截到 max_rows 行并标记 truncated
    wrapped_sql = f"SELECT * FROM ({sql_stripped}) AS __inner LIMIT {max_rows + 1}"

    # ---- 计时开始 ----
    t0 = time.perf_counter()

    # ---- 打开只读连接 ----
    # read_only=True：DuckDB 层面的第二道防御。即使正则被绕过，写操作也会在这里被拒。
    # 每次调用都新开连接：避免跨线程共享连接的并发问题；DuckDB 开连接很快（<10ms）
    try:
        con = duckdb.connect(str(settings.duckdb_abs_path), read_only=True)
    except Exception as e:
        # 数据库文件不存在、被锁等基础错误
        return QueryResult(
            sql=sql,
            success=False,
            error=f"无法连接到数据库：{e}。请确认 {settings.duckdb_abs_path} 存在（跑 make setup 初始化）。",
            execution_ms=int((time.perf_counter() - t0) * 1000),
        )

    try:
        # ---- 执行 SQL ----
        columns, rows_tuples = _execute_with_timeout(con, wrapped_sql, timeout_seconds)

        # ---- 处理截断 ----
        truncated = len(rows_tuples) > max_rows
        if truncated:
            rows_tuples = rows_tuples[:max_rows]  # 只保留前 max_rows 行

        # ---- 转成 list[dict] ----
        # zip(columns, row) 生成 [(col_name, value), ...]，dict() 转成 {col_name: value}
        rows_as_dicts = [dict(zip(columns, row)) for row in rows_tuples]

        return QueryResult(
            sql=sql,
            success=True,
            columns=columns,
            rows=rows_as_dicts,
            row_count=len(rows_as_dicts),
            truncated=truncated,
            execution_ms=int((time.perf_counter() - t0) * 1000),
        )

    except Exception as e:
        return QueryResult(
            sql=sql,
            success=False,
            error=_format_error(sql, e),
            execution_ms=int((time.perf_counter() - t0) * 1000),
        )

    finally:
        # 确保连接关闭，不管成功失败
        try:
            con.close()
        except Exception:
            pass


# ============================================================================
# 开发自检：python -m insight_pilot.tools.duckdb_executor
# ============================================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        # 传了参数就把参数当 SQL 跑
        test_sql = " ".join(sys.argv[1:])
    else:
        # 默认跑一个简单查询做冒烟测试
        test_sql = "SELECT table_name FROM information_schema.tables WHERE table_schema='main' LIMIT 5"

    print(f"执行：{test_sql}\n")
    result = execute_sql(test_sql)
    print(result.to_llm_string())
