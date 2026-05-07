# InsightPilot —— 代码精读路线图

> 这份指南帮你**深度理解** vibe coding 写出的 ~5500 行代码。
> 不是按字母序读，而是按**理解依赖层级**读 —— 先读契约，再读工具，最后读编排。
>
> 总时长：**6-8 小时**（建议分 3-5 天，每次 1-2 小时不拖过载）

---

## 阅读前的准备（10 分钟）

**1. 确认环境能跑：**
```bash
cd insight-pilot
uv run insight-pilot version            # 应该能输出版本号
uv run python -m insight_pilot.config   # 应该能打印配置（API key 显示 ***SET***）
```

**2. 三个窗口同时开着：**
- 编辑器（看代码）
- 另一个 tab 开 `docs/design-decisions.md`（每读一个文件配套精读对应章节）
- 笔记本 / Notion（记下"我没想清楚"的问题，回头一起问）

**3. 设定阅读心态：**
- ✅ 读不懂 → 先记下问题，继续往下，**不要钻死**
- ✅ 想动手验证 → 改一行试试，看会不会崩
- ✅ 觉得"为什么这样"是个好问题 → 一定记下来
- ❌ 不要追求"100% 理解每一行" —— 80/20 即可

---

## 第 1 层：数据契约（~1 小时）

> **这一层是地基。读不懂这里，后面都是空中楼阁。**

### 📄 `src/insight_pilot/state.py`（~280 行）

**🎯 核心职责**：定义整个项目共享的"数据语言"。所有 Agent 都要按这里的契约读写。

**🔗 上下游**：被几乎所有文件 import。改这里 = 牵动全身。

**📖 配套精读**：`design-decisions.md` §4「State 设计」

**❓ 读完应该能回答**：
1. **三种类型系统的分工** —— 为什么 `ExecutionStep` 用 Pydantic，`QueryResult` 用 dataclass，`AgentState` 用 TypedDict？
2. **`Annotated[list[X], operator.add]` 是什么意思？** 不写会发生什么？
3. **`add_messages` 和 `operator.add` 区别？** 都是合并 list，为什么 messages 用前者？
4. **`create_initial_state` 为什么必须存在？** 直接手写 `AgentState({"user_query": ...})` 会出什么问题？
5. **`@dataclass` 上的 `to_llm_string` 方法是什么时候被调用的？**

**⚠️ 常见困惑点**：
- 看到 Pydantic、dataclass、TypedDict 三种类型可能会懵 —— **它们是按"输入可信度"分的**，不是按"功能"分
- `Annotated` 看起来像普通类型注解，其实是 LangGraph 的 reducer 信号

---

### 📄 `src/insight_pilot/config.py`（~140 行）

**🎯 核心职责**：从 `.env` / 环境变量加载所有配置，类型安全 + 单例缓存。

**🔗 上下游**：被所有需要"知道路径、超时、模型"的文件 import。

**📖 配套精读**：`design-decisions.md` §7「配置复杂度」

**❓ 读完应该能回答**：
1. **`Field(...)` 里三个点是什么意思？** 和 `Field(default="x")` 区别？
2. **`@computed_field` 和 `@property` 顺序为什么不能反？**
3. **`@lru_cache` 实现单例的好处？** 比 `global _settings` 强在哪？
4. **`duckdb_path` 和 `duckdb_abs_path` 为什么要分两个字段？**

**⚠️ 常见困惑点**：
- `model_config = SettingsConfigDict(...)` 是 Pydantic v2 的写法，v1 用过来的人会困惑
- `_PROJECT_ROOT` 是用 `Path(__file__).parent.parent.parent` 推出来的 —— 改文件位置会崩

---

### ✅ 第 1 层验收

跑这个测试，全绿才进第 2 层：
```bash
uv run pytest tests/test_state.py -v
```

---

## 第 2 层：核心工具（~1.5 小时）

> **每个工具都是独立可读的，不依赖图编排。**

### 📄 `src/insight_pilot/tools/duckdb_executor.py`（~300 行）

**🎯 核心职责**：把 SQL 字符串变成结构化 `QueryResult`。三层只读防御 + LLM 友好的错误分类 + 子线程超时。

**🔗 上下游**：被 `lang_tools.py` 包装；graph 层重新执行 SQL 时也调它。

**📖 配套精读**：`design-decisions.md` §8「SQL 安全：三层纵深防御」

**❓ 读完应该能回答**：
1. **`_READONLY_PATTERN` 这条正则的每一段在做什么？**（参考 §8 已经讲过）
2. **为什么要把 SQL 包一层 `SELECT * FROM (...) LIMIT max+1`？** 不只是为了截断，对吧？
3. **`_format_error` 里为什么要按错误类型分类？** 不能直接 `return str(exc)` 吗？
4. **`_execute_with_timeout` 用 `threading.Thread` + `con.interrupt()` 实现超时**，为什么不用 `signal.SIGALRM`？
5. **每次调 `execute_sql` 都新开 DuckDB 连接**，为什么不用连接池？

**⚠️ 常见困惑点**：
- `LIMIT max_rows + 1` 的"+1"技巧很巧妙，要看懂为什么 —— 用来检测原查询是否被截断
- 错误信息里的"修复建议"是给 LLM 看的，不是给人看的

---

### 📄 `src/insight_pilot/tools/metadata_explorer.py`（~280 行）

**🎯 核心职责**：给 LLM 提供 `list_tables` / `describe_table` / `sample_rows` 三个探查工具。

**🔗 上下游**：被 `lang_tools.py` 包装。

**📖 配套精读**：`design-decisions.md` §9「数据治理与元数据管理」

**❓ 读完应该能回答**：
1. **`TABLE_DESCRIPTIONS` 字典里塞的 `customer_id vs customer_unique_id` 提示，是给谁看的？**
2. **`describe_table(table_name)` 内部为什么要先调 `_get_all_table_names()` 做白名单校验？** 不是已经只读连接了吗？
3. **为什么 `sample_rows` 用 `USING SAMPLE N ROWS` 而不是 `LIMIT N`？**
4. **三个工具都返回 `str` 而不是 `list[dict]`**，为什么？

**⚠️ 常见困惑点**：
- "白名单防 SQL 注入"和"只读连接"是**两层独立防御**（前面拦"绕开校验进 DROP"，后面拦"WITH ... DELETE"）
- DuckDB 不支持 parameterized identifier（表名不能用 `?` 占位），所以拼字符串前必须校验

---

### 📄 `src/insight_pilot/tools/python_sandbox.py`（~290 行）

**🎯 核心职责**：subprocess 隔离地跑 LLM 写的 Python 代码，捕获 stdout + 检测新生成的图表。

**🔗 上下游**：被 `lang_tools.py` 通过 `make_run_python_tool` 工厂包装。

**📖 配套精读**：`design-decisions.md` §5「Subprocess 沙盒 vs Docker / E2B」

**❓ 读完应该能回答**：
1. **`SANDBOX_PRELUDE` 是什么？** 为什么要在 LLM 代码前面拼一段"前导脚本"？
2. **数据怎么进沙盒？** —— `INPUT_DATA_PATH` 环境变量 + JSON 文件
3. **新生成的图表怎么被检测出来？** —— `files_after - files_before` 差集
4. **为什么 cwd 设成 `outputs/`？** 安全和便利哪个考虑更多？
5. **`_format_python_error` 跟 SQL 的错误分类器一脉相承**，体现什么共通设计原则？

**⚠️ 常见困惑点**：
- "差集检测图表" 这个机制在 Phase 3 出过 bug —— 重跑代码时 files_before 已包含图，差集变空。所以后来改成 captures 模式
- subprocess 不阻止文件读写、网络访问 —— 它只阻止"主进程崩溃"

---

### ✅ 第 2 层验收

```bash
uv run pytest tests/test_duckdb_executor.py tests/test_metadata_explorer.py -v
uv run python -m insight_pilot.tools.python_sandbox  # 应该生成一张图到 outputs/
```

---

## 第 3 层：适配 + 知识层（~45 分钟）

### 📄 `src/insight_pilot/tools/lang_tools.py`（~180 行）

**🎯 核心职责**：把"框架无关"的核心工具包装成 LangChain `@tool` 对象，供 Agent 用。这是 Hexagonal Architecture 的"外层"。

**🔗 上下游**：核心工具 → 这里 → Agent。

**📖 配套精读**：`design-decisions.md` §3「不直接用 LangChain SQLDatabaseAgent」

**❓ 读完应该能回答**：
1. **为什么要这层适配？** 直接在 `duckdb_executor.py` 上加 `@tool` 不行吗？
2. **`@tool` 装饰器读什么生成 LLM 看到的 schema？** —— docstring + 类型注解
3. **`make_run_python_tool` 为什么是工厂而不是普通 `@tool` 函数？** —— 闭包要捕获 query_results
4. **`captures` 参数是什么时候诞生的？解决了什么 bug？**

**⚠️ 常见困惑点**：
- LLM 看不见 Python 代码的实现，只看 docstring + 参数 schema —— **docstring 写得好坏决定 LLM 调不调对**
- 闭包 `captures` 参数有过坑：旧 `langgraph.prebuilt.create_react_agent` 重新包装工具会丢失闭包，迁移到 `langchain.agents.create_agent` 才修好

---

### 📄 `src/insight_pilot/tools/knowledge_base.py`（~210 行）

**🎯 核心职责**：把 `data/knowledge_base/*.md` 切片索引到 ChromaDB；按相似度返回 Top-K 业务知识片段。

**🔗 上下游**：graph 的 `knowledge_retrieval_node` 调它。

**📖 配套精读**：`design-decisions.md` §9「数据治理」（特别是"Agent 如何消费元数据"那段）

**❓ 读完应该能回答**：
1. **切片粒度选 `##` 二级标题是怎么权衡的？** 太细 / 太粗各会怎样？
2. **`build_index(force=False)` 的幂等机制怎么实现？** —— `collection.count() > 0` 跳过
3. **检索失败时为什么返回空字符串而不是抛异常？** —— RAG 是辅助能力，不能让整图崩
4. **embedding 模型是什么？为什么默认用 all-MiniLM-L6-v2？**

**⚠️ 常见困惑点**：
- ChromaDB 第一次调用会自动下载 embedding 模型（~100MB），慢一点是正常的
- `PersistentClient` vs `EphemeralClient`：前者写盘（可跨进程），后者只在内存

---

### 📄 `src/insight_pilot/tools/sensitivity.py`（~280 行）

**🎯 核心职责**：判断 Reporter 产出的报告是不是"敏感"，决定 Reviewer 要不要 interrupt。

**🔗 上下游**：被 `agents/reviewer.py` 调用。

**📖 配套精读**：（无对应章节，但可以参考 §5 的"威胁模型分级"思路）

**❓ 读完应该能回答**：
1. **两层判定的设计逻辑？** 关键词 + LLM 兜底，为什么不只用一层？
2. **关键词模式分了 5 个类别**（投资/裁撤/声誉/金额/绝对措辞），分类的好处？
3. **`_classify_by_llm` 为什么用 gpt-4o-mini 而不是 gpt-4o？**
4. **失败时为什么默认 `is_sensitive=True`？** 这是什么策略？

**⚠️ 常见困惑点**：
- "保守策略：宁可错触发也不漏触发" —— 因为漏报和误报代价不对称
- 我们测试时发现"X 投资回报最高"这种**隐式建议**会被漏判 —— 这是已知限制

---

### ✅ 第 3 层验收

```bash
uv run python -m insight_pilot.tools.knowledge_base "什么是 UV？"   # 应该返回相关片段
uv run python -m insight_pilot.tools.sensitivity                    # 跑 7 个测试场景
```

---

## 第 4 层：Agent 层（~1.5 小时）

> **重头戏。每个 Agent 体现不同设计模式，对比着读最有收获。**

### 📄 `src/insight_pilot/agents/planner.py`（~140 行）

**🎯 核心职责**：用 `with_structured_output` 让 LLM 输出合法的 `list[ExecutionStep]`。

**📖 配套精读**：`design-decisions.md` §6「ReAct vs 结构化输出」

**❓ 读完应该能回答**：
1. **`with_structured_output(ExecutionPlan)` 内部做了什么？**（OpenAI function calling 的转换）
2. **为什么要包一层 `ExecutionPlan` 而不直接 `with_structured_output(list[ExecutionStep])`？** —— OpenAI 要求顶层是 object 不是 array
3. **为什么 Planner 不需要 ReAct 循环？**

**⚠️ 常见困惑点**：
- Planner 是**唯一不调工具**的 Agent
- 输出已经是 Pydantic 校验过的对象，下游直接用，**不需要再校验**

---

### 📄 `src/insight_pilot/agents/query.py`（~120 行）

**🎯 核心职责**：用 `create_agent` 把 LLM + 4 个工具 + system prompt 组合成 ReAct Agent。

**📖 配套精读**：`design-decisions.md` §6 + `prompts/query.py` 文件本身（精读 prompt 三层结构）

**❓ 读完应该能回答**：
1. **`create_agent` 内部做了什么？** 我们如果不用它，要自己写多少代码？
2. **为什么 `temperature=0`？**
3. **prompt 里"五个 Olist 业务坑"是给谁看的？什么时候发挥作用？**

---

### 📄 `src/insight_pilot/agents/analysis.py`（~110 行）

**🎯 核心职责**：和 Query Agent 类似，但工具是动态的（`make_run_python_tool` 工厂构建）。

**❓ 读完应该能回答**：
1. **`build_analysis_agent(query_results, captures)` 比 `build_query_agent()` 多了两个参数**，为什么？
2. **`captures` 列表在哪里被填充？什么时候被读？** —— 工具内 append，graph 层读
3. **如果不要 `captures`，从 messages 里事后提取 AnalysisResult 行不行？**（提示：会触发 chart_paths 检测 bug）

---

### 📄 `src/insight_pilot/agents/reporter.py`（~200 行）

**🎯 核心职责**：把完整的 State 综合成一篇 Markdown 报告。纯 LLM 综合，不调工具。

**❓ 读完应该能回答**：
1. **`_format_state_for_reporter` 在做什么？** 为什么不直接把 state 喂给 LLM？
2. **为什么 Reporter 用 `temperature=0.3` 而其他 Agent 用 `0`？**
3. **结尾那段 `if content.startswith("```markdown"): ...` 是修什么 bug？**

---

### 📄 `src/insight_pilot/agents/reviewer.py`（~110 行）

**🎯 核心职责**：判断敏感 → 调 `interrupt()` → 处理用户决定。

**📖 配套精读**：（前面我们的对话讨论了 `interrupt()` 工作机制）

**❓ 读完应该能回答**：
1. **`interrupt({...})` 这一行执行的瞬间发生了什么？**（state 持久化 + 抛 GraphInterrupt）
2. **`decision = interrupt(...)` 的返回值什么时候才有值？** —— 用户调 `Command(resume=...)` 恢复时
3. **如果用户输入 `"yes"` 而不是 `"approve"`，会怎样？**（看 `decision_str.startswith("a")` 的逻辑）
4. **为什么 reviewer 不是 LLM Agent，没有 prompt 文件？**

**⚠️ 常见困惑点**：
- `interrupt()` **不会重跑** reviewer_node 函数开头 —— 恢复时从 `decision = interrupt(...)` 这行**右边**继续。所以 interrupt 之前的代码（敏感性判定）只跑一次

---

### ✅ 第 4 层验收

不用跑测试，**自我对话**：
- 我能不能把 5 个 Agent 按"输入 / 工具 / LLM 调用次数 / 输出"画成一张表？
- 5 个 Agent 里，哪个最简单？哪个最复杂？为什么？

---

## 第 5 层：编排层（~1.5 小时）

> **最后两个文件。读懂了它们，整个项目就贯通了。**

### 📄 `src/insight_pilot/graph.py`（~370 行）

**🎯 核心职责**：把所有节点编排成 StateGraph，加 Checkpointer，定义条件路由。

**📖 配套精读**：`design-decisions.md` §1「LangGraph vs CrewAI」 + §4「State 设计」

**❓ 读完应该能回答**：
1. **整个图的拓扑能不能在脑子里画出来？**（START → kb → planner → router → ... → reporter → reviewer → END）
2. **`decide_next_step` 函数读什么字段做决定？** 它是 LLM 还是纯逻辑？
3. **`builder.add_conditional_edges("query", decide_next_step, routes)` 这行在做什么？**
4. **`compile(checkpointer=SqliteSaver(...))` 不传 checkpointer 会怎样？** —— `interrupt()` 无法 resume
5. **每个节点函数返回的 dict 是怎么"合并"进 State 的？**（提示：reducer）

**⚠️ 常见困惑点**：
- `add_conditional_edges` 的"分支"不是写死的字符串 —— 是函数返回值 → 字典查找
- `iteration_count` 这种简单字段没 reducer，**默认覆盖语义**

---

### 📄 `src/insight_pilot/main.py`（~270 行）

**🎯 核心职责**：CLI 入口。Typer 解析命令、Rich 渲染、`graph.invoke` 循环处理 interrupt。

**❓ 读完应该能回答**：
1. **`_run_graph_with_interrupt_handling` 的核心循环是什么？** 什么条件下结束？
2. **`thread_id` 是用来干什么的？** 每次跑 query 用同一个 ID 行不行？
3. **`graph.invoke(Command(resume="approve"), config=config)` 这个调用怎么"接上"上一次的 state？**
4. **`_render_event` 看到 `tool_calls` 时为什么单独高亮 `execute_sql` 的 SQL？**

**⚠️ 常见困惑点**：
- `Command(resume=...)` 不是 dict，是 LangGraph 的特殊类
- 每次 invoke 都拿当前的 final state；多个 invoke 共享同一个 thread_id 才能续

---

### ✅ 第 5 层验收（最终）

跑一次完整的 demo：
```bash
uv run insight-pilot query "对比 Top 3 品类的营收和评分，给出投资建议"
```

边跑边看终端输出，**对每个节点你都应该能预测下一步会发生什么**。

如果做得到，这就是"读懂了"的证据。

---

## 跨文件理解：通关测试

读完所有文件后，**不要看代码**，回答这些"贯通题"：

### Q1：用户问一句"2017 年月度营收"，从入口到输出，State 经历了什么变化？

应该能说出：
- 入口：`create_initial_state` 填 user_query，其他都是空
- knowledge_retrieval：填 business_context
- planner：填 execution_plan，current_step_index=0
- query：append query_results[0]，current_step_index=1
- analysis：append analysis_results[0] + chart_paths
- reporter：填 report_markdown
- reviewer：填 needs_human_review, status="complete"

### Q2：哪三个文件改坏了，整个项目就跑不起来？

提示：**契约层 + 编排层**。具体哪三个，自己想。

### Q3：如果要把 DuckDB 换成 PostgreSQL，要改几个文件？哪些不用改？

应该能说出"业务逻辑零改动，只改数据层"——这是 Hexagonal Architecture 的好处。

### Q4：interrupt() 触发后，电脑断电关机，明天还能续吗？

应该能说出"能 —— state 在 .checkpoints.db 里，用同一个 thread_id 调 `Command(resume=...)` 即可"。

### Q5：你的项目和"在 ChatGPT 里直接问问题"差距在哪？

应该能说出多个角度（结构化、可复现、可审计、有图表、可挂载新数据源）。

---

## 遇到读不懂的怎么办？

1. **先在笔记里记下问题**（"我不懂为什么 Planner 用 Pydantic"）
2. **继续往下读**，可能后面就解释了
3. **整理 5-10 个问题攒一波**，找我或者搜索官方文档
4. **极端情况**：动手实验。改一行注释掉 → 跑一遍 → 看会不会崩

---

## 记忆固化技巧

读完所有代码后，强烈建议做**两个动作**：

### 动作 1：手画架构图（半小时）

用纸和笔（不是工具），画出：
- 7 个节点
- 节点间的边（条件 / 无条件）
- State 流动方向
- 每个节点的输入 / 输出字段

画出来对照实际代码，差距 = 你还没真懂的地方。

### 动作 2：徒手写 graph.py 的核心 50 行（不抄代码）

```python
def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("...", ...)
    # ...
    return builder.compile()
```

写不出来 = 还没记住。**面试白板就是这种场景**。

---

## 后续

读完后告诉我"读完了"，我接着帮你写 `interview-prep.md`（基于你读完后**实际还有的疑问**写，而不是凭空写一堆没用的题）。
