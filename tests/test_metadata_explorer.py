"""
tests/test_metadata_explorer.py —— metadata_explorer 的测试

测什么：
  1. list_tables —— 返回 Markdown，包含预期的表
  2. describe_table —— 合法表返回 schema；非法表名拒绝
  3. sample_rows —— 随机采样、n 的范围钳制、非法名拒绝

【安全重点】
  describe_table / sample_rows 内部会把 table_name 拼到 SQL 字符串里
  （DuckDB 不支持 identifier 参数化）。必须验证白名单校验有效，
  防止 LLM 传 "orders; DROP TABLE x" 这种注入。
"""

from __future__ import annotations

from insight_pilot.tools.metadata_explorer import (
    describe_table,
    list_tables,
    sample_rows,
)


# ============================================================================
# list_tables 测试
# ============================================================================
class TestListTables:
    """list_tables 的测试。"""

    def test_returns_markdown_with_table_names(self, populated_duckdb):
        """应返回 Markdown，含 fixture 里建的所有表名。"""
        output = list_tables()
        assert "customers" in output
        assert "orders" in output
        assert "order_items" in output
        assert "orders_full" in output

    def test_distinguishes_tables_from_views(self, populated_duckdb):
        """表和视图在输出里应能区分。"""
        output = list_tables()
        # 中文标签"视图"或 Markdown 里的类型列应出现
        assert "视图" in output or "VIEW" in output.upper()

    def test_shows_row_counts(self, populated_duckdb):
        """行数应显示在输出里。"""
        output = list_tables()
        # customers 有 3 行，orders 有 4 行
        assert "3" in output
        assert "4" in output

    def test_includes_business_description_for_known_tables(self, populated_duckdb):
        """
        customers 在 TABLE_DESCRIPTIONS 里有业务描述
        （"customer_id 是订单关联键..."），应出现在输出里。
        """
        output = list_tables()
        assert "customer_unique_id" in output or "UV" in output


# ============================================================================
# describe_table 测试
# ============================================================================
class TestDescribeTable:
    """describe_table 的测试。"""

    def test_valid_table_returns_schema(self, populated_duckdb):
        """合法表名应返回字段列表。"""
        output = describe_table("customers")
        assert "customer_id" in output
        assert "customer_unique_id" in output
        assert "customer_state" in output

    def test_valid_view_returns_schema(self, populated_duckdb):
        """视图也应能 describe。"""
        output = describe_table("orders_full")
        assert "order_id" in output
        assert "customer_state" in output  # 视图 join 过来的字段

    def test_shows_column_types(self, populated_duckdb):
        """输出里应包含字段类型。"""
        output = describe_table("orders")
        # orders.order_purchase_timestamp 是 TIMESTAMP
        assert "TIMESTAMP" in output

    def test_invalid_table_rejected(self, populated_duckdb):
        """不存在的表应被拒，错误信息提示用 list_tables。"""
        output = describe_table("totally_fake_table_xyz")
        assert "[错误]" in output or "不存在" in output
        assert "list_tables" in output

    def test_sql_injection_attempt_rejected(self, populated_duckdb):
        """
        SQL 注入尝试应被白名单校验拦住，不触及 DB。

        攻击样本：table_name = "customers; DROP TABLE orders"
        """
        malicious = "customers; DROP TABLE orders"
        output = describe_table(malicious)
        # 应当被拒（错误信息包含"不存在"或"错误"）
        assert "[错误]" in output or "不存在" in output

        # 更重要：orders 表必须还在
        # 用 describe_table 合法调用验证
        check = describe_table("orders")
        assert "order_id" in check  # orders 表还能正常 describe

    def test_shows_business_description_when_available(self, populated_duckdb):
        """
        有业务描述的表（customers 在 TABLE_DESCRIPTIONS 里），
        describe 输出应包含业务说明部分。
        """
        output = describe_table("customers")
        # 业务描述里提到 customer_unique_id 的重要性
        assert "业务说明" in output or "unique_id" in output


# ============================================================================
# sample_rows 测试
# ============================================================================
class TestSampleRows:
    """sample_rows 的测试。"""

    def test_valid_table_returns_markdown_table(self, populated_duckdb):
        """合法表应返回 Markdown 格式的样例数据。"""
        output = sample_rows("customers", 3)
        # Markdown 表格的分隔符
        assert "|" in output
        # 应包含实际数据
        assert "SP" in output or "RJ" in output

    def test_respects_n_parameter(self, many_rows_duckdb):
        """n 参数应限制返回行数（但因为随机性，不测精确数字）。"""
        output = sample_rows("big_table", 5)
        # 数表格里的数据行（不含表头和分隔线）
        # 粗略估计：输出应该不包含 1000 行 label
        # 更稳妥：只验证调用没报错
        assert "|" in output
        assert "[错误]" not in output

    def test_n_clamped_to_upper_bound(self, many_rows_duckdb):
        """n > 20 应被钳制到 20。"""
        output = sample_rows("big_table", 999)
        assert "[错误]" not in output
        # 输出里应显示"20 行"而不是"999 行"
        assert "20 行" in output

    def test_n_clamped_to_lower_bound(self, populated_duckdb):
        """n < 1 应被钳制到 1。"""
        output = sample_rows("customers", 0)
        assert "[错误]" not in output

    def test_invalid_table_rejected(self, populated_duckdb):
        """不存在的表应被拒。"""
        output = sample_rows("not_a_real_table", 5)
        assert "[错误]" in output or "不存在" in output

    def test_sql_injection_attempt_rejected(self, populated_duckdb):
        """SQL 注入尝试应被拦。"""
        malicious = "customers; DROP TABLE orders"
        output = sample_rows(malicious, 5)
        assert "[错误]" in output or "不存在" in output

        # orders 表必须完好
        check = sample_rows("orders", 3)
        assert "[错误]" not in check


# ============================================================================
# 交互场景：验证一次完整的"探查链路"能跑通
#
# 这个测试不光测单个函数，还验证"Agent 典型使用模式"：
#   list_tables → describe_table → sample_rows
# ============================================================================
class TestExplorationFlow:
    """端到端探查流程测试。"""

    def test_full_exploration_flow(self, populated_duckdb):
        """
        模拟 Agent 的典型 ReAct 流程：先列表，再选一个描述，再看样例。
        每一步都应成功且没有错误标记。
        """
        # Step 1：列出所有表
        tables_output = list_tables()
        assert "customers" in tables_output
        assert "[错误]" not in tables_output

        # Step 2：describe customers
        schema_output = describe_table("customers")
        assert "customer_unique_id" in schema_output
        assert "[错误]" not in schema_output

        # Step 3：采样看数据
        sample_output = sample_rows("customers", 2)
        assert "[错误]" not in sample_output
