# InsightPilot —— 设计决策笔记

> 这份笔记记录计划文档之外的深层技术选型。每个决策都对应面试常见追问，
> 按"问题 → 表层答案 → 深层答案 → 面试金句"的结构组织。
>
> 最后更新：2026-04-23

---

## 目录

1. [为什么选 LangGraph，而不是 CrewAI / AutoGen？](#1-为什么选-langgraph而不是-crewai--autogen)
2. [DuckDB "模拟" Athena 究竟差在哪？](#2-duckdb-模拟-athena-究竟差在哪)
3. [为什么不直接用 LangChain 的 SQLDatabaseAgent？](#3-为什么不直接用-langchain-的-sqldatabaseagent)
4. [State 设计：为什么一个大 AgentState，而不是每个 Agent 自己的 state？](#4-state-设计为什么一个大-agentstate而不是每个-agent-自己的-state)
5. [Subprocess 沙盒 vs Docker / E2B —— 安全性妥协](#5-subprocess-沙盒-vs-docker--e2b--安全性妥协)
6. [ReAct 循环 vs 纯结构化输出 —— Query Agent 的选型](#6-react-循环-vs-纯结构化输出--query-agent-的选型)
7. [配置复杂度：InsightPilot 150 行 vs GraphRAG 15 行](#7-配置复杂度insightpilot-150-行-vs-graphrag-15-行)
8. [SQL 安全：三层纵深防御怎么设计的](#8-sql-安全三层纵深防御怎么设计的)
9. [数据治理与元数据管理 —— 从字典到数据目录](#9-数据治理与元数据管理--从字典到数据目录)

---

## 1. 为什么选 LangGraph，而不是 CrewAI / AutoGen？

### 表层答案
显式图控制、与面试逐字稿一致、技术含量高。

### 深层答案：三类框架的抽象粒度不同

| 框架 | 抽象层 | 控制流 | 适合场景 |
|---|---|---|---|
| **LangChain AgentExecutor** | 单 Agent + 工具 | ReAct 一个 while 循环 | 简单工具调用 |
| **CrewAI** | 多 Agent + "角色扮演" | Agent 之间靠 LLM 自己协商 | Demo / 营销玩具 |
| **AutoGen** | 多 Agent + "对话群聊" | 靠 GroupChatManager 路由消息 | 研究性原型 |
| **LangGraph** | **显式状态机** | 节点 + 边 + 条件路由 | 生产级、可调试、可测试 |

### 关键洞察
CrewAI / AutoGen 的控制流是**隐式的** —— "让 LLM 决定下一个谁发言"。这在 demo 里很酷，但生产环境会出问题：

- 同样的输入跑 10 次，路径可能不同 → 无法回归测试
- 出错时不知道在哪一步断的 → 无法调试
- 想加"敏感结论要人工审批"节点？架构要大改

LangGraph 的 StateGraph 本质是**把 LLM 决策关在状态机里**。每次转移都由一个**可测试的函数**决定（conditional edge function）。这就是为什么 Anthropic 自己的 Claude agents 也走这条路。

### 面试金句
> "LangGraph 和直接写 while 循环的区别在两处：**Checkpointer**（状态可持久化 + 断点续传）和 **interrupt()**（人机协同原生支持）。手写 while 循环要自己实现这两个，工作量不小。"

---

## 2. DuckDB "模拟" Athena 究竟差在哪？

### 表层答案
都是分析型 SQL 引擎。

### 深层答案：差异必须讲清楚

| 维度 | DuckDB | AWS Athena |
|---|---|---|
| **计算模式** | 本地单机（进程内） | Serverless（Presto/Trino 集群） |
| **存储** | 本地文件 / 内存 | S3（Parquet/ORC/CSV） |
| **并发查询** | 单进程单查询 | 多用户并发 |
| **成本模型** | 零成本 | 按扫描数据量计费（$5/TB） |
| **SQL 方言** | PostgreSQL-like + DuckDB 扩展 | Presto/Trino 方言（ANSI-ish） |
| **元数据** | 自己的 catalog | AWS Glue Catalog |

### 本项目中要警惕的方言差异

| 语法 | DuckDB | Athena（Presto） |
|---|---|---|
| 字符串聚合 | `string_agg(x, ',')` | `array_join(array_agg(x), ',')` |
| 日期截断 | `DATE_TRUNC('month', ts)` | 同上 ✅ 兼容 |
| 数组/结构 | `LIST` / `STRUCT` | `ARRAY` / `ROW` |
| Join 简写 | `USING (col)` | 同上 ✅ 兼容 |
| 自动类型推断 CSV | `read_csv_auto(...)` | 建 external table 时手写 schema |

### 面试金句
> "我选 DuckDB 是因为它提供了 Athena 80% 的 SQL 语法且零成本本地开发。生产上线时真正要做的是：
> ① 把 CSV 加载改成从 S3 读 Parquet，
> ② 替换 `string_agg` 这类方言差异，
> ③ 把 `duckdb.connect(...)` 换成 `pyathena` 或 SQLAlchemy Athena driver。
> **Agent 业务逻辑不用改。**"

这就是**可移植性**的专业回答 —— 展示你懂"抽象边界"。

---

## 3. 为什么不直接用 LangChain 的 SQLDatabaseAgent？

### 表层答案
定制空间不够。

### 深层答案：prebuilt Agent 的三个硬伤

1. **SQL 执行和结果返回绑死**
   我们需要把 SQL 结果**存进 State** 供后续 Analysis Agent 消费，而不是当场返回给用户。
   LangChain 的 SQLDatabaseAgent 直接把结果塞进 LLM 对话，无法导出。

2. **元数据探查无法分步骤**
   真实生产场景，LLM 要先 `list_tables` → `describe_table` → 再写 SQL，这是三次独立工具调用。
   LangChain 把它们捆成一个黑盒 —— 你看不见、改不了中间步骤。

3. **Prompt 无法从业务视角约束**
   没法注入"客户所在州用 `customer_state` 字段"这种业务规则。
   Text-to-SQL 的准确率，**60% 靠 prompt 里的业务知识**。

### 我们的做法
用 `create_react_agent`（LangGraph prebuilt，更底层）+ 自定义工具：

```
工具清单：
  - list_tables()          列出所有表/视图
  - describe_table(name)   查字段名 + 类型
  - sample_rows(name, n)   看前 n 行数据
  - execute_sql(sql)       执行 SELECT（只读、500 行上限）
```

Prompt 里注入 Olist 业务知识 + DuckDB 方言约束。

### 面试金句
> "LangChain SQL Agent 是 notebook demo 级别的抽象。生产环境需要工具级别的粒度控制 —— 什么时候让 LLM 探查元数据、什么时候让它写 SQL、结果怎么存进下游可消费的格式。所以我用 `create_react_agent` + 自定义工具链。"

---

## 4. State 设计：为什么一个大 AgentState，而不是每个 Agent 自己的 state？

### 这是 LangGraph 最核心的设计决策，面试官 100% 会问。

### 两种风格对比

```python
# 风格 A：一个大 State（我们采用的）
class AgentState(TypedDict):
    user_query: str
    execution_plan: list[ExecutionStep]
    query_results: Annotated[list[QueryResult], operator.add]
    analysis_results: Annotated[list[AnalysisResult], operator.add]
    report_markdown: str
    # ... 全部字段在一起

# 风格 B：每个 Agent 自己的 State（Subgraph 模式）
class QueryAgentState(TypedDict):
    sql: str
    rows: list[dict]

class AnalysisAgentState(TypedDict):
    code: str
    chart_path: str
```

### 怎么选？

| 选 A 当... | 选 B 当... |
|---|---|
| Agent 之间数据依赖强 | Agent 独立性强 |
| 整体作为一个系统交付 | 每个 Agent 想打包成可复用组件 |
| **本项目场景** | "SQL Agent"作为 NPM 包卖给别人用 |

### 我们选 A 的真正理由

数据流是**顺流**的：

```
user_query
   ↓
Planner 读 user_query → 写 execution_plan
   ↓
Query Agent 读 execution_plan[i] → 写 query_results[i]
   ↓
Analysis Agent 读 query_results → 写 analysis_results[i]
   ↓
Reporter 读全部 → 写 report_markdown
   ↓
Reviewer 读 report_markdown → 判断是否 interrupt
```

一个大 State 就是**共享内存黑板（Blackboard Pattern）**，简洁直接。

### 关键细节：`Annotated[list[X], operator.add]` 的机制

- **没写 reducer** → 默认覆盖语义：`new_value = returned_value`（循环里每 step 都覆盖前一步的结果 → BUG）
- **写了 `operator.add`** → 列表合并：`new_value = old_value + returned_value`（正确累积）

踩坑警示：`operator.or_` 对 dict 是 `|=` 合并，但对列表 item 不去重。需要去重合并就自己写 reducer function。

### 面试金句
> "我用一个大 AgentState 是因为这五个 Agent 数据依赖是线性顺流的。每个 Agent 的输出是下一个的输入，共享黑板比消息传递更简洁。关键是列表字段必须用 `Annotated[list[...], operator.add]` —— 这是 LangGraph 新手最常见的坑，不写就会发现循环跑完只剩最后一步的结果。"

---

## 5. Subprocess 沙盒 vs Docker / E2B —— 安全性妥协

### 背景
Analysis Agent 会让 LLM 生成 Python 代码（pandas + matplotlib）并执行。这是**代码注入攻击面**，安全方案很重要。

### 方案对比

| 方案 | 隔离级别 | 实现复杂度 | 生产就绪 |
|---|---|---|---|
| `exec()` 同进程 | **零** | 极低 | ❌ 绝不可用 |
| **subprocess + timeout**（我们的选择） | 进程级（共享 OS） | 低 | ⚠️ 开发/面试 OK，生产不够 |
| Docker 容器 | 容器级 | 中 | ✅ 大多数场景 |
| gVisor / Firecracker | 内核隔离 | 高 | ✅ 金融级 |
| E2B / Modal sandboxes | 云托管 | 低 | ✅ 快速生产 |

### Subprocess 方案的具体弱点

LLM 生成的代码能：
- 读你机器的**任何文件**（除用户权限外）
- 调网络 → 理论上能把数据外泄
- 执行 `import os; os.system("rm -rf ~")` → 60 秒超时前足够造成破坏
- CPU/内存无限制 → `while True: x = [0]*10**9` 会把 laptop 搞崩

### 迁移路径设计

在 `python_sandbox.py` 定义 `CodeExecutor` 抽象接口：

```python
class CodeExecutor(Protocol):
    def run(self, code: str, timeout: int) -> ExecutionResult: ...

class SubprocessExecutor: ...   # 当前实现
class DockerExecutor: ...        # 未来实现
class E2BExecutor: ...           # 未来实现
```

通过环境变量 `CODE_EXECUTOR=subprocess|docker|e2b` 切换。

### 面试金句
> "这个项目 subprocess 够用，因为：① 面试演示场景下我控制输入，② 跑在一次性环境。生产上线必须换 Docker 或 E2B —— 我在 `python_sandbox.py` 里定义了 `CodeExecutor` 抽象接口，subprocess 和 Docker 是两个实现，通过环境变量切换。README 写了这个 migration path。"

这个回答展示的是**知道权衡、知道什么时候要升级方案** —— 比"我用 Docker 所以很安全"更有说服力。

---

## 6. ReAct 循环 vs 纯结构化输出 —— Query Agent 的选型

### 两种 Query Agent 写法

**方案 A —— ReAct（我们的选择）：**
```
LLM 思考 → 调 list_tables → 看结果
     → 调 describe_table → 看结果
     → 写 SQL → 调 execute_sql → 看结果
     → 返回最终答案
```
每一步 LLM 自主决定下一步做什么。

**方案 B —— 纯结构化输出（Plan-Execute）：**
```python
# LLM 一次性输出
{
    "tables_to_check": ["orders", "customers"],
    "sql": "SELECT ... FROM orders ..."
}
# 程序逐个执行，不给 LLM 反馈机会
```

### 为什么 Query Agent 选 A？

- Olist schema 对 LLM 是**未知**的，它需要"探查 → 看 → 决策"的循环才能写对 SQL
- SQL 报错时（字段名拼错等），ReAct 能看到报错自己修；B 要另起一轮
- ReAct 失败时轨迹**可解释**（"它以为有 `total_price` 字段，其实叫 `price`"）→ 调试友好

### 为什么 Planner 选 B（结构化输出）？

- 规划步骤本身**不需要看数据** —— 只基于 user_query 文本
- `with_structured_output()` 保证产出**合法的** `ExecutionStep` list，而不是 LLM 想生成啥就生成啥
- 单次 LLM 调用，成本低

### 这就是 orchestrator-workers 模式

```
   [Orchestrator: Planner]          ← 用结构化输出（确定性高）
        ↓  execution_plan
   [Workers: Query/Analysis Agent]  ← 用 ReAct（自主适应）
        ↓  results
   [Reporter]                       ← 用纯 LLM 综合
```

**上层规划用确定性结构化调用，底层执行用自主 ReAct 循环。**

### 面试金句
> "Planner 和 Query Agent 用了不同的模式不是随意的 —— Planner 只处理文本不需要看数据，所以用 structured output 保证产出结构合法；Query Agent 要和 schema 交互，SQL 可能报错，所以用 ReAct 让它有 self-correction 能力。这是 orchestrator-workers 模式的典型应用。"

---

## 7. 配置复杂度：InsightPilot 150 行 vs GraphRAG 15 行

### 触发问题
面试官看到 `config.py` 时可能会问："为什么一个数据分析 Agent 的配置要这么复杂？"这题考的是**你对工程复杂度的判断力**，不是你写了多少行代码。

### 表层对比

**GraphRAG 典型配置（15 行）：**

```python
import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
EMBEDDING_MODEL = "text-embedding-3-small"
```

**InsightPilot `config.py`（~150 行）：**
pydantic-settings + Literal + Field 约束 + computed_field 派生路径 + lru_cache 单例。

### 深层答案：复杂度有五个来源

| 来源 | GraphRAG | InsightPilot | 是否真需要？ |
|---|---|---|---|
| **消费方数量** | 1 个 LLM 链 | 5 个 Agent + 沙盒 + 工具链 | ✅ 真需要 |
| **安全阈值** | 几乎没有 | `max_rows` / `sql_timeout` / `sandbox_timeout` / `max_iterations` | ✅ 真需要 |
| **运行入口** | notebook | CLI + notebook + pytest + Makefile | ⚠️ 部分需要 |
| **路径抽象** | 无本地文件 | DuckDB / outputs / knowledge_base 都是文件 | ✅ 真需要 |
| **类型校验** | 错了一眼看出 | 配错跑半天才发现 | ⚠️ 部分需要 |

### 真正不能省的刚需（3 个）

1. **多 Agent 共享配置** —— 5 个 Agent 各自 `os.getenv()` 会失控
2. **安全阈值（超时/行数/循环上限）** —— 防止 Analysis Agent 的 subprocess 跑死机器
3. **路径绝对化** —— CLI / pytest / notebook 从不同 cwd 启动，相对路径会崩

### 可以承认过度的部分

| 代码 | 简化后果 |
|---|---|
| `Literal["gpt-4o", "gpt-4o-mini", ...]` | 小 —— 错了会在 OpenAI 调用时报错 |
| `ge=1, le=10000` 数值范围 | 小 —— 正常人不会配 -1 |
| `@computed_field` 四个派生属性 | 小 —— 只影响 `.model_dump()` 显示 |
| `log_level` 字段 | 零 —— 目前还没接入 logging |

### 精简版（40 行够用）

```python
from pathlib import Path
from pydantic_settings import BaseSettings

_ROOT = Path(__file__).resolve().parent.parent.parent

class Settings(BaseSettings):
    openai_api_key: str             # 必填
    openai_model: str = "gpt-4o"
    duckdb_path: Path = Path("data/warehouse.duckdb")
    max_sql_rows: int = 500
    sql_timeout: int = 30
    python_sandbox_timeout: int = 60
    max_iterations: int = 20

    class Config:
        env_file = _ROOT / ".env"
        extra = "ignore"

    @property
    def duckdb_abs_path(self) -> Path:
        return _ROOT / self.duckdb_path

def get_settings() -> Settings:
    return Settings()
```

### 为什么还是写了复杂版？

**面试作品 vs 生产项目的权衡不同：**

- 面试作品：门面代码要展示"工程品味信号"—— Literal、Field 约束、computed_field 都是被会写 Python 的面试官识别的信号
- 生产项目：配置要**简单到同事 30 秒能读懂**，否则改配置的人会怕

**信号密度 vs 易读性**，是两个不同的优化目标。

### 判断规则（经验法则）

```
项目复杂度（Agent 数 × 工具数 × 入口数 × 部署环境数）
    ↕ 成正比
配置复杂度（字段数、校验严格度、抽象层）
```

- **1-2 个 LLM 调用 + notebook 运行** → GraphRAG 级别，15 行够了
- **3-5 个 Agent + CLI/notebook/test 多入口** → InsightPilot 级别，合理复杂
- **>10 个服务、多环境** → 拆成多个 Settings 类或用 Dynaconf / Hydra

### 面试金句
> "配置复杂度应该是对**消费者数量**的响应，不是炫技的场所。我在 GraphRAG 里用 `os.getenv` 就够了，因为只有一个 LLM 链；InsightPilot 有 5 个 Agent 共享配置 + 安全阈值是刚需 + 多入口跑，所以升级到 pydantic-settings。这种判断力比'能写多复杂'更重要。"

---

## 8. SQL 安全：三层纵深防御怎么设计的

### 触发问题
面试官问：**"Text-to-SQL 的安全性怎么保证？LLM 要是生成了 `DROP TABLE` 呢？"**

### 表层答案（不够）
"我在工具里校验 SQL 必须以 SELECT 开头。"

这个答案有个漏洞 —— 任何单层防御都能被绕过。好的答案要展示**纵深防御**思维。

### 深层答案：三层协同

`tools/duckdb_executor.py` 里的 SQL 进来，要经过 3 层拦截：

```
     ┌─────────────────────────────────────────────────┐
     │  第 1 层：正则白名单（轻量，快速拒绝 90% 攻击）   │
     │   _READONLY_PATTERN.match(sql)                  │
     │   只允许 SELECT / WITH 开头                     │
     └────────────────────┬────────────────────────────┘
                          │ （绕过了？继续）
                          ↓
     ┌─────────────────────────────────────────────────┐
     │  第 2 层：SQL 包装（SELECT * FROM (...) LIMIT）  │
     │   把用户 SQL 包在子查询里                        │
     │   → 多语句注入会变成子查询语法错误              │
     └────────────────────┬────────────────────────────┘
                          │ （还绕过？继续）
                          ↓
     ┌─────────────────────────────────────────────────┐
     │  第 3 层：DuckDB read_only=True                  │
     │   duckdb.connect(path, read_only=True)          │
     │   数据库层面拒绝任何写操作                       │
     └─────────────────────────────────────────────────┘
```

### 第 1 层：正则拆解

```python
_READONLY_PATTERN = re.compile(
    r"^\s*(--[^\n]*\n\s*)*(SELECT|WITH)\b",
    re.IGNORECASE | re.DOTALL,
)
```

| 片段 | 含义 |
|---|---|
| `^` | **必须从字符串开头**开始匹配（`.match()` 强制的） |
| `\s*` | 允许前导空白（空格、tab、换行） |
| `(--[^\n]*\n\s*)*` | 允许零到多行 `--` 注释开头 |
| `(SELECT\|WITH)` | 白名单：只能是 SELECT 或 WITH |
| `\b` | 词边界，防止 `SELECTED` 被当成 `SELECT` 通过 |

两个 flag：
- `re.IGNORECASE` —— `select` / `SELECT` / `SeLeCt` 都认
- `re.DOTALL` —— `.` 能匹配换行（给未来扩展留口）

**关键：`.match()` 只从字符串第 0 位匹配**，不是 `.search()`（后者会找任意位置）。这意味着 `"DROP TABLE; SELECT 1"` 这种把 SELECT 藏在后面的构造**通不过**。

### 绕过尝试 × 各层拦截效果

| 攻击样本 | L1 正则 | L2 包装 | L3 read_only | 最终 |
|---|---|---|---|---|
| `DROP TABLE customers` | ❌ 拦 | — | — | 拦住 |
| `drop table customers` | ❌ 拦（IGNORECASE） | — | — | 拦住 |
| `   DROP TABLE customers` | ❌ 拦 | — | — | 拦住 |
| `-- SELECT伪装\nDROP TABLE x` | ❌ 拦 | — | — | 拦住 |
| `SELECTEDROP TABLE x` | ❌ 拦（\b 词边界） | — | — | 拦住 |
| `SELECT 1; DROP TABLE x` | ✅ 过 | ❌ 拦（子查询语法错误） | — | 拦住 |
| `WITH x AS (SELECT 1) DELETE FROM customers` | ✅ 过 | ✅ 过 | ❌ 拦 | 拦住 |
| `/* comment */ SELECT * FROM orders` | ❌ 拦（误伤） | — | — | 保守误杀 |

### L2 的巧妙之处

看这行：

```python
wrapped_sql = f"SELECT * FROM ({sql_stripped}) AS __inner LIMIT {max_rows + 1}"
```

这一步不仅是为了加 LIMIT 上限 —— **副作用是天然阻止多语句注入**。

比如用户 SQL 是 `SELECT 1; DROP TABLE customers`，包装后：

```sql
SELECT * FROM (SELECT 1; DROP TABLE customers) AS __inner LIMIT 501
```

子查询里**不允许多条语句**，DuckDB 解析时直接 `Parser Error`。攻击失效。

**这是纵深防御的精华 —— 安全不是单点完美，是多点叠加。**

### L3 的存在理由

```python
con = duckdb.connect(str(settings.duckdb_abs_path), read_only=True)
```

`read_only=True` 让整个连接在 **DuckDB 内核**层面禁止写。即使前两层都出 bug（比如正则写错、SQL 包装有漏洞），DuckDB 自己也会拒绝 DELETE / DROP / INSERT / UPDATE。

**这是最后一道保险丝**，前两层是应用层，这一层是引擎层，两者独立。

### 更硬核的版本（AST 解析）

真正生产级的做法是用 SQL AST 解析器：

```python
import sqlparse

def _is_readonly_sql_strict(sql: str) -> bool:
    parsed = sqlparse.parse(sql)
    if len(parsed) != 1:                    # 多语句直接拒
        return False
    stmt = parsed[0]
    if stmt.get_type() != "SELECT":         # AST 层面的类型判断
        return False
    # 还可以递归遍历子节点，确保没有 INSERT/UPDATE/DELETE token
    return True
```

**为什么这个项目不用？**
- 依赖更重（多一个 sqlparse）
- 维护成本高（SQL 方言变化要更新规则）
- **威胁模型不匹配**：我们的输入来源是 LLM，不是恶意用户。LLM 偶尔会写错，但不会主动构造攻击。

### 面试金句
> "这段 SQL 安全不是单层防御，是三层纵深：
> **第一层正则** 挡住 90% 的直接 DROP；
> **第二层 SQL 包装** 让多语句注入变成子查询语法错误；
> **第三层 DuckDB read_only** 是内核级兜底。
> 每一层都假设上一层会失守 —— 这是纵深防御（Defense in Depth）的核心原则。
> 我选正则而不是 AST 解析，是因为威胁模型是'LLM 偶尔犯错'而非'对抗性攻击'，成本收益不匹配。如果是多租户 SaaS 我会升级到 sqlparse。"

### 附：判断安全强度的准则

**安全措施强度必须匹配威胁模型：**

| 场景 | 合适的安全强度 |
|---|---|
| 面试作品 / 单用户 demo | 正则 + read_only 足够 |
| 内部工具 / 已认证用户 | + AST 校验 |
| 多租户 SaaS / 外部用户 | + 行级权限 + 查询审计 |
| 金融 / 医疗级系统 | + 代理层查询改写 + 沙盒隔离 |

---

## 9. 数据治理与元数据管理 —— 从字典到数据目录

### 触发问题
面试官看到 `metadata_explorer.py` 里的 `TABLE_DESCRIPTIONS = {...}` 硬编码字典，会追问：
**"这种做法怎么扩展到 100 张表？业务方想改 KPI 口径怎么办？"**

这是**数据治理**问题，考的是你对数据团队真实工作流的理解。

### 表层答案：现状
`TABLE_DESCRIPTIONS` 字典写死在 Python 代码里，11 张表够用。

### 深层答案：字典方案在 6 个维度上会崩

| 问题 | 发生在什么时候 |
|---|---|
| 非技术人员改不了 | 业务方想更新 KPI 口径，得 PR |
| 字段级描述塞不下 | 现在只有表级，一个表 30 个字段就没法写 |
| 搜索困难 | LLM 要问"有哪些用户数相关的字段" → 没法检索 |
| 多语言困难 | 中英文双版本要自己维护两套 dict |
| 版本管控困难 | 谁什么时候改的？为什么改？需要 git blame |
| 元数据 / 业务知识混杂 | schema 是自动的，业务含义是人写的，放一起不好管 |

**核心问题：字典里塞的是"业务知识"，但业务知识应该有自己的生命周期。**

### 四层演进路径

#### Level 1：YAML / JSON 外置（小团队起步）

把业务知识搬到独立的 YAML 文件，一表一文件：

```yaml
# data/metadata/tables/customers.yaml
name: customers
type: dimension
description: 客户维度表
owner: data-team@company.com
updated_at: 2026-04-20
gotchas:
  - issue: customer_id vs customer_unique_id 容易混淆
    detail: |
      customer_id 是订单级标识，一个人多次下单会有多个
      customer_unique_id 才是真实用户 ID
      统计 UV 必须用 customer_unique_id
columns:
  customer_id:
    type: VARCHAR
    description: 订单关联键（非 user 主键！）
    is_pii: false
  customer_unique_id:
    type: VARCHAR
    description: 真实用户唯一标识
    is_pii: true
  customer_state:
    type: VARCHAR(2)
    valid_values: [SP, RJ, MG, ...]
related_tables:
  - orders
  - order_reviews
```

Python 侧：
```python
import yaml
from pathlib import Path

TABLE_METADATA = {
    f.stem: yaml.safe_load(f.read_text())
    for f in Path("data/metadata/tables").glob("*.yaml")
}
```

**获得什么：**
- 非技术人员可直接改 YAML（PM / BA 都熟）
- 字段级描述有空间
- 结构化字段（`is_pii`、`valid_values`）Agent 可直接用
- git diff 看得清"谁改了什么口径"

**够用规模：** 20-50 张表。

#### Level 2：数据库原生注释（让 DB 成为 source of truth）

PostgreSQL / BigQuery / Snowflake / Databricks / DuckDB 1.1+ 都支持：

```sql
COMMENT ON TABLE customers IS '客户维度表：customer_id 是订单级';
COMMENT ON COLUMN customers.customer_unique_id IS '真实用户唯一标识，统计 UV 用这个';
```

查询注释：
```sql
SELECT table_name, comment FROM duckdb_tables() WHERE schema_name='main';
SELECT column_name, comment FROM duckdb_columns() WHERE table_name='customers';
```

**好处：**
- 注释跟着表走，迁移数据时自动带过去
- DB GUI 工具（DBeaver、DataGrip）直接显示
- 多个下游系统（Superset / Tableau / BI）都能读
- **source of truth 是 DB 本身，没有"文档和 DB 不一致"的风险**

**坏处：**
- 只能存纯文本，结构化差
- 大段业务文档塞不进去

**实战组合**：字段级简短说明用 `COMMENT`，长文档放 YAML 外挂。

#### Level 3：dbt 模型文档（大厂标配）

dbt 把业务文档当成一等公民：

```yaml
# models/marts/dim_customers.yml
version: 2
models:
  - name: dim_customers
    description: |
      客户维度表。
      口径：
        - UV = COUNT(DISTINCT customer_unique_id)
        - 活跃用户 = 最近 90 天有订单的 customer_unique_id
    meta:
      owner: data-team@company.com
      pii_level: high
    columns:
      - name: customer_unique_id
        description: 真实用户唯一标识
        tests:
          - unique
          - not_null
```

**dbt 的 killer 特性：**
- description 里可以引用其他模型：`{{ doc('customer_pii_policy') }}`
- **测试跟文档绑定** —— 你说"唯一"就会自动被测试
- **血缘关系自动生成** —— `dbt docs generate` 出交互网页
- **metrics 独立定义** —— 口径作为一级对象：

```yaml
metrics:
  - name: monthly_active_users
    calculation_method: count_distinct
    expression: customer_unique_id
    timestamp: order_purchase_timestamp
    time_grains: [day, week, month]
    filters:
      - field: order_status
        operator: '='
        value: "'delivered'"
```

**这是"口径"最专业的表达形式** —— SQL 级别可执行、可测试、可复用。

#### Level 4：数据目录 / 语义层（公司级基础设施）

公司规模再大就需要专门的**数据目录（Data Catalog）**：

| 工具 | 厂商 | 定位 |
|---|---|---|
| **DataHub** | LinkedIn 开源 | 开源旗舰 |
| **Amundsen** | Lyft 开源 | UI 偏搜索引擎 |
| **Atlan** | 商业 | 对 AI Agent 友好 |
| **OpenMetadata** | 开源 | 对 LLM 原生支持 |
| **Collibra** | 商业 | 企业级，偏合规 |

字节自研的 DataLeap 本质上是 DataHub + 自己的 UI + 权限系统。

**这些工具提供的四个核心能力：**

1. **Business Glossary（业务术语表）**
   "DAU"、"ROAS"、"订单完成率"作为词条，跨团队统一定义；字段可绑定到术语

2. **Lineage（血缘）**
   自动从 SQL 解析出字段的上下游。**Agent 回答"这个数字口径对不对"时血缘是核心依据**

3. **Data Contracts（数据契约）**
   上游承诺"schema 不会变，SLA 99.9%"，下游基于契约写代码

4. **Semantic Layer（语义层）**
   Cube.dev / AtScale / dbt Semantic Layer。应用层不直接写 SQL，而是调语义 API。
   **LLM 和语义层天作之合** —— LLM 只需生成"要哪个 metric、按哪个维度切"

### Agent 如何消费元数据？三种模式

| 模式 | 做法 | 适用场景 |
|---|---|---|
| **A. 全塞 system prompt** | 所有表描述一次喂进去 | 表 < 20 张，总文档 < 5K tokens |
| **B. RAG 检索** | 业务文档 embedding，按问题检索 Top-K 塞 prompt | 文档量大、长尾术语多 |
| **C. Tool Call 按需** | LLM 自己调 `list_tables`/`describe_table` | ReAct 天然适配（**InsightPilot 现状**） |

**实战：三种常组合用**
- 超核心术语（如"UV 定义"）→ system prompt 常驻
- 可枚举的 metric / table → tool call 按需
- 长尾业务文档 → RAG

### InsightPilot 的升级路径

| 阶段 | 动作 | 收益 |
|---|---|---|
| **现在（demo 阶段）** | 保留 `TABLE_DESCRIPTIONS` 字典 | 简单、够用 |
| **第四阶段** | 抽到 `data/knowledge_base/*.md` + ChromaDB RAG | 支持"术语 → SQL"检索，面试亮点 |
| **假设上生产** | YAML per-table + DuckDB `COMMENT` | 业务方可维护，文档跟 DB 走 |
| **假设团队扩大** | 接 dbt，metric 独立定义 | 口径可测试、可复用 |
| **假设公司规模** | 对接 DataHub / Atlan | 血缘 + 搜索 + 跨部门复用 |

### 第四阶段的 RAG 文档设计示例

`data/knowledge_base/metrics.md` 将采用这种格式：

```markdown
# ROAS（广告投入回报率）

## 定义
ROAS = 广告带来的营收 / 广告花费

## 口径细节
- 营收：已配送订单（status='delivered'）的 payment_total 之和
- 广告花费：当期广告投放成本
- 归因窗口：点击后 7 天内购买

## 相关字段
- orders.order_status
- orders.payment_total

## 计算 SQL 模板
SELECT SUM(payment_total) / {ad_spend}
FROM orders WHERE order_status = 'delivered'
  AND order_purchase_timestamp BETWEEN {start} AND {end}
```

**这种设计兼顾两种消费者**：人读起来像业务 wiki，LLM embedding 后语义检索也准。

### 面试金句

> "数据治理是个演进问题，不是一步到位的。**小团队** YAML + DuckDB COMMENT 起步，**中型团队** 上 dbt 把 metric 作为一等公民，**大型团队** 用 DataHub / Atlan 做数据目录 + 语义层。
>
> **Agent 时代的新增维度是**：元数据不只给人看，还要给 LLM 消费 —— 这意味着要兼顾结构化（便于程序处理）和自然语言（便于 embedding 和检索）。
>
> 我在 InsightPilot 里的分层设计（Python dict → YAML → ChromaDB RAG）就是这个演进路径的缩影 —— 做面试 demo 用字典够了，但架构里预留了向数据目录演进的扩展点。"

### 判断治理复杂度的准则

```
团队规模 × 表数量 × 消费者数量
    ↕ 成正比
治理工具的投入
```

- **1 人 / < 10 张表 / 单消费者** → 字典或 YAML
- **10 人 / 50 张表 / 多团队** → dbt + DB COMMENT
- **100 人 / 1000 张表 / 全公司** → DataHub / Atlan + 语义层
- **1000 人 / 10000 张表 / 合规场景** → + Data Contracts + Data Mesh

---

## 附：还没展开但值得继续深挖的主题

以下话题等我们实际写到对应代码时再细聊：

- [ ] **Checkpointer 底层机制**：SqliteSaver 为什么不是 PostgresSaver？thread_id 语义、resume 语义
- [ ] **`interrupt()` 是什么魔法？**：其实是 Python generator yield + 状态序列化
- [ ] **RAG 为什么用 ChromaDB**：vs Pinecone / Weaviate / pgvector 的选型
- [ ] **Prompt 工程细节**：Query Agent 的 system prompt 怎么写才能让 SQL 准确率上去
- [ ] **循环保护 `max_iterations`**：为什么 20 不是 10 或 50
- [ ] **流式输出 `astream_events`**：第五阶段 CLI 美化会用到

---

## 使用建议

- **面试前一天**：通读这份 + 计划文档 + 代码目录，足够撑 1 小时技术面
- **面试中**：如果被问到某个决策，按"表层 → 深层 → 金句"的顺序回答，金句作为收尾
- **面试后**：记下被追问到的新角度，回来补充进"还没展开"清单
