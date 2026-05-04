# 核心业务指标定义

本文档定义 Olist 电商分析中常用的 KPI 指标，含口径、计算 SQL 和坑点。
每条术语 LLM 在写 SQL / 分析时需要严格遵守这些定义。

---

## GMV（Gross Merchandise Volume，商品交易总额）

**定义**：所有订单的商品销售总额，**不扣除退货、运费、税费**。

**口径**：
- 在 Olist 数据集中，最接近 GMV 的字段是 `orders_full.payment_total`
- 但 `payment_total` 包含运费，如果要"纯商品额"需要从 `order_items.price` 累加（不含 freight_value）
- 默认场景用 `payment_total` 简化

**SQL 模板**：
```sql
SELECT SUM(payment_total) AS gmv
FROM orders_full
WHERE order_status IN ('delivered', 'shipped')
  AND order_purchase_timestamp BETWEEN '2017-01-01' AND '2017-12-31';
```

**坑点**：
- 不要用 `order_items.price * quantity` —— Olist 的 order_items 一行就是一个商品（没有 quantity 字段）
- 一定要过滤 order_status，不然会把 canceled 也算进去虚高

---

## 营收（Revenue）

**定义**：通常等同于 GMV。在 Olist 语境下，"月度营收"、"年营收"指的都是 `payment_total` 之和。

**口径**：
- **必须过滤 order_status** —— 默认 `IN ('delivered', 'shipped')`
- 时间字段用 `order_purchase_timestamp`（下单时间），不要用 `order_delivered_customer_date`（到货时间）

**SQL 模板（月度营收）**：
```sql
SELECT
    DATE_TRUNC('month', order_purchase_timestamp) AS month,
    SUM(payment_total) AS revenue
FROM orders_full
WHERE order_status IN ('delivered', 'shipped')
GROUP BY month
ORDER BY month;
```

---

## UV（Unique Visitors，独立访客数）/ 用户数

**定义**：去重后的真实用户数。

**口径（关键！）**：
- **必须用 `customer_unique_id`，不是 `customer_id`**
- `customer_id` 是订单级标识 —— 一个人多次下单会有多个 customer_id
- `customer_unique_id` 才是真实用户的唯一标识

**SQL 模板**：
```sql
SELECT COUNT(DISTINCT customer_unique_id) AS unique_users
FROM orders_full
WHERE order_purchase_timestamp BETWEEN '2017-01-01' AND '2017-12-31';
```

**坑点**：用 `customer_id` 去重会高估用户数（大约 1.05x-1.1x），月活/年活要严格用 unique_id。

---

## 客单价（AOV, Average Order Value）

**定义**：平均每个订单的金额。

**口径**：
- 分子：订单总额
- 分母：**订单数**（不是用户数）
- 要不要过滤 canceled？业务上看：客单价分析关注成交质量，**应该过滤**

**SQL 模板**：
```sql
SELECT
    SUM(payment_total) / COUNT(DISTINCT order_id) AS aov
FROM orders_full
WHERE order_status IN ('delivered', 'shipped');
```

**变体**：
- "用户平均消费" = SUM / COUNT(DISTINCT customer_unique_id)
- 这两个不一样，注意区分

---

## 复购率 / 复购客户占比

**定义**：在某时间段内，下过 ≥2 单的用户占总用户的比例。

**口径**：
- 分子：COUNT(DISTINCT customer_unique_id WHERE order_count >= 2)
- 分母：COUNT(DISTINCT customer_unique_id)
- 需要先按 customer_unique_id 聚合

**SQL 模板**：
```sql
WITH user_orders AS (
    SELECT
        customer_unique_id,
        COUNT(*) AS order_count
    FROM orders_full
    WHERE order_status IN ('delivered', 'shipped')
    GROUP BY customer_unique_id
)
SELECT
    SUM(CASE WHEN order_count >= 2 THEN 1 ELSE 0 END) * 1.0
    / COUNT(*) AS repeat_purchase_rate
FROM user_orders;
```

---

## 配送时效（Delivery Time）

**定义**：从下单到收货的实际天数。

**口径**：
- 起点：`order_purchase_timestamp`
- 终点：`order_delivered_customer_date`
- 仅看 `order_status = 'delivered'`（其他状态没收货时间）

**SQL 模板**：
```sql
SELECT
    AVG(EXTRACT(EPOCH FROM (order_delivered_customer_date - order_purchase_timestamp)) / 86400) AS avg_days
FROM orders_full
WHERE order_status = 'delivered'
  AND order_delivered_customer_date IS NOT NULL;
```

---

## 配送延迟（Delivery Delay）

**定义**：实际收货日期 vs 预计交付日期的差值（正值表示晚到）。

**口径**：
- delay_days = (order_delivered_customer_date - order_estimated_delivery_date) / 1 day
- > 0：晚于预期；<= 0：按时或提前
- 仅在 `order_status = 'delivered'` 时有意义

**SQL 模板**：
```sql
SELECT
    customer_state,
    AVG(EXTRACT(EPOCH FROM (order_delivered_customer_date - order_estimated_delivery_date)) / 86400) AS avg_delay_days
FROM orders_full
WHERE order_status = 'delivered'
GROUP BY customer_state
ORDER BY avg_delay_days DESC;
```

---

## NPS / 评分（Rating Score）

**定义**：用户对订单的评分，1-5 星。

**口径**：
- 字段：`order_reviews.review_score`（INT 1-5）
- 一个订单可能没评分（不强制）→ 计算时排除 NULL
- 平均分 < 4.0 通常被认为体验有问题

**SQL 模板**：
```sql
SELECT
    AVG(r.review_score) AS avg_rating,
    COUNT(*) AS num_reviews
FROM order_reviews r
JOIN orders_full o USING (order_id)
WHERE o.order_status = 'delivered';
```

---

## Top N 品类（Top Categories）

**定义**：按某指标（通常是营收）排序的前 N 个品类。

**口径**：
- 视图：`order_items_enriched`（已 join 商品和品类）
- 品类用**英文名** `category_en`（不是葡语 `category_pt`）—— 给用户展示更友好
- 默认按"商品销售额"排序：SUM(price)

**SQL 模板（Top 5 营收品类）**：
```sql
SELECT
    category_en,
    SUM(price) AS revenue,
    COUNT(DISTINCT order_id) AS num_orders
FROM order_items_enriched
WHERE category_en IS NOT NULL
GROUP BY category_en
ORDER BY revenue DESC
LIMIT 5;
```

**坑点**：`category_en` 可能是 NULL（没翻译的品类），加 `IS NOT NULL` 过滤。

---

## 卖家集中度（Seller Concentration）

**定义**：某品类下，前 N 个卖家占总销售额的比例。常用 Top 10 卖家占比作为集中度指标。

**口径**：
- 高集中度（>60%）：少数卖家垄断，新进难
- 低集中度（<30%）：长尾市场，竞争激烈

**SQL 模板**：
```sql
WITH seller_revenue AS (
    SELECT
        category_en,
        seller_id,
        SUM(price) AS rev
    FROM order_items_enriched
    GROUP BY category_en, seller_id
),
ranked AS (
    SELECT
        category_en,
        seller_id,
        rev,
        ROW_NUMBER() OVER (PARTITION BY category_en ORDER BY rev DESC) AS rn,
        SUM(rev) OVER (PARTITION BY category_en) AS category_total
    FROM seller_revenue
)
SELECT
    category_en,
    SUM(CASE WHEN rn <= 10 THEN rev ELSE 0 END) / category_total AS top10_concentration
FROM ranked
GROUP BY category_en, category_total;
```

---

## 支付方式分布

**定义**：各种支付方式（credit_card / boleto / voucher / debit_card）的订单数和金额占比。

**口径**：
- `payment_type` 在 `order_payments` 表，**不在 orders_full**
- 一个订单可能有多种支付方式（信用卡 + 优惠券）
- 简化分析时取 max 或第一种

**SQL 模板**：
```sql
SELECT
    payment_type,
    COUNT(DISTINCT order_id) AS num_orders,
    SUM(payment_value) AS total_value
FROM order_payments
GROUP BY payment_type
ORDER BY num_orders DESC;
```

**注意**：`payment_type = 'credit_card'` 不等于"分期付款"。分期数在 `payment_installments` 字段（INT，1 = 全款，>1 = 分期）。
