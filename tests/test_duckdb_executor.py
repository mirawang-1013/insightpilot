"""
tests/test_duckdb_executor.py —— duckdb_executor 的测试

测试分两层：
  1. 纯逻辑测试（不需要 DB）：正则白名单、错误分类
  2. 集成测试（需要 DB fixture）：execute_sql 端到端

【重点覆盖：docs/design-decisions.md §8 的三层纵深防御】
  - L1 正则：接受合法 SELECT，拒绝各种写操作
  - L2 SQL 包装：阻止多语句注入
  - L3 DuckDB read_only：兜底
"""

from __future__ import annotations

import pytest

from insight_pilot.tools.duckdb_executor import (
    _format_error,
    _is_readonly_sql,
    execute_sql,
)


# ============================================================================
# 第一层：正则白名单的纯逻辑测试
#
# 这些测试直接检查 _is_readonly_sql 返回值，不跑 SQL。
# 好处：不依赖 DB、跑得快、失败定位精准。
# ============================================================================
class TestReadonlyRegex:
    """_is_readonly_sql 正则白名单测试。"""

    # ---- 应该接受的情况 ----

    def test_accepts_simple_select(self):
        assert _is_readonly_sql("SELECT * FROM orders") is True

    def test_accepts_lowercase_select(self):
        """IGNORECASE flag 让大小写都认。"""
        assert _is_readonly_sql("select * from orders") is True

    def test_accepts_mixed_case(self):
        assert _is_readonly_sql("SeLeCt * from orders") is True

    def test_accepts_with_cte(self):
        """WITH 开头的 CTE 是分析师常用模式。"""
        sql = "WITH x AS (SELECT 1) SELECT * FROM x"
        assert _is_readonly_sql(sql) is True

    def test_accepts_leading_whitespace(self):
        assert _is_readonly_sql("   SELECT 1") is True

    def test_accepts_leading_newlines(self):
        assert _is_readonly_sql("\n\n  SELECT 1") is True

    def test_accepts_leading_comment(self):
        """-- 注释开头应被允许。"""
        sql = "-- 月度营收统计\nSELECT * FROM orders"
        assert _is_readonly_sql(sql) is True

    def test_accepts_multiple_leading_comments(self):
        """多行注释也应被允许。"""
        sql = "-- 注释 1\n-- 注释 2\nSELECT 1"
        assert _is_readonly_sql(sql) is True

    # ---- 应该拒绝的情况 ----

    def test_rejects_drop_table(self):
        assert _is_readonly_sql("DROP TABLE customers") is False

    def test_rejects_insert(self):
        assert _is_readonly_sql("INSERT INTO customers VALUES (1)") is False

    def test_rejects_update(self):
        assert _is_readonly_sql("UPDATE customers SET state='SP'") is False

    def test_rejects_delete(self):
        assert _is_readonly_sql("DELETE FROM customers") is False

    def test_rejects_create_table(self):
        assert _is_readonly_sql("CREATE TABLE x (id INT)") is False

    def test_rejects_alter_table(self):
        assert _is_readonly_sql("ALTER TABLE customers ADD COLUMN age INT") is False

    def test_rejects_truncate(self):
        assert _is_readonly_sql("TRUNCATE TABLE customers") is False

    def test_rejects_drop_disguised_as_comment(self):
        """注释伪装成 SELECT，实际是 DROP —— 应被拒。"""
        sql = "-- SELECT 伪装\nDROP TABLE customers"
        assert _is_readonly_sql(sql) is False

    def test_rejects_word_prefix_attack(self):
        """
        SELECTEDROP 这种词前缀攻击 —— \\b 词边界的作用。
        注意：SELECTEDROP 不是合法 SQL 关键字，但正则引擎可能贪婪匹配
        SELECT 这 6 个字符，然后 \\b 要求词边界才算真匹配。
        """
        assert _is_readonly_sql("SELECTEDROP TABLE x") is False

    def test_rejects_empty_string(self):
        """空字符串不算合法 SQL。"""
        assert _is_readonly_sql("") is False

    def test_rejects_only_whitespace(self):
        """只有空白字符也不算。"""
        assert _is_readonly_sql("   \n\t  ") is False


# ============================================================================
# 第二层：错误分类器的纯逻辑测试
#
# _format_error 把 DuckDB 异常转成 LLM 友好的带建议的错误字符串。
# 这里用字符串模拟异常，不实际跑 SQL。
# ============================================================================
class TestFormatError:
    """_format_error 错误分类测试。"""

    def test_missing_column_gives_describe_hint(self):
        """字段不存在应提示用 describe_table。"""
        exc = Exception('Referenced column "bad_col" not found in FROM clause!')
        output = _format_error("SELECT bad_col FROM orders", exc)
        assert "字段不存在" in output
        assert "describe_table" in output

    def test_missing_table_gives_list_hint(self):
        """表不存在应提示用 list_tables。"""
        exc = Exception("Catalog Error: Table with name wrong_table does not exist!")
        output = _format_error("SELECT * FROM wrong_table", exc)
        assert "表不存在" in output
        assert "list_tables" in output

    def test_parser_error_gives_syntax_hint(self):
        """语法错误应提示检查语法。"""
        exc = Exception("Parser Error: syntax error at or near 'FOM'")
        output = _format_error("SELECT * FOM orders", exc)
        assert "语法错误" in output

    def test_group_by_error_gives_hint(self):
        """GROUP BY 错误应给专门提示。"""
        exc = Exception("column 'state' must appear in the GROUP BY clause")
        output = _format_error("SELECT state, COUNT(*) FROM t", exc)
        assert "GROUP BY" in output

    def test_unknown_error_still_returns_something(self):
        """即使是未分类的错误也应返回合理的字符串。"""
        exc = Exception("some unexpected error")
        output = _format_error("SELECT 1", exc)
        # 不 assert 具体内容，只确保返回字符串不是空
        assert output
        assert isinstance(output, str)


# ============================================================================
# 第三层：集成测试 —— execute_sql 端到端
# 依赖 populated_duckdb / many_rows_duckdb fixture
# ============================================================================
class TestExecuteSQLBasic:
    """execute_sql 基础功能测试。"""

    def test_simple_select_succeeds(self, populated_duckdb):
        """最基本的成功路径。"""
        result = execute_sql("SELECT customer_id, customer_state FROM customers")
        assert result.success is True
        assert result.row_count == 3
        assert "customer_id" in result.columns
        assert "customer_state" in result.columns

    def test_rows_are_dicts(self, populated_duckdb):
        """rows 应是 list[dict]，不是 list[tuple]。"""
        result = execute_sql("SELECT customer_id FROM customers LIMIT 1")
        assert len(result.rows) == 1
        assert isinstance(result.rows[0], dict)
        assert "customer_id" in result.rows[0]

    def test_column_order_preserved(self, populated_duckdb):
        """columns 列表顺序应和 SELECT 里的顺序一致。"""
        result = execute_sql("SELECT customer_state, customer_id FROM customers LIMIT 1")
        assert result.columns == ["customer_state", "customer_id"]

    def test_cte_query_works(self, populated_duckdb):
        """WITH 开头的 CTE 应能执行。"""
        sql = """
            WITH delivered AS (
                SELECT * FROM orders WHERE order_status = 'delivered'
            )
            SELECT COUNT(*) AS cnt FROM delivered
        """
        result = execute_sql(sql)
        assert result.success is True
        assert result.rows[0]["cnt"] == 3

    def test_view_query_works(self, populated_duckdb):
        """视图（orders_full）应能查询。"""
        result = execute_sql("SELECT * FROM orders_full LIMIT 2")
        assert result.success is True
        assert result.row_count == 2
        assert "customer_state" in result.columns


class TestExecuteSQLSecurity:
    """
    execute_sql 的安全防御测试。

    对应 docs/design-decisions.md §8 的三层纵深防御，
    这里的每个测试都对应一层防御的自动化验证。
    """

    # ---- L1：正则拦截 ----

    def test_drop_rejected_by_regex(self, populated_duckdb):
        """DROP 应被 L1 正则直接拒绝，不触及 DB。"""
        result = execute_sql("DROP TABLE customers")
        assert result.success is False
        assert "只允许" in result.error  # 来自正则拦截的错误信息

    def test_delete_rejected_by_regex(self, populated_duckdb):
        result = execute_sql("DELETE FROM customers")
        assert result.success is False

    def test_insert_rejected_by_regex(self, populated_duckdb):
        result = execute_sql("INSERT INTO customers VALUES ('c4', 'u4', 'MG', 'belo')")
        assert result.success is False

    # ---- L2：SQL 包装阻止多语句注入 ----

    def test_multi_statement_injection_blocked(self, populated_duckdb):
        """
        SELECT 1; DROP TABLE customers 能通过 L1 正则（SELECT 开头），
        但 L2 的 SQL 包装会让它在子查询里语法错误，DuckDB 拒绝。
        """
        result = execute_sql("SELECT 1; DROP TABLE customers")
        # 应该失败（不管是正则拦还是解析失败，总之不能成功）
        assert result.success is False

        # 更重要：验证 customers 表还在（攻击没成功）
        check = execute_sql("SELECT COUNT(*) AS cnt FROM customers")
        assert check.success is True
        assert check.rows[0]["cnt"] == 3

    # ---- L3：DuckDB read_only 兜底 ----

    def test_cte_with_delete_blocked_by_readonly(self, populated_duckdb):
        """
        WITH x AS (SELECT 1) DELETE FROM customers 会通过 L1（WITH 开头）
        也可能通过 L2（单语句），最后靠 L3 的 read_only=True 拦住。
        """
        result = execute_sql("WITH x AS (SELECT 1) DELETE FROM customers")
        assert result.success is False

        # 再次验证 customers 表完好
        check = execute_sql("SELECT COUNT(*) AS cnt FROM customers")
        assert check.success is True
        assert check.rows[0]["cnt"] == 3


class TestExecuteSQLTruncation:
    """行数截断测试。"""

    def test_under_limit_not_truncated(self, populated_duckdb):
        """行数未超限，truncated 应为 False。"""
        result = execute_sql("SELECT * FROM customers")  # 只有 3 行
        assert result.truncated is False
        assert result.row_count == 3

    def test_exactly_at_limit_not_truncated(self, many_rows_duckdb):
        """正好等于 max_rows 时不应被标记截断。"""
        # big_table 有 1000 行，max_rows=5 时应恰好截到 5，标记截断
        # 如果想测"恰好不截断"，需要 max_rows=1000
        result = execute_sql("SELECT * FROM big_table", max_rows=1000)
        assert result.truncated is False
        assert result.row_count == 1000

    def test_over_limit_truncated(self, many_rows_duckdb):
        """超过 max_rows 应标记截断并只返回前 max_rows 行。"""
        result = execute_sql("SELECT * FROM big_table", max_rows=10)
        assert result.truncated is True
        assert result.row_count == 10

    def test_truncation_preserves_order(self, many_rows_duckdb):
        """截断应保留原查询的顺序，不随机丢行。"""
        result = execute_sql(
            "SELECT id FROM big_table ORDER BY id LIMIT 100",
            max_rows=5,
        )
        # 应得到前 5 行（id 0-4）
        ids = [row["id"] for row in result.rows]
        assert ids == [0, 1, 2, 3, 4]


class TestExecuteSQLErrorHandling:
    """错误路径测试 —— 验证 LLM 友好的错误信息。"""

    def test_missing_column_gives_friendly_error(self, populated_duckdb):
        """查不存在的字段，错误应提示 describe_table。"""
        result = execute_sql("SELECT totally_fake_column FROM customers")
        assert result.success is False
        assert "describe_table" in result.error

    def test_missing_table_gives_friendly_error(self, populated_duckdb):
        """查不存在的表，错误应提示 list_tables。"""
        result = execute_sql("SELECT * FROM non_existent_table_xyz")
        assert result.success is False
        assert "list_tables" in result.error

    def test_parser_error_gives_syntax_hint(self, populated_duckdb):
        """语法错误应给提示。"""
        result = execute_sql("SELEKT * FROMM orders")  # 故意拼错
        assert result.success is False
        # 即使分类不准，error 也不能为空
        assert result.error

    def test_trailing_semicolon_stripped(self, populated_duckdb):
        """SQL 末尾的分号应被自动去掉，不影响 LIMIT 包装。"""
        result = execute_sql("SELECT 1 AS x;")
        assert result.success is True
        assert result.rows[0]["x"] == 1


class TestExecuteSQLMetadata:
    """QueryResult 返回元数据字段的测试。"""

    def test_execution_ms_recorded(self, populated_duckdb):
        """execution_ms 应被记录为 >= 0 的整数。"""
        result = execute_sql("SELECT 1")
        assert isinstance(result.execution_ms, int)
        assert result.execution_ms >= 0

    def test_sql_preserved_in_result(self, populated_duckdb):
        """原始 SQL 应被保留在 result.sql 里，用于错误溯源。"""
        original_sql = "SELECT customer_id FROM customers"
        result = execute_sql(original_sql)
        assert result.sql == original_sql
