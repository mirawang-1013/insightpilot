"""
tools/metadata_explorer.py —— 元数据探查工具（Query Agent 的"眼睛"）

【职责】
    给 LLM 提供三个探查工具，让它能在写 SQL 前先"看清"数据：
      1. list_tables()       —— 这个数据仓库里有哪些表/视图？
      2. describe_table(name) —— 这张表的字段是什么？
      3. sample_rows(name, n) —— 这张表的数据实际长什么样？

【为什么独立成一个文件而不是塞进 duckdb_executor？】
    职责不同：executor 跑业务 SQL，explorer 查元数据。
    输出格式不同：executor 返回 QueryResult dataclass，explorer 返回 Markdown 字符串。
    被调用频率不同：Agent 一般探查 3-4 次才执行 1 次 SQL，拆开更清晰。

【核心设计：输出为 LLM 优化】
    所有函数都返回 str（Markdown 格式），不是 dict/list。
    原因：LangChain 工具返回值会被字符串化塞进 LLM message，
         我们直接控制渲染能省 token 且提高理解准确率。
"""

from __future__ import annotations

import duckdb

from insight_pilot.config import get_settings


# ============================================================================
# 业务知识字典：TABLE_DESCRIPTIONS
#
# 【这是 metadata_explorer 最关键的部分 —— 但它不写代码逻辑，只写业务知识】
#
# DuckDB 没有 `COMMENT ON TABLE` 那样的原生注释，所以表的"业务含义"必须手工
# 维护在这里。这些描述是 Agent 做 table 选择时的关键线索。
#
# 描述原则：
#   1. 一句话说清业务语义（"订单行级明细" vs "订单头"）
#   2. 标注 join key（LLM 写 join 时能直接用）
#   3. 点出常见坑点（Olist 最臭名昭著的 customer_id vs unique_id 问题）
# ============================================================================
TABLE_DESCRIPTIONS: dict[str, str] = {
    # ---- 原始维度表 ----
    "customers": (
        "客户维度表。"
        "注意：customer_id 是订单关联键（一个人多次下单会有多个 customer_id）；"
        "customer_unique_id 才是真实用户标识，统计 UV 要用这个。"
    ),
    "geolocation": (
        "地理位置表：zip_code_prefix → 经纬度/城市/州。"
        "注意：一个 zip_code 可能对应多行（不同精度）。"
    ),
    "order_items": (
        "订单行级明细。一个订单有多行（每个商品一行）。"
        "Join: order_id (→ orders)、product_id (→ products)、seller_id (→ sellers)。"
    ),
    "order_payments": (
        "订单支付记录。一个订单可能有多条支付（如分期 + 优惠券）。"
        "Join: order_id (→ orders)。"
        "统计订单金额前需要先 GROUP BY order_id 聚合。"
    ),
    "order_reviews": (
        "订单评论和评分（1-5 星）。Join: order_id (→ orders)。"
    ),
    "orders": (
        "订单头（order header），一个订单一行。"
        "含订单状态（delivered/shipped/canceled 等）、下单时间、发货时间、到货时间。"
        "Join: customer_id (→ customers)。"
    ),
    "products": (
        "产品维度表。字段 product_category_name 是葡语原名，"
        "需要 join product_category_translation 获取英文类别。"
    ),
    "sellers": (
        "卖家维度表。含卖家所在城市和州。Join: seller_id (→ order_items)。"
    ),
    "product_category_translation": (
        "品类名葡语→英文映射表。Join: product_category_name (→ products)。"
    ),

    # ---- 预 join 视图（推荐 LLM 优先使用）----
    "orders_full": (
        "【推荐】订单宽表视图：orders + customers + payments 预 join。"
        "大多数'订单 / 营收 / 地域'类分析首选这张表，省去手动 join。"
        "含：订单状态、购买/发货时间线、客户所在州、支付总额、支付方式、分期数。"
    ),
    "order_items_enriched": (
        "【推荐】订单项宽表视图：order_items + products + sellers + 类别英文名。"
        "做品类分析、卖家集中度分析时首选。"
        "含：商品价格、运费、产品尺寸重量、卖家位置、品类英文名。"
    ),
}


# ============================================================================
# 辅助函数：获取只读连接
#
# 【为什么不和 duckdb_executor 共用一个连接？】
#   每次查询独立开连接：
#     - 线程安全（LangGraph 可能并发调用）
#     - 零状态污染
#     - 开销 <10ms 可忽略
#   如果未来性能敏感，再提取 connection pool 作为共享模块。
# ============================================================================
def _connect() -> duckdb.DuckDBPyConnection:
    """打开只读连接。失败时抛异常让上层 catch。"""
    settings = get_settings()
    return duckdb.connect(str(settings.duckdb_abs_path), read_only=True)


# ============================================================================
# 辅助函数：校验表名是否存在
#
# 【为什么要校验？】
#   后面的 describe_table / sample_rows 需要在 SQL 里拼接表名
#   （DuckDB 的 parameterized query 不支持 identifier 绑定）。
#   如果不校验就直接拼，LLM 传 `orders; DROP TABLE x` 就能注入。
#
#   校验策略：先从 information_schema 拉所有合法表名，只允许传入值
#   精确出现在列表里才继续。
# ============================================================================
def _get_all_table_names() -> set[str]:
    """从 information_schema 拉取所有合法表/视图名（main schema）。"""
    con = _connect()
    try:
        rows = con.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
        """).fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


# ============================================================================
# 工具 1：list_tables
#
# 返回所有表/视图的列表，带行数和业务描述。
# 这是 Agent ReAct 循环的第一步通常会调用的工具。
# ============================================================================
def list_tables() -> str:
    """
    列出数据仓库中所有表和视图，含行数和业务描述。

    Returns:
        Markdown 格式的字符串，LLM 直接读。
    """
    try:
        con = _connect()
    except Exception as e:
        return f"[错误] 无法连接数据库：{e}"

    try:
        # information_schema.tables 是 SQL 标准元数据表
        # table_type 区分：'BASE TABLE' = 物理表，'VIEW' = 视图
        # 按类型倒序：VIEW 在前（视图是推荐入口），然后按表名字母排
        objects = con.execute("""
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = 'main'
            ORDER BY
                CASE table_type WHEN 'VIEW' THEN 0 ELSE 1 END,
                table_name
        """).fetchall()

        if not objects:
            return (
                "[空] 数据仓库里没有表。"
                "请先跑 `python scripts/setup_data.py` 初始化数据。"
            )

        # ---- 构造 Markdown 输出 ----
        lines = ["# 数据仓库表列表\n"]
        lines.append("| 名称 | 类型 | 行数 | 业务说明 |")
        lines.append("|------|------|------|----------|")

        for table_name, table_type in objects:
            # 查行数。COUNT(*) 在 DuckDB 列存上很快（不用扫全表，有元数据）
            row_count = con.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0]

            # 类型标签本地化
            type_label = "视图" if table_type == "VIEW" else "表"

            # 从业务字典查描述；没定义的表给一个温和的提示
            description = TABLE_DESCRIPTIONS.get(
                table_name,
                "（无业务描述 —— 如需了解请用 describe_table 查字段）",
            )

            lines.append(
                f"| `{table_name}` | {type_label} | {row_count:,} | {description} |"
            )

        # 追加一条使用提示，帮 LLM 决定下一步
        lines.append(
            "\n**提示**：推荐优先使用视图（`orders_full`、`order_items_enriched`），"
            "它们预 join 好了最常用的维度，能省去大量手写 join。"
        )

        return "\n".join(lines)

    except Exception as e:
        return f"[错误] 列表查询失败：{e}"
    finally:
        con.close()


# ============================================================================
# 工具 2：describe_table
#
# 返回指定表的字段列表（字段名 + 类型 + 可空性）。
# ============================================================================
def describe_table(table_name: str) -> str:
    """
    查看指定表的字段 schema。

    Args:
        table_name: 表或视图名。必须是 list_tables 返回过的名字之一。

    Returns:
        Markdown 格式的字段表。
    """
    # ---- 校验表名：防 SQL 注入 ----
    # 详细解释见 _get_all_table_names 的文档。
    valid_tables = _get_all_table_names()
    if table_name not in valid_tables:
        return (
            f"[错误] 表不存在：`{table_name}`\n"
            f"修复建议：用 list_tables 工具查可用表名。"
            f"\n\n可用的表（前 10 个）：{sorted(valid_tables)[:10]}"
        )

    try:
        con = _connect()
    except Exception as e:
        return f"[错误] 无法连接数据库：{e}"

    try:
        # information_schema.columns 是字段级元数据
        # ordinal_position 决定列顺序（按 CREATE TABLE 的定义顺序）
        columns = con.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'main' AND table_name = ?
            ORDER BY ordinal_position
            """,
            [table_name],  # 用参数化，这里是 value 不是 identifier，安全
        ).fetchall()

        if not columns:
            return f"[错误] 表 `{table_name}` 没有字段（这不应该发生）。"

        # ---- 构造 Markdown 输出 ----
        lines = [f"# 表 `{table_name}` 的 Schema\n"]

        # 顶部加业务描述（如果有）
        if table_name in TABLE_DESCRIPTIONS:
            lines.append(f"**业务说明**：{TABLE_DESCRIPTIONS[table_name]}\n")

        lines.append("| 字段 | 类型 | 可空 |")
        lines.append("|------|------|------|")

        for col_name, col_type, is_nullable in columns:
            nullable_mark = "是" if is_nullable == "YES" else "否"
            lines.append(f"| `{col_name}` | `{col_type}` | {nullable_mark} |")

        # 追加提示：下一步可以做什么
        lines.append(
            f"\n**提示**：用 `sample_rows('{table_name}')` 看前几行实际数据，"
            f"能帮你判断字段的值分布和格式。"
        )

        return "\n".join(lines)

    except Exception as e:
        return f"[错误] 查询 schema 失败：{e}"
    finally:
        con.close()


# ============================================================================
# 工具 3：sample_rows
#
# 返回指定表的若干行样例数据，帮 LLM 理解"数据实际长什么样"。
#
# 【为什么用 USING SAMPLE 而不是 LIMIT？】
#   LIMIT 5 拿到的往往是最早插入的几行，容易是历史遗留 / 边界数据。
#   USING SAMPLE 5 ROWS 是随机采样，更能代表数据的整体分布形态。
# ============================================================================
def sample_rows(table_name: str, n: int = 5) -> str:
    """
    返回指定表的 N 行随机样例。

    Args:
        table_name: 表或视图名。
        n: 采样行数，默认 5。上限 20（防止 LLM 要 100 行把 context 撑爆）。

    Returns:
        Markdown 格式的样例数据表。
    """
    # ---- 校验 n 的范围 ----
    # 防止 LLM 传一个奇葩值（比如 10000）。
    if n < 1:
        n = 1
    if n > 20:
        n = 20

    # ---- 校验表名：防 SQL 注入 ----
    valid_tables = _get_all_table_names()
    if table_name not in valid_tables:
        return (
            f"[错误] 表不存在：`{table_name}`\n"
            f"修复建议：用 list_tables 工具查可用表名。"
        )

    try:
        con = _connect()
    except Exception as e:
        return f"[错误] 无法连接数据库：{e}"

    try:
        # USING SAMPLE N ROWS：DuckDB 原生随机采样
        # 对视图也有效（会下推到底层表的采样）
        # 注意：table_name 这里是拼到 SQL 里的，上面已经做过白名单校验
        sql = f"SELECT * FROM {table_name} USING SAMPLE {n} ROWS"
        result = con.execute(sql)

        # description 返回列元数据，这里只要列名
        columns = [desc[0] for desc in result.description]
        rows = result.fetchall()

        if not rows:
            return f"[空] 表 `{table_name}` 里没有数据。"

        # ---- 构造 Markdown 输出 ----
        lines = [f"# 表 `{table_name}` 的样例数据（随机 {len(rows)} 行）\n"]

        # 表头
        lines.append("| " + " | ".join(f"`{c}`" for c in columns) + " |")
        lines.append("|" + "|".join("-" * (len(c) + 2) for c in columns) + "|")

        # 数据行
        for row in rows:
            # 格式化每个值：None → 'NULL'，字符串太长截断，保留 dict/list 的 repr
            formatted = []
            for val in row:
                if val is None:
                    formatted.append("NULL")
                else:
                    s = str(val)
                    # 截断长字符串，防止一行占满屏幕
                    if len(s) > 40:
                        s = s[:37] + "..."
                    # 把 | 转义，否则破坏 Markdown 表格
                    s = s.replace("|", "\\|")
                    formatted.append(s)
            lines.append("| " + " | ".join(formatted) + " |")

        return "\n".join(lines)

    except Exception as e:
        return f"[错误] 采样失败：{e}"
    finally:
        con.close()


# ============================================================================
# 开发自检入口
#
# 用法：
#   python -m insight_pilot.tools.metadata_explorer                    # 跑全部三个工具
#   python -m insight_pilot.tools.metadata_explorer list               # 只跑 list_tables
#   python -m insight_pilot.tools.metadata_explorer describe orders    # 查 orders 表
#   python -m insight_pilot.tools.metadata_explorer sample orders 3    # 采样 orders 表 3 行
# ============================================================================
if __name__ == "__main__":
    import sys

    args = sys.argv[1:]

    if not args:
        # 默认：展示三个工具的输出样本，验证一切正常
        print("=" * 72)
        print("list_tables()")
        print("=" * 72)
        print(list_tables())
        print()
        print("=" * 72)
        print("describe_table('orders_full')")
        print("=" * 72)
        print(describe_table("orders_full"))
        print()
        print("=" * 72)
        print("sample_rows('orders_full', 3)")
        print("=" * 72)
        print(sample_rows("orders_full", 3))

    elif args[0] == "list":
        print(list_tables())

    elif args[0] == "describe" and len(args) >= 2:
        print(describe_table(args[1]))

    elif args[0] == "sample" and len(args) >= 2:
        n = int(args[2]) if len(args) >= 3 else 5
        print(sample_rows(args[1], n))

    else:
        print("用法：")
        print("  python -m insight_pilot.tools.metadata_explorer")
        print("  python -m insight_pilot.tools.metadata_explorer list")
        print("  python -m insight_pilot.tools.metadata_explorer describe <table>")
        print("  python -m insight_pilot.tools.metadata_explorer sample <table> [n]")
        sys.exit(1)
