# 常见分析模式

本文档收录 Olist 分析中最常出现的几种"问题 → 解法"模式。
LLM 在拆解 plan 或写代码时可以参考。

---

## 模式 1：时间趋势分析

**场景**：2017 月度营收、季度订单量、年度增长

**Plan 模板**：
1. (query) 按时间粒度聚合（DATE_TRUNC + GROUP BY）
2. (analysis) 画折线图

**Python 代码模板**：
```python
df = get_df(0)
df["month"] = pd.to_datetime(df["month"])

plt.figure(figsize=(10, 6))
plt.plot(df["month"], df["revenue"], marker="o")
plt.title("Monthly Revenue 2017")
plt.xlabel("Month")
plt.ylabel("Revenue (BRL)")
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(f"chart_{step_id}_trend.png")
plt.close()
```

---

## 模式 2：地域对比

**场景**：各州订单数、配送时效、评分分布

**Plan 模板**：
1. (query) 按 state GROUP BY，取多个指标
2. (analysis) 画柱状图或地图

**Python 代码模板**：
```python
df = get_df(0)
df_sorted = df.sort_values("order_count", ascending=False).head(10)  # 取 Top 10

plt.figure(figsize=(12, 6))
plt.bar(df_sorted["state"], df_sorted["order_count"])
plt.title("Top 10 States by Order Count")
plt.xlabel("State")
plt.ylabel("Number of Orders")
plt.tight_layout()
plt.savefig(f"chart_{step_id}_states.png")
plt.close()
```

---

## 模式 3：相关性分析

**场景**：配送延迟和评分的关系、价格和销量的关系

**Plan 模板**：
1. (query) 取两个指标到同一行（per state / per order）
2. (analysis) 算相关系数 + 画散点图

**Python 代码模板**：
```python
df = get_df(0)
print(df.head())

# 算皮尔森相关系数
corr = df["delay_days"].corr(df["avg_rating"])
print(f"相关系数: {corr:.3f}")

# 画散点图
plt.figure(figsize=(10, 6))
plt.scatter(df["delay_days"], df["avg_rating"], alpha=0.6)
plt.title(f"Delay vs Rating (corr={corr:.2f})")
plt.xlabel("Avg Delay (days)")
plt.ylabel("Avg Rating")
plt.grid(alpha=0.3)
plt.tight_layout()
plt.savefig(f"chart_{step_id}_correlation.png")
plt.close()
```

**坑点**：
- |相关系数| > 0.5 才算"明显相关"
- 相关 ≠ 因果，结论里不能写"延迟导致评分降低"，只能写"两者负相关"

---

## 模式 4：Top N 排名

**场景**：Top 5 品类、Top 10 卖家、Top 20 高评分商品

**Plan 模板**：
1. (query) GROUP BY + ORDER BY + LIMIT N
2. (analysis) 画水平柱状图（更适合排名）

**Python 代码模板**：
```python
df = get_df(0).head(10)

plt.figure(figsize=(10, 6))
plt.barh(df["category_en"], df["revenue"])  # barh = 水平柱状图
plt.title("Top 10 Categories by Revenue")
plt.xlabel("Revenue (BRL)")
plt.gca().invert_yaxis()  # 第一名在上面
plt.tight_layout()
plt.savefig(f"chart_{step_id}_top10.png")
plt.close()
```

---

## 模式 5：分布分析

**场景**：订单金额分布、评分分布、配送时间分布

**Plan 模板**：
1. (query) 取明细级数据（不聚合）
2. (analysis) 画直方图 + 算分位数

**Python 代码模板**：
```python
df = get_df(0)

# 算关键分位数
print("分位数:")
print(df["order_value"].describe(percentiles=[0.25, 0.5, 0.75, 0.9, 0.95]))

# 画直方图
plt.figure(figsize=(10, 6))
plt.hist(df["order_value"], bins=50, edgecolor="black", alpha=0.7)
plt.axvline(df["order_value"].median(), color="red", linestyle="--", label=f"Median: {df['order_value'].median():.0f}")
plt.title("Order Value Distribution")
plt.xlabel("Order Value (BRL)")
plt.ylabel("Frequency")
plt.legend()
plt.tight_layout()
plt.savefig(f"chart_{step_id}_distribution.png")
plt.close()
```

---

## 模式 6：多维对比（双 Y 轴）

**场景**：营收和评分一起看、订单量和客单价一起看

**为什么用双 Y 轴？** 两个指标量级差距大（营收百万 vs 评分 1-5），用同一坐标会让其中一个"压扁"。

**Python 代码模板**：
```python
import numpy as np
df = get_df(0).head(5)

fig, ax1 = plt.subplots(figsize=(12, 6))

# 左 Y 轴：营收（柱状）
ax1.bar(df["category_en"], df["revenue"], alpha=0.7, color="steelblue", label="Revenue")
ax1.set_xlabel("Category")
ax1.set_ylabel("Revenue (BRL)", color="steelblue")
ax1.tick_params(axis="y", labelcolor="steelblue")

# 右 Y 轴：评分（折线）
ax2 = ax1.twinx()
ax2.plot(df["category_en"], df["avg_rating"], marker="o", color="darkred", linewidth=2, label="Rating")
ax2.set_ylabel("Avg Rating", color="darkred")
ax2.tick_params(axis="y", labelcolor="darkred")
ax2.set_ylim(0, 5)

plt.title("Top 5 Categories: Revenue vs Rating")
plt.xticks(rotation=30, ha="right")
plt.tight_layout()
plt.savefig(f"chart_{step_id}_dual_axis.png")
plt.close()
```

---

## 模式 7：业务建议综合

**场景**："给出投资建议"、"哪个最值得"

**Plan 模板**：
1. (query) 取关键指标
2. (query) 取风险信号
3. (analysis) 综合打分 / 排序
4. (analysis) 用 print 输出建议

**打分模板**：
```python
df_revenue = get_df(0)  # category_en, revenue
df_quality = get_df(1)  # category_en, avg_rating, top10_concentration

# 合并
df = df_revenue.merge(df_quality, on="category_en")

# 标准化各维度（0-1）
df["score_revenue"] = df["revenue"] / df["revenue"].max()
df["score_rating"] = (df["avg_rating"] - 3) / 2  # 评分 3-5 标准化到 0-1
df["score_competition"] = 1 - df["top10_concentration"]  # 集中度低 = 好

# 综合分（可调权重）
df["overall_score"] = (
    df["score_revenue"] * 0.5
    + df["score_rating"] * 0.3
    + df["score_competition"] * 0.2
)

df_sorted = df.sort_values("overall_score", ascending=False)

# 输出建议
print("===== 投资建议 =====")
for _, row in df_sorted.iterrows():
    rec = "✓ 优先" if row["overall_score"] > 0.7 else ("⚠️ 观察" if row["overall_score"] > 0.5 else "✗ 慎入")
    print(f"{row['category_en']}: 评分 {row['overall_score']:.2f} → {rec}")
```

---

## 通用编码原则

1. **先 print(df.head()) 确认数据形态**，再写后续逻辑
2. **每张图独立 plt.figure() + plt.close()**，避免叠加
3. **图标题用英文**，中文 matplotlib 默认会乱码
4. **关键数字 print 出来**，让 Reporter 能引用
5. **DataFrame 操作前后打印 shape**，方便定位 reshape bug
