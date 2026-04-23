"""
tests/conftest.py —— pytest 共享 fixture

【conftest.py 是什么？】
    pytest 的约定：放在测试目录里的 conftest.py 会被该目录（及子目录）下
    所有测试文件自动加载，**不需要 import**。
    这是 pytest 官方推荐的"共享 fixture 集中管理"做法。

【本文件提供的 fixture】
    - test_settings     覆盖全局配置，指向临时 DuckDB 路径
    - populated_duckdb  在临时路径构造一个带数据的测试 DB

【设计原则】
    1. 不 mock DuckDB —— 真实行为最可靠（见 docs/design-decisions.md）
    2. 临时数据在 tmp_path，pytest 自动清理
    3. 测试 schema 够小，但保留 Olist 的关键关系（orders + customers + 视图）
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest


# ============================================================================
# Fixture 1：test_settings
#
# 作用：
#   - 覆盖 OPENAI_API_KEY（随便设一个值，测试不调 OpenAI）
#   - 覆盖 DUCKDB_PATH 指向临时目录
#   - 清 get_settings 的 lru_cache 让新环境生效
# ============================================================================
@pytest.fixture
def test_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    提供一个指向临时 DuckDB 的 Settings 实例。

    【monkeypatch 的魔法】
      pytest 内置的 fixture，修改环境变量 / sys.path / 对象属性后
      会在 fixture 结束时自动还原，不污染其他测试。

    【tmp_path 的魔法】
      pytest 每个测试函数都会拿到一个独占的临时目录，
      测试结束后目录自动删除。并发测试也安全（每个测试独占）。
    """
    # 设置环境变量 —— 这些会被 pydantic-settings 读取
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("DUCKDB_PATH", str(tmp_path / "test.duckdb"))

    # 清 get_settings 的 lru_cache，强制它重新读取环境变量
    # 不清的话，如果其他测试先跑过 get_settings()，这里拿到的是旧缓存
    from insight_pilot.config import get_settings
    get_settings.cache_clear()

    # yield 之前：setup；yield 之后：teardown
    # 用 yield 而不是 return，让 fixture 能在测试结束后执行清理
    yield get_settings()

    # teardown：测试完再清一次，防止影响下一个测试
    get_settings.cache_clear()


# ============================================================================
# Fixture 2：populated_duckdb
#
# 在 test_settings 的基础上，往临时 DuckDB 里塞一套"迷你 Olist"测试数据：
#   - customers（3 行）
#   - orders（4 行）
#   - order_items（5 行）
#   - orders_full（视图，预 join）
#
# 【为什么只塞这点数据？】
#   测试不是在做压力测试，关键是覆盖"数据存在"的路径。
#   少量数据让每个测试毫秒级跑完，CI 压力小。
# ============================================================================
@pytest.fixture
def populated_duckdb(test_settings):
    """
    在临时路径构造带数据的 DuckDB，返回 test_settings。

    【为什么 yield test_settings 而不是 duckdb 连接？】
      测试代码应该通过正式的工具函数（execute_sql、list_tables）访问 DB，
      不直接操作连接。这样测试也在验证"工具函数读 settings 配置的路径正常"。
    """
    # 用 read_only=False 打开写连接来建表
    # 注意：连接必须在插数据后关闭，否则 execute_sql 的 read_only 连接打不开
    con = duckdb.connect(str(test_settings.duckdb_abs_path), read_only=False)
    try:
        # ---- 建客户表 ----
        con.execute("""
            CREATE TABLE customers (
                customer_id VARCHAR,
                customer_unique_id VARCHAR,
                customer_state VARCHAR,
                customer_city VARCHAR
            )
        """)
        con.execute("""
            INSERT INTO customers VALUES
                ('c1', 'u1', 'SP', 'sao paulo'),
                ('c2', 'u2', 'RJ', 'rio de janeiro'),
                ('c3', 'u1', 'SP', 'campinas')  -- u1 下过两单，测 unique_id 去重用
        """)

        # ---- 建订单表 ----
        con.execute("""
            CREATE TABLE orders (
                order_id VARCHAR,
                customer_id VARCHAR,
                order_status VARCHAR,
                order_purchase_timestamp TIMESTAMP
            )
        """)
        con.execute("""
            INSERT INTO orders VALUES
                ('o1', 'c1', 'delivered', '2017-01-15 10:00:00'),
                ('o2', 'c2', 'delivered', '2017-02-20 14:30:00'),
                ('o3', 'c1', 'canceled',  '2017-03-05 09:15:00'),
                ('o4', 'c3', 'delivered', '2017-04-10 16:45:00')
        """)

        # ---- 建订单项表 ----
        con.execute("""
            CREATE TABLE order_items (
                order_id VARCHAR,
                order_item_id INT,
                product_id VARCHAR,
                price DOUBLE
            )
        """)
        con.execute("""
            INSERT INTO order_items VALUES
                ('o1', 1, 'p1', 99.50),
                ('o1', 2, 'p2', 20.00),
                ('o2', 1, 'p1', 99.50),
                ('o3', 1, 'p3', 150.00),
                ('o4', 1, 'p2', 20.00)
        """)

        # ---- 建宽表视图（模拟真实项目的 orders_full）----
        con.execute("""
            CREATE VIEW orders_full AS
            SELECT
                o.order_id,
                o.customer_id,
                o.order_status,
                o.order_purchase_timestamp,
                c.customer_unique_id,
                c.customer_state,
                c.customer_city
            FROM orders o
            LEFT JOIN customers c USING (customer_id)
        """)
    finally:
        con.close()

    return test_settings


# ============================================================================
# Fixture 3：many_rows_duckdb
#
# 专门用来测"行数截断"逻辑：塞一张 1000 行的表，
# 验证 execute_sql 的 max_rows 参数正确工作。
# ============================================================================
@pytest.fixture
def many_rows_duckdb(test_settings):
    """塞 1000 行数据的 DB，用来测截断。"""
    con = duckdb.connect(str(test_settings.duckdb_abs_path), read_only=False)
    try:
        # DuckDB 的 range() 是个便捷函数，生成 0..999 的整数序列
        con.execute("""
            CREATE TABLE big_table AS
            SELECT i AS id, 'row_' || i AS label
            FROM range(1000) AS t(i)
        """)
    finally:
        con.close()
    return test_settings
