"""
scripts/setup_data.py —— 数据层一键初始化脚本

【职责】
    1. 从 Kaggle 下载 Olist 巴西电商数据集（9 个 CSV）
    2. 加载进 DuckDB，建立干净命名的表（如 `customers` 而不是 `olist_customers_dataset`）
    3. 创建便捷视图（orders_full、order_items_enriched）—— 给下游 LLM 用，减少 join

【用法】
    uv run python scripts/setup_data.py            # 幂等：已存在则跳过
    uv run python scripts/setup_data.py --force    # 强制重建（删除旧 DB）
    uv run python scripts/setup_data.py --info     # 只打印当前 DB 元数据，不做加载

【为什么写成 CLI 脚本而不是包内模块？】
    这是"一次性数据准备"任务，不属于运行时逻辑。放 scripts/ 下明确区分：
    src/ 是会被打包发布的代码，scripts/ 是开发/部署时用的工具。

【Kaggle 认证】
    kagglehub 需要认证才能下载。两种方式：
      方式 A：在 ~/.kaggle/kaggle.json 放 API token
      方式 B：.env 或环境变量设 KAGGLE_USERNAME + KAGGLE_KEY
    任一可用即可。都没有时脚本会给出明确修复指引。
"""

# ============================================================================
# 标准库导入
# ============================================================================
from __future__ import annotations   # Python 3.10+ 推迟类型注解求值，性能更好

import argparse                      # 解析 --force / --info 等 CLI 参数
import os                            # 读环境变量、检查文件
import shutil                        # 复制文件（kagglehub 下载到缓存目录，我们要搬到 data/olist/）
import sys                           # sys.exit 返回错误码给 Makefile
from pathlib import Path             # 路径操作用 Path 而非字符串拼接

# ============================================================================
# 第三方库导入
#   - duckdb：主角，数据仓库引擎
#   - kagglehub：Kaggle 官方下载库（比 kaggle CLI 更简洁）
#   - dotenv：加载 .env 里的 KAGGLE_USERNAME/KAGGLE_KEY
# ============================================================================
import duckdb
from dotenv import load_dotenv

# 延迟导入 kagglehub —— 因为它在没有 KAGGLE_* 环境时会打印警告，
# 我们希望先把 .env 里的变量加载到 os.environ 再导入，避免无谓警告
# （import 发生在 main() 里）

# ============================================================================
# 常量与配置
# ============================================================================

# 脚本文件所在目录的父目录 = 项目根
#   __file__ → scripts/setup_data.py
#   .resolve() → 绝对路径
#   .parent → scripts/
#   .parent.parent → 项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 数据目录：原始 CSV 和 DuckDB 文件的家
DATA_DIR = PROJECT_ROOT / "data"
CSV_DIR = DATA_DIR / "olist"             # 原始 CSV 落地位置
DUCKDB_PATH = DATA_DIR / "warehouse.duckdb"  # DuckDB 单文件数据库

# Kaggle 数据集标识：<owner>/<dataset-name>
# olistbr 是 Olist 巴西官方上传的
KAGGLE_DATASET = "olistbr/brazilian-ecommerce"

# ---------------------------------------------------------------------------
# 表名映射：Kaggle CSV 文件名 → DuckDB 里的简洁表名
#
# 【为什么要重命名？】
#   LLM 写 SQL 时，表名越像业务语义越好。
#   `olist_order_items_dataset` 这种名字只会让 Text-to-SQL 拧巴。
#
# 字典顺序无所谓，加载时按这个顺序处理。
# ---------------------------------------------------------------------------
CSV_TO_TABLE: dict[str, str] = {
    "olist_customers_dataset.csv": "customers",
    "olist_geolocation_dataset.csv": "geolocation",
    "olist_order_items_dataset.csv": "order_items",
    "olist_order_payments_dataset.csv": "order_payments",
    "olist_order_reviews_dataset.csv": "order_reviews",
    "olist_orders_dataset.csv": "orders",
    "olist_products_dataset.csv": "products",
    "olist_sellers_dataset.csv": "sellers",
    "product_category_name_translation.csv": "product_category_translation",
}


# ============================================================================
# 辅助函数：彩色打印
#
# 【为什么不直接用 rich？】
#   setup 脚本要尽量少依赖，早期阶段 rich 可能还没装（用户还没 uv sync）。
#   用 ANSI 转义码是零依赖方案。rich 留给运行时 CLI 用。
# ============================================================================
def _c(msg: str, color: str = "cyan") -> str:
    """返回 ANSI 彩色字符串。color ∈ {green, yellow, red, cyan, bold}"""
    codes = {
        "green": "\033[92m",
        "yellow": "\033[93m",
        "red": "\033[91m",
        "cyan": "\033[96m",
        "bold": "\033[1m",
    }
    reset = "\033[0m"
    return f"{codes.get(color, '')}{msg}{reset}"


def info(msg: str) -> None:
    """普通信息（青色）"""
    print(f"{_c('[INFO]', 'cyan')} {msg}")


def success(msg: str) -> None:
    """成功信息（绿色）"""
    print(f"{_c('[OK]', 'green')} {msg}")


def warn(msg: str) -> None:
    """警告（黄色）"""
    print(f"{_c('[WARN]', 'yellow')} {msg}")


def error(msg: str) -> None:
    """错误（红色），打到 stderr 方便脚本管道过滤"""
    print(f"{_c('[ERROR]', 'red')} {msg}", file=sys.stderr)


# ============================================================================
# 核心函数 1：校验 Kaggle 认证
#
# 【设计决策：为什么把这步独立成函数？】
#   认证失败是用户最可能遇到的错误。独立函数让我们可以：
#     - 在下载之前就检查（失败快）
#     - 给出精准的错误信息和修复步骤
#     - 未来换别的数据源时容易替换
# ============================================================================
def check_kaggle_credentials() -> bool:
    """
    检查 Kaggle 认证是否配置好。返回 True 表示可用。

    优先级：
      1. 环境变量 KAGGLE_USERNAME + KAGGLE_KEY（.env 或 shell export）
      2. ~/.kaggle/kaggle.json
    """
    # ---- 方式 A：环境变量 ----
    if os.getenv("KAGGLE_USERNAME") and os.getenv("KAGGLE_KEY"):
        info("检测到 KAGGLE_USERNAME / KAGGLE_KEY 环境变量")
        return True

    # ---- 方式 B：kaggle.json 文件 ----
    kaggle_json = Path.home() / ".kaggle" / "kaggle.json"
    if kaggle_json.exists():
        info(f"检测到 {kaggle_json}")
        return True

    # ---- 都没有：给出明确的修复指引 ----
    error("未找到 Kaggle 认证信息。")
    print()
    print(_c("请从下面两种方式任选一种配置：", "bold"))
    print()
    print(_c("  方式 A（推荐）：", "yellow"))
    print("    1. 去 https://www.kaggle.com/settings")
    print("    2. 点 'Create New Token'，下载 kaggle.json")
    print("    3. 放到 ~/.kaggle/kaggle.json")
    print("    4. chmod 600 ~/.kaggle/kaggle.json  （Kaggle 要求文件权限私密）")
    print()
    print(_c("  方式 B：", "yellow"))
    print("    在项目的 .env 里加入：")
    print("      KAGGLE_USERNAME=你的用户名")
    print("      KAGGLE_KEY=你的 API key")
    print()
    return False


# ============================================================================
# 核心函数 2：下载并把 CSV 搬到项目内
#
# 【为什么要搬？kagglehub 下载到 ~/.cache/kagglehub/ 不是也能读吗？】
#   能读，但不便于：
#     - 版本锁定（别人 clone 后需要自己重下）
#     - notebook demo 展示（看得见 data/olist/*.csv 更直观）
#     - 离线演示（面试时网络可能抽风）
#   所以我们把 CSV 复制到 data/olist/ 入项目管控（但 .gitignore 排除）。
# ============================================================================
def download_dataset() -> Path:
    """
    从 Kaggle 下载 Olist 数据集，复制到 data/olist/，返回 CSV 目录路径。
    如果 data/olist/ 已有全部 9 个 CSV，跳过下载。
    """
    # 如果 data/olist/ 已有全部 9 个文件，跳过下载（幂等）
    existing = [f for f in CSV_TO_TABLE if (CSV_DIR / f).exists()]
    if len(existing) == len(CSV_TO_TABLE):
        info(f"data/olist/ 已包含全部 {len(CSV_TO_TABLE)} 个 CSV，跳过下载")
        return CSV_DIR

    # 这里才 import kagglehub —— 前面讲过，避免 .env 还没加载时的误警告
    import kagglehub  # noqa: PLC0415  ruff 会警告 import 放顶部，但这里是故意的

    info(f"从 Kaggle 下载数据集：{KAGGLE_DATASET} ...")
    # kagglehub.dataset_download 返回的是解压后的本地目录
    # 下载缓存在 ~/.cache/kagglehub/datasets/olistbr/brazilian-ecommerce/versions/N/
    cache_dir = Path(kagglehub.dataset_download(KAGGLE_DATASET))
    success(f"下载完成，缓存位置：{cache_dir}")

    # 把 9 个 CSV 复制到项目内 data/olist/
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    for csv_name in CSV_TO_TABLE:
        src = cache_dir / csv_name
        dst = CSV_DIR / csv_name
        if not src.exists():
            # 可能 Kaggle 数据集结构变化（非常偶发），给出明确错误而非 FileNotFoundError
            error(f"Kaggle 数据集里没找到 {csv_name}，请检查数据集版本")
            sys.exit(1)
        shutil.copy2(src, dst)   # copy2 保留元数据（修改时间等）
    success(f"已把 {len(CSV_TO_TABLE)} 个 CSV 复制到 {CSV_DIR}")
    return CSV_DIR


# ============================================================================
# 核心函数 3：把 CSV 加载到 DuckDB
#
# 【关键 SQL：CREATE TABLE ... AS SELECT * FROM read_csv_auto(...)】
#   这是 DuckDB 最强的一个特性：不用写 schema，自动推断列类型。
#   对比传统做法：pandas.read_csv → to_sql，DuckDB 原生方案快 10 倍 +。
#
# 【SAMPLE_SIZE 参数】
#   read_csv_auto 默认扫前 20000 行推断类型。对 Olist 足够（最大表 ~110 万行）。
#   如果类型推错了（比如日期被当成字符串），可以用 types={...} 手工指定。
# ============================================================================
def load_csv_to_duckdb(csv_dir: Path, db_path: Path) -> None:
    """把 data/olist/ 下的 CSV 全部加载成 DuckDB 表。"""
    # duckdb.connect 打开或创建单文件 DB；传 str 不传 Path（老版本 duckdb 兼容性）
    con = duckdb.connect(str(db_path))

    try:
        for csv_name, table_name in CSV_TO_TABLE.items():
            csv_path = csv_dir / csv_name
            info(f"  加载 {csv_name} → 表 {table_name}")

            # CREATE OR REPLACE：让 --force 重建时覆盖旧表
            # read_csv_auto：DuckDB 的自动类型推断函数
            #   header=true：第一行是列名
            #   sample_size=-1：扫全文件推断类型（Olist 不大，准确率优先于速度）
            sql = f"""
                CREATE OR REPLACE TABLE {table_name} AS
                SELECT * FROM read_csv_auto(
                    '{csv_path}',
                    header=true,
                    sample_size=-1
                )
            """
            con.execute(sql)

            # 打印行数做校验，方便一眼看出加载是否完整
            row_count = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            success(f"    {table_name}: {row_count:,} 行")

    finally:
        # 不管成功失败都要关连接，防止 .duckdb 文件被锁
        con.close()


# ============================================================================
# 核心函数 4：创建便捷视图
#
# 【设计哲学】
#   视图不是为了"炫技"，是为了**降低 LLM 写 SQL 出错的概率**。
#   我们把最常见的 3-4 表 join 预先定义成视图，LLM 写 SELECT 就够了。
#
#   但不要过度抽象：
#     - orders_full：常用（订单 + 客户 + 付款）→ 值得
#     - order_items_enriched：常用（商品明细 + 产品 + 卖家 + 类别英文名）→ 值得
#     - 别的（如评论、地理）LLM 手写 join 也不会错太多 → 不做
# ============================================================================
def create_views(db_path: Path) -> None:
    """创建便捷视图给 LLM 用。"""
    con = duckdb.connect(str(db_path))
    try:
        info("创建便捷视图...")

        # ---- 视图 1：orders_full —— 订单宽表 ----
        # 包含：订单状态/时间线 + 客户所在州 + 支付方式/金额
        # 这是 Olist 80% 订单分析场景的起点
        con.execute("""
            CREATE OR REPLACE VIEW orders_full AS
            SELECT
                o.order_id,
                o.customer_id,
                o.order_status,
                o.order_purchase_timestamp,
                o.order_approved_at,
                o.order_delivered_carrier_date,
                o.order_delivered_customer_date,
                o.order_estimated_delivery_date,
                -- 客户维度
                c.customer_unique_id,
                c.customer_city,
                c.customer_state,
                c.customer_zip_code_prefix,
                -- 付款聚合（一个订单可能有多笔付款，先聚合）
                p.payment_total,
                p.payment_types,
                p.num_installments
            FROM orders o
            LEFT JOIN customers c USING (customer_id)
            LEFT JOIN (
                -- 子查询：把同一订单的多条付款记录聚合成一行
                -- string_agg：把多个 payment_type 拼成 'credit_card,boleto'
                SELECT
                    order_id,
                    SUM(payment_value)                            AS payment_total,
                    string_agg(DISTINCT payment_type, ',')        AS payment_types,
                    MAX(payment_installments)                     AS num_installments
                FROM order_payments
                GROUP BY order_id
            ) p USING (order_id)
        """)
        success("  orders_full （订单 + 客户 + 支付汇总）")

        # ---- 视图 2：order_items_enriched —— 商品明细宽表 ----
        # 包含：订单项 + 商品信息 + 卖家位置 + 类别英文名
        # 分析"品类"和"卖家集中度"场景的起点
        con.execute("""
            CREATE OR REPLACE VIEW order_items_enriched AS
            SELECT
                oi.order_id,
                oi.order_item_id,
                oi.product_id,
                oi.seller_id,
                oi.shipping_limit_date,
                oi.price,
                oi.freight_value,
                -- 产品维度
                p.product_category_name          AS category_pt,  -- 葡语原名
                t.product_category_name_english  AS category_en,  -- 英文译名（LLM 友好）
                p.product_weight_g,
                p.product_length_cm,
                p.product_height_cm,
                p.product_width_cm,
                -- 卖家维度
                s.seller_city,
                s.seller_state
            FROM order_items oi
            LEFT JOIN products p USING (product_id)
            LEFT JOIN product_category_translation t USING (product_category_name)
            LEFT JOIN sellers s USING (seller_id)
        """)
        success("  order_items_enriched （订单项 + 产品 + 卖家 + 类别英文名）")

    finally:
        con.close()


# ============================================================================
# 核心函数 5：打印数据仓库概览
#
# 【用途】
#   脚本最后打印所有表/视图的行数和样例列，让用户一眼看到"我的数据仓库里有啥"。
#   面试演示时这个输出很有说服力 —— "我不是空口说有数据，你看日志"。
# ============================================================================
def print_warehouse_summary(db_path: Path) -> None:
    """打印数据仓库内所有表/视图的概要信息。"""
    con = duckdb.connect(str(db_path))
    try:
        print()
        print(_c("=" * 72, "bold"))
        print(_c(f"  数据仓库概览：{db_path}", "bold"))
        print(_c("=" * 72, "bold"))

        # information_schema.tables 是 SQL 标准的元数据表
        # table_type: 'BASE TABLE' 表示物理表，'VIEW' 表示视图
        objects = con.execute("""
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = 'main'
            ORDER BY table_type DESC, table_name
        """).fetchall()

        for table_name, table_type in objects:
            row_count = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            # [T] 表示 Table，[V] 表示 View
            tag = "T" if table_type == "BASE TABLE" else "V"
            print(f"  [{tag}] {table_name:<35} {row_count:>12,} 行")

        print(_c("=" * 72, "bold"))
    finally:
        con.close()


# ============================================================================
# CLI 入口
# ============================================================================
def main() -> int:
    """
    CLI 主入口。返回 exit code：0 成功，非 0 失败。
    返回 int 让 Makefile / CI 可以根据 $? 判断是否通过。
    """
    # ---- 解析命令行参数 ----
    parser = argparse.ArgumentParser(
        description="初始化 InsightPilot 数据仓库：下载 Olist → 构建 DuckDB",
        # RawDescriptionHelpFormatter 让 help 的换行保留
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重建 DuckDB（删除旧文件）。默认如果 DB 已存在则跳过加载。",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="只打印当前 DB 概览，不做下载/加载。",
    )
    args = parser.parse_args()

    # ---- 加载 .env，让 KAGGLE_USERNAME / KAGGLE_KEY 进 os.environ ----
    load_dotenv(PROJECT_ROOT / ".env")

    # ---- 分支 1：仅查询模式 ----
    if args.info:
        if not DUCKDB_PATH.exists():
            warn(f"DB 文件不存在：{DUCKDB_PATH}")
            info("先跑 `make setup` 或 `python scripts/setup_data.py` 构建数据仓库。")
            return 1
        print_warehouse_summary(DUCKDB_PATH)
        return 0

    # ---- 分支 2：幂等检查 ----
    # 已存在且没加 --force → 认为 setup 已完成，只打印概览
    if DUCKDB_PATH.exists() and not args.force:
        info(f"{DUCKDB_PATH} 已存在。用 --force 可强制重建。")
        print_warehouse_summary(DUCKDB_PATH)
        return 0

    # ---- 分支 3：完整 setup 流程 ----
    # 如果 --force 且 DB 存在，删掉重建
    if args.force and DUCKDB_PATH.exists():
        warn(f"--force：删除旧 DB {DUCKDB_PATH}")
        DUCKDB_PATH.unlink()

    # 1. 认证检查（失败就直接退出，不做后面的昂贵操作）
    if not check_kaggle_credentials():
        return 1

    # 2. 下载（或复用已有 CSV）
    try:
        csv_dir = download_dataset()
    except Exception as e:
        error(f"下载失败：{e}")
        return 1

    # 3. 加载到 DuckDB
    DATA_DIR.mkdir(exist_ok=True)
    info(f"加载 CSV 到 DuckDB：{DUCKDB_PATH}")
    try:
        load_csv_to_duckdb(csv_dir, DUCKDB_PATH)
    except Exception as e:
        error(f"CSV 加载失败：{e}")
        return 1

    # 4. 建视图
    try:
        create_views(DUCKDB_PATH)
    except Exception as e:
        error(f"视图创建失败：{e}")
        return 1

    # 5. 打印概览
    print_warehouse_summary(DUCKDB_PATH)
    success("数据仓库准备完成！")
    return 0


# ============================================================================
# Python 惯用法：if __name__ == "__main__"
#
# 让这个文件既能作为脚本执行（python scripts/setup_data.py），
# 也能作为模块被 import（未来可能在测试里调用 create_views 做清理）。
# ============================================================================
if __name__ == "__main__":
    sys.exit(main())
