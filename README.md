# InsightPilot

> LangGraph-based multi-agent data analysis system — interview portfolio piece.

自然语言业务问题 → 自动规划 → SQL 取数 → Python 分析 → Markdown 报告（带图表）。对敏感结论触发人机协同审批。

## 快速开始

```bash
# 1. 装依赖（uv + Python 3.11+）
make install

# 2. 配环境变量
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY

# 3. 准备数据（下载 Olist → 构建 DuckDB）
make setup

# 4. 跑演示
make demo
```

## 架构

5 个 Agent：Planner / Query / Analysis / Reporter / Reviewer，由 LangGraph StateGraph 编排。详见 `docs/architecture.md`（WIP）。

## 项目状态

🚧 开发中 —— 第一阶段（数据层 + 工具）
