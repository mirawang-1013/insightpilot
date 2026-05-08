# InsightPilot 代码精读 —— 苏格拉底式 Q&A 笔记

> 这份笔记是 vibe coding 后深度精读阶段的学习记录。
> 用问答形式保留思考过程，每个 Q 都是引导式的小问题，逐步建构对核心概念的理解。
>
> 4 个主题：
> 1. LangGraph State 的 Reducer 机制
> 2. 正则表达式 `^\s*(--[^\n]*\n\s*)*` 拆解
> 3. 多线程超时机制（`_execute_with_timeout`）
> 4. `execute_sql` 主函数的工程细节

---

# Part 1：Reducer（`Annotated[list[X], operator.add]`）

## Q1：Python dict 的 `update` 默认行为？

```python
state = {"x": 1, "y": 2}
state.update({"x": 5})
```

**state 变成什么？**

💡 **答案**：`{"x": 5, "y": 2}` —— 新值覆盖旧值，未提及的字段不变。

🎯 **关键**：这是 Python 内置 dict 的**默认合并规则**。

---

## Q2：list 字段也一样吗？

```python
state = {"results": [1, 2]}
state.update({"results": [3]})
```

**state 变成什么？**

💡 **答案**：`{"results": [3]}` —— **list 也被整体覆盖**，不会自动拼接。

🎯 **关键**：**默认行为对任何类型的字段都是覆盖**，不管是 str / int / list / dict。

---

## Q3：LangGraph 也用这套默认规则会有什么问题？

跑一个 2 步的 plan：
- Step 1 (query) 返回 `{"query_results": [QR1]}`
- Step 2 (query) 返回 `{"query_results": [QR2]}`

**最终 `state["query_results"]` 是什么？**

💡 **答案**：`[QR2]` —— Step 1 的结果**被覆盖了**，QR1 消失。

🎯 **关键问题**：后面 Analysis Agent 只能看到最后一步的结果，**前面所有步骤的数据都丢了**。整个多步分析变得毫无意义。

---

## Q4-6：建立 `operator.add` 的概念

**Q4**：把两个 list 拼起来用什么运算符？
👉 `+`

**Q5**：`[1, 2] + [3]` 等于什么？
👉 `[1, 2, 3]`（list 的 `+` 是拼接）

**Q6**：`operator.add(a, b)` 等于什么？
👉 等价于 `a + b`，是 `+` 的**函数版**：

```python
operator.add(2, 3)         # 5
operator.add([1, 2], [3])  # [1, 2, 3]
operator.add("a", "b")     # "ab"
```

🎯 **关键**：`operator.add` 不是新概念，就是把 Python 的 `+` 包成函数。**对 list 来说就是拼接**。

---

## Q7：用 `operator.add` 当 LangGraph 的合并规则

如果告诉 LangGraph "用 `operator.add` 合并 query_results 字段"：

```python
state["query_results"] = operator.add(old_value, new_value)
```

那么 Step 1 之后是 `[QR1]`，Step 2 返回 `{"query_results": [QR2]}`，**最终是什么？**

💡 **答案**：`[QR1, QR2]` —— 累积成功！

🎯 **关键**：合并规则换了，行为就从覆盖变成累积。

---

## Q8：`Annotated` 是怎么"告诉"LangGraph 的？

```python
class AgentState(TypedDict):
    user_query: str                                              # 没标注 → 默认覆盖
    query_results: Annotated[list[QueryResult], operator.add]    # 标注了 → 用 operator.add
```

如果当前 state 是 `{"user_query": "旧问题", "query_results": [QR1]}`，节点返回 `{"user_query": "新问题", "query_results": [QR2]}`，**合并后两个字段分别是什么？**

💡 **答案**：
- `user_query = "新问题"` —— 默认覆盖
- `query_results = [QR1, QR2]` —— 用 operator.add 累积

🎯 **关键**：**每个字段独立一套规则**。同一次合并里，A 字段可能覆盖，B 字段可能累积。

---

## 一句话锁住

> **每个字段都有"合并规则"。**
> **不写 = 默认规则 = 覆盖。**
> **写 `Annotated[类型, 函数]` = 用这个函数合并。**
> **`operator.add` 是众多可选函数里的一种 —— 对 list 来说就是拼接（累积）。**

---

## 项目里的 5 个 reducer 字段

| 字段 | reducer | 为什么累积 |
|---|---|---|
| `explored_schemas` | `operator.add` | Query Agent 探查过的表名累积 |
| `query_results` | `operator.add` | 多个 query step 的 SQL 结果累积 |
| `analysis_results` | `operator.add` | 多个 analysis step 的输出累积 |
| `chart_paths` | `operator.add` | 多张图表路径累积 |
| `messages` | `add_messages` | 消息列表（特殊：去重 + tool_call 配对）|

**反例（list 但不累积）：**
- `execution_plan: list[ExecutionStep]` —— Planner 只跑一次，不需要累积

🎯 **判定准则**：
- 多次写入 + 想全部保留 → 用 reducer 累积
- 一次写入 / 只关心最新值 → 默认覆盖

---

# Part 2：正则 `^\s*(--[^\n]*\n\s*)*` 拆解

## Q1：`^` 是什么意思？

```python
正则: ^SELECT
A: "SELECT * FROM x"
B: "DROP TABLE; SELECT * FROM x"
```

**正则能匹配哪个？**

💡 **答案**：只能匹配 A。

🎯 **关键**：`^` = **"必须从字符串开头匹配"**（锚点）。B 开头是 DROP，不是 SELECT，所以失败。

---

## Q2：去掉 `^` 会怎样？

正则 `SELECT` 能匹配 `"DROP TABLE; SELECT * FROM x"` 吗？

💡 **答案**：能。

🎯 **关键**：没有 `^` 时，正则可以**在字符串任意位置**找匹配。`^` 是限定开头的"锚"。

---

## Q3-4：`\s*` 是什么？

- `\s` = 一个空白字符（空格 / tab / 换行）
- `*` = 前面那个东西**出现 0 次或多次**

所以 `\s*` = **0 个或多个空白字符**（**包括 0 个**）。

🎯 **关键**：正则**不"删除"字符**，只**匹配 / 允许**。`\s*` 让正则的"光标"走过空白字符。

字符串 `"abc"`（没空白），正则 `\s*` 能匹配吗？
👉 能（匹配 0 个空白）。

---

## Q5：`(ab)*` 是什么意思？

- `(...)` = **分组**（把括号里当一个整体）
- `*` 在组后面 = **整组重复 0 次或多次**

所以 `(ab)*` = 字符串 `"ab"` 作为整体，出现 0+ 次。

匹配的字符串：`""`（0 次）、`"ab"`（1 次）、`"abab"`（2 次）...

---

## Q6-7：`[^\n]*` 是什么？

- `[abc]` = 任意一个字符**是** a/b/c
- `[^abc]` = 任意一个字符**不是** a/b/c（`^` 在方括号里表示"排除"）
- `[^\n]` = 任意一个非换行字符
- `[^\n]*` = 0 个或多个非换行字符

🎯 **通俗讲**：`[^\n]*` = **"一行的内容"**（因为遇到换行就停）。

例：`"hello world\nbye"` 上，`[^\n]*` 从位置 0 能匹配 `"hello world"`（11 字符，到 `\n` 前停）。

---

## Q8：`--[^\n]*\n\s*` 匹配什么？

```
--           → 字面意思的两个减号
[^\n]*       → 0+ 非换行字符（注释正文）
\n           → 一个换行符
\s*          → 0+ 空白（注释后可能的缩进 / 空行）
```

🎯 **答案**：**匹配 SQL 的一行单行注释**（`-- xxxx\n`）。

---

## Q9：`(--[^\n]*\n\s*)*` 加上外层 `*`？

整组（一行注释）重复 **0 到多次**：

| 次数 | 匹配 |
|---|---|
| 0 次 | 没注释 |
| 1 次 | 一行注释 |
| N 次 | N 行连续注释 |

🎯 **关键**：注释是**允许的，不是必须的**（因为是 `*` 包含 0 次）。

---

## Q10：完整前缀 `^\s*(--[^\n]*\n\s*)*`

```
^                         字符串开头开始
\s*                       允许 0+ 个前导空白
(--[^\n]*\n\s*)*          允许 0+ 行单行注释（每行后可有空白）
```

🎯 **整段意思**：**"字符串开头允许有空白和注释，但去掉这些之后，必须是某个东西"**。

---

## Q11：完整正则 `^\s*(--[^\n]*\n\s*)*(SELECT|WITH)\b`

加上后半段：**"那个'某个东西'必须是 SELECT 或 WITH"**。

| 输入 | 匹配 | 原因 |
|---|---|---|
| `"SELECT 1"` | ✓ | 没前导内容，直接命中 SELECT |
| `"   SELECT 1"` | ✓ | `\s*` 吃掉空白 |
| `"-- 注释\nSELECT 1"` | ✓ | 注释组吃掉一行注释 |
| `"DROP TABLE x"` | ✗ | 前缀吃完后是 D，不是 S/W |
| `"-- SELECT 伪装\nDROP TABLE x"` | ✗ | 注释组吃掉伪装行，看到的是 DROP |

🎯 **真正使命**：**保证 SQL 真的是 SELECT/WITH 开头（即只读查询），同时容忍合法的前导空白和注释**。

---

# Part 3：多线程超时机制（`_execute_with_timeout`）

## Q1：单线程的 sleep 是什么行为？

```python
print("A")
time.sleep(10)
print("B")
```

整段大概多少秒？
👉 **10 秒过一点**，因为 sleep 阻塞主线程。

---

## Q2：用子线程后呢？

```python
print("A")
thread = threading.Thread(target=time.sleep, args=(10,))
thread.start()       # 启动子线程
print("B")
```

主线程从开始到打印 B 多少秒？

💡 **答案**：约 3 毫秒（不到 1 秒）。

🎯 **关键**：`thread.start()` **不阻塞主线程**。它只是"派活"给子线程，主线程立刻继续。

**类比**：
- 你（主线程）："小张去买咖啡" ← `thread.start()`
- 小张：去买（10 分钟）← 子线程跑
- 你：继续工作 ← 主线程不等

---

## Q3：`thread.join()` 是什么？

```python
print("A")
thread.start()
thread.join()         # 主线程等子线程
print("B")
```

打印 B 用多少秒？
👉 **10 秒过一点**。

🎯 **关键**：`thread.join()` 让主线程**停下来等子线程完成**。

---

## Q4：`thread.join(timeout=3)`？

```python
thread.join(timeout=3)   # 等最多 3 秒
```

如果子线程要 sleep 10 秒，**主线程从开始到打印 B 多少秒？**
👉 **3 秒过一点**。

🎯 **关键概念**：
- 等子线程，但**最多等 timeout 秒**
- 时间到了如果子线程还没结束，**主线程不等了**，自己继续
- **子线程不会被杀**，它在后台继续跑

🎯 **这就是给 SQL 加超时的核心机制**。

---

## Q5：子线程怎么把结果"传"给主线程？

```python
def runner():
    rows = con.execute(sql).fetchall()
    # rows 是 runner 内部的局部变量，主线程看不到
```

**问题**：主线程怎么拿到 rows？

💡 **答案**：用一个 dict 当"邮箱"，子线程往里塞，主线程从里读：

```python
result_container = {}    # 主线程定义的"邮箱"

def runner():
    rows = con.execute(sql).fetchall()
    result_container["rows"] = rows   # 塞进邮箱

# 主线程
thread.start()
thread.join(timeout=30)
rows = result_container["rows"]       # 从邮箱取
```

🎯 **为什么用 dict 不用普通变量？**

```python
result = None

def runner():
    result = "hello"    # ← 这是创建局部变量！外面的 result 不变
```

但是 dict 不一样 —— **修改 dict 的键值不算"创建新变量"**：

```python
result_container = {}

def runner():
    result_container["key"] = "hello"   # 修改 dict 内容（外面也能看到）
```

---

## Q6：跨线程的异常会自动传播吗？

**正常函数调用**：A 抛异常 → 自动传给调用方 B → 再传给上层。

**子线程**：

```python
def runner():
    raise ValueError("出错")  # 这个异常死在子线程里！

thread = threading.Thread(target=runner)
thread.start()
thread.join()
print("继续跑")    # 异常被吞了，主线程不知道
```

🎯 **重要事实**：**子线程的异常不会自动传到主线程**。这是 Python 多线程的坑。

**所以必须手动接力**：

```python
def runner():
    try:
        rows = con.execute(sql).fetchall()
        result_container["rows"] = rows
    except Exception as e:
        exception_container["error"] = e   # 异常塞另一个邮箱
```

主线程检查：
```python
if "error" in exception_container:
    raise exception_container["error"]
```

---

## Q7：为什么用**两个**邮箱（result + exception）？

💡 **答案**：契约清晰 —— `result_container` 有内容 ⟺ 成功；`exception_container` 有内容 ⟺ 失败。

**反面教材**（一个邮箱）：

```python
container["value"] = ???   # 是数据？还是异常？要靠 isinstance 判断
```

逻辑混乱，容易出错。

🎯 **设计原则**：**两个独立邮箱，自然区分两种状态**，主线程一行 `if "error" in exception_container` 就能判断。

---

## Q8：`is_alive()` 是什么？

主线程超时检查：

```python
thread.join(timeout=30)
if thread.is_alive():
    # 子线程还在跑
```

| 状态 | `is_alive()` 返回 |
|---|---|
| 子线程还在跑 | `True` |
| 子线程已结束 | `False` |

🎯 **超时检测**：`thread.join(timeout=30)` 之后 `is_alive()` 还是 `True` → 说明 30 秒到了它还没跑完 → **超时了**。

---

## Q9：`con.interrupt()` 为什么用 try/except 包？

```python
if thread.is_alive():
    try:
        con.interrupt()        # 取消查询
    except Exception:
        pass                   # 失败也无所谓
    raise TimeoutError(...)
```

💡 **答案**：防御性编程。万一 `con.interrupt()` 自己抛异常，**不希望它掩盖了真正的"超时错误"**。

**反面教材**：

```python
if thread.is_alive():
    con.interrupt()    # 假如这行抛 ConnectionError
    raise TimeoutError(...)   # 永远不执行 ❌

# 用户看到的错误：ConnectionError（误导！其实是超时）
```

🎯 **原则**：**清理 / 善后代码必须 fail-safe**，不能让清理失败覆盖真正的问题。

---

## Q10：`daemon=True` 是什么？

```python
thread = threading.Thread(target=_runner, daemon=True)
```

| 类型 | 主程序退出时 |
|---|---|
| 非守护线程（默认）| **等**这些线程跑完才退出 |
| 守护线程（`daemon=True`）| **强制杀**掉这些线程 |

🎯 **为什么我们必须 `daemon=True`**：万一 DuckDB 不响应 interrupt，子线程一直挂着。**没 daemon → 整个程序退不出**。`daemon=True` 是给"逃脱不了的子线程"的最终后门。

---

# Part 4：`execute_sql` 主函数细节

## Q1：为什么参数默认 `None` 而不是硬编码？

```python
# 写法 A（硬编码）
def execute_sql(sql, max_rows: int = 500): ...

# 写法 B（哨兵 + settings）
def execute_sql(sql, max_rows: int | None = None):
    if max_rows is None:
        max_rows = settings.max_sql_rows
```

假设用户 `.env` 里把 `MAX_SQL_ROWS=2000`，调用 `execute_sql("...")` 不传 max_rows：

| | 写法 A | 写法 B |
|---|---|---|
| 实际 max_rows | **500**（硬编码不变） | **2000**（从 settings 拿）|

🎯 **关键原则**：**Single Source of Truth**

> 一个默认值只能在一个地方定义。`config.py` 是唯一定义默认值的地方。
> 写法 A 把 500 写死在签名里，**两个地方都成了"默认值"** —— 改一处不改另一处就不同步。

---

## Q2：strip 三连为什么需要？

```python
sql_stripped = sql.strip().rstrip(";").strip()
```

输入：`"  SELECT 1 ;  "`

```
原始:                 "  SELECT 1 ;  "
.strip():            "SELECT 1 ;"     (两端空白去掉)
.rstrip(";"):        "SELECT 1 "      (末尾分号去掉，分号前空格暴露)
.strip() 第二次:     "SELECT 1"       (清理暴露出来的空格)
```

🎯 **为什么必须做这个清理**：

下一行会包装：
```python
wrapped_sql = f"SELECT * FROM ({sql_stripped}) AS __inner LIMIT 501"
```

如果 sql_stripped 末尾有分号：

```sql
SELECT * FROM (SELECT 1;) AS __inner LIMIT 501
                       ↑ 子查询里有分号 → DuckDB Parser Error ❌
```

🎯 **额外好处**：**自动阻止多语句注入**。`SELECT 1; DROP TABLE x` 包装后变成子查询里有分号，DuckDB 直接拒绝。这就是 §8 三层纵深防御的**第二层**。

---

## Q3：为什么 `QueryResult.sql = sql`（原始）而不是 `sql_stripped`？

```python
return QueryResult(sql=sql, ...)    # 注意：用原始 sql 不是 sql_stripped
```

🎯 **原因**：**保留原始用户意图，不让内部处理污染对外契约**。

| 场景 | 原始 sql 回执 | 处理后 sql 回执 |
|---|---|---|
| LLM 看回执 | "嗯，是我刚写的那条" ✓ | "我没写过这条啊？" ❌ |
| 调试日志 | 看得到原始字符串（包括分号、空格）| 信息丢失 |

🎯 **设计原则**：**Transparency of Side Effects（副作用透明）**

> 调用方传 X 给你，回执里也应该说 X，不应该说"我处理后的 X'"。

📌 **数据流二分**：
- **真实执行路径**：sql → strip → wrap → DuckDB
- **回执存档路径**：sql 原样 → QueryResult.sql

---

## Q4：为什么 LIMIT `max_rows + 1` 而不是 `max_rows`？

```python
wrapped_sql = f"SELECT * FROM ({sql_stripped}) AS __inner LIMIT {max_rows + 1}"
```

假设 `max_rows = 500`，LIMIT 是 **501**。

| 场景 | LIMIT 500 | LIMIT 501 |
|---|---|---|
| 原查询正好 500 行 | 拿 500 行 | 拿 500 行 |
| 原查询 100 万行 | 拿 500 行 | 拿 501 行 |

**LIMIT 500**：两种情况都是 500 行 → **无法区分** "是否被截断"。
**LIMIT 501**：拿到 501 → 知道被截断；拿到 500 → 知道完整。

```python
truncated = len(rows_tuples) > max_rows   # > 500 就是截断了
if truncated:
    rows_tuples = rows_tuples[:max_rows]   # 砍掉多余的 1 行
```

🎯 **意义**：让 LLM 能感知"结果是否完整"，决定是否要加 WHERE 过滤。多 1 行的代价微小，换来宝贵的边界信息。

---

## Q5：tuple 解包

```python
columns, rows_tuples = _execute_with_timeout(con, wrapped_sql, timeout_seconds)
```

`_execute_with_timeout` 签名：

```python
def _execute_with_timeout(...) -> tuple[list[str], list[tuple]]:
    ...
    return result_container["columns"], result_container["rows"]
```

🎯 **Python 语法**：
- 函数 `return a, b` 自动打包成 tuple `(a, b)`
- 调用方 `x, y = func()` 自动解包，`x` 拿第一个，`y` 拿第二个

---

## Q6：`zip` 是什么？

```python
zip(["id", "name"], (1, "Alice"))
# 产出：[("id", 1), ("name", "Alice")]
```

🎯 **关键**：zip 把两个序列**对应位置配对**。字面意思是"拉链"。

---

## Q7：`dict(zip(columns, row))`

```python
columns = ["id", "name"]
row = (1, "Alice")

dict(zip(columns, row))
# 第一步 zip: [("id", 1), ("name", "Alice")]
# 第二步 dict: {"id": 1, "name": "Alice"}
```

🎯 **核心目的**：把 DuckDB 返回的 tuple 数据**贴上列名标签**，变成 LLM 友好的字典格式。

---

## Q8：列表推导式

```python
rows_as_dicts = [dict(zip(columns, row)) for row in rows_tuples]
```

等价的 for 循环：

```python
rows_as_dicts = []
for row in rows_tuples:
    rows_as_dicts.append(dict(zip(columns, row)))
```

输入：
```python
columns = ["id", "name"]
rows_tuples = [(1, "Alice"), (2, "Bob")]
```

输出：
```python
[
    {"id": 1, "name": "Alice"},
    {"id": 2, "name": "Bob"},
]
```

🎯 **效果**：tuple 形式 → dict 形式，**给每个值贴列名标签**。

---

## Q9：`finally` 什么时候执行？

```python
try:
    columns, rows_tuples = _execute_with_timeout(...)
    return QueryResult(success=True, ...)

except Exception as e:
    return QueryResult(success=False, error=...)

finally:
    try:
        con.close()
    except Exception:
        pass
```

🎯 **答案**：**不管哪种情况都执行**。

| 情况 | 执行顺序 |
|---|---|
| try 成功 | try → finally → return 真正生效 |
| try 抛异常被 except 接住 | try → except → finally → return 真正生效 |
| try 抛异常 except 接不住 | try → finally → 异常继续往上抛 |

🎯 **核心需求**：DuckDB 连接**必须关**，否则文件被锁。`finally` 把"清理"集中到一处，**永远不会忘**。

🎯 **`con.close()` 也用 try/except 包**：跟 `con.interrupt()` 一个思路 —— **清理代码必须 fail-safe**，不让清理失败掀起新异常掩盖原错误。

---

# 学习方法总结

这次精读使用的方法叫 **Socratic Method（苏格拉底式教学法）**：

| 技巧 | 实践 |
|---|---|
| **Active Recall**（主动回忆）| 不直接告诉答案，让你自己答 |
| **Worked Examples**（具体例子优先）| 先给输入让你预测，再讲规则 |
| **Scaffolding**（脚手架）| 每次只引入一个新概念 |
| **Just-in-time Correction**（即时反馈）| 答错立刻修正，不让模糊过去 |
| **Bloom 认知阶梯** | 识别 → 理解 → 应用 → 分析 |

---

## 你可以用这套方法...

- **自学代码**：每读一段，自问"如果我改这一行会怎样"，然后实际改一下验证
- **教别人**：忍住"直接告诉答案"的冲动，先问问题
- **面试**：当面试官讲解概念，主动反问"如果加上 X 会怎样" —— 既能验证理解，又显思考活跃

---

## 面试可以讲的金句汇总

> **关于 Reducer**：
> "我用一个大 AgentState 是因为这五个 Agent 数据依赖是线性顺流的。关键是列表字段必须用 `Annotated[list[...], operator.add]` —— 这是 LangGraph 新手最常见的坑，不写就会发现循环跑完只剩最后一步的结果。messages 字段特殊，必须用 `add_messages` 而不是 `operator.add`，因为它做基于消息 ID 的去重 + tool_call 配对。"

> **关于 SQL 安全**：
> "SQL 安全是三层纵深防御：① 正则白名单挡 90% 显式攻击；② SQL 包装让多语句注入变成子查询语法错误；③ DuckDB read_only 是引擎级兜底。每一层都假设上一层会失守。"

> **关于多线程超时**：
> "DuckDB 没有原生超时，我用'子线程跑 SQL + 主线程 join 带 timeout' 实现。两个 dict 邮箱分别接成功结果和异常，因为跨线程的异常不会自动传播。daemon=True 是给逃脱不了的子线程的最终后门，避免主程序退不出。"

> **关于 execute_sql 设计细节**：
> "几个工程细节：回执用原始 sql，内部用 stripped sql —— 不让实现细节泄漏到对外接口。LIMIT max+1 多取 1 行做边界探测识别截断。rows 用 list[dict] 不用 list[tuple] 给值贴列名标签，LLM 友好。try/except/finally 三段保证连接不泄漏。"

---

# Part 5：Execution Memory 实现细节（4 个 Section，12 个 Q&A）

> 这是给 InsightPilot 加"自我改善飞轮"的实现复盘。
> 涉及 5 个文件的改动：tools/exemplar_store.py（新建）+ state.py / graph.py / planner.py / reviewer.py（修改）+ 4 个优化（quality_score / last_validated_at / team_id / upvote/downvote）。

## Section A：存储层（exemplar_store.py 核心）

### Q1：为什么用单独的 ChromaDB collection（`insight_pilot_exemplars`），不和 `insight_pilot_kb` 混在一起？

💡 **答案**：两类数据**生命周期 + 内容形态 + 消费模式**都不同。

**生产者不同**：知识是人写的；exemplar 是系统自动累积的
**消费时机不同**：知识每次都用；exemplar 按相似度命中才用
**质量门控不同**：知识默认权威；exemplar 需要 approved + 非 stale 过滤
**管理操作不同**：知识手工编辑；exemplar 需要 validate / upvote 等运维 API

**4 个具体失败模式**（如果混在一起）：
1. **检索污染** —— 烂 exemplar 被 RAG 检索为业务知识返回
2. **质量门控逻辑错位** —— knowledge 不需要 approved 字段
3. **文档长度差异** —— 长知识文档会挤掉短 exemplar
4. **批量管理困难** —— 想清空 exemplar 池没法做（会带走知识）

🎯 **架构原则**：**Bounded Context（限界上下文）** —— 不同生命周期 / 不同消费模式的数据，应该有自己的存储。**混在一起短期省事，长期是技术债。**

---

### Q2：`Exemplar` dataclass 的 list / dict 字段为什么要 `json.dumps(...)` 序列化？

```python
def to_metadata(self) -> dict[str, str]:
    return {
        "execution_plan_json": json.dumps(self.execution_plan, ensure_ascii=False),
        "sqls_json": json.dumps(self.sqls, ensure_ascii=False),
        ...
    }
```

💡 **答案**：ChromaDB 的 metadata **只接受基础类型**（str / int / float / bool），不接受 list 或 dict。所以必须把 list 序列化成字符串（JSON 编码）后再塞 metadata。

**关键修正**：不是把 list **转 dict**，是把 list **转字符串**。metadata 本身**就是个 dict**（外层结构），dict 里的 value 必须是基础类型。

---

### Q3：`from_metadata` 用 `metadata.get("execution_plan_json", "[]")` 给空字符串默认值，为什么？

💡 **答案**：**幽灵字段保护**（schema 演进时的健壮性）。

如果某条老 exemplar 是在加这个字段**之前**存的，metadata 里就**没这个键**。`.get()` 返回 `None`，`json.loads(None)` 会崩。

给 `"[]"` 默认值兜底：
- 没这个键 → 返回 `"[]"`（合法的空数组 JSON）
- json.loads("[]") → 返回 `[]`（空 Python list）
- 老数据优雅退化，新代码不挂

🎯 **核心原则**：**数据 schema 演进时，反序列化要能优雅退化**。这是 schema migration 的基础防御。

---

### Q4：`save_exemplar` 和 `retrieve_exemplars` 都用 try/except 包主逻辑，失败 print 到 stderr 但 `return None`，为什么？

💡 **答案**：**Best-effort side-effect** —— exemplar 存储是辅助优化，**绝不能让它害死主任务**。

**反例**：如果让异常抛出去：
```
主任务（取数 + 报告）已经成功
    ↓
save_exemplar 抛异常（磁盘满 / ChromaDB 崩）
    ↓
graph.invoke 抛异常 → CLI 显示"❌ 失败"
    ↓
用户困惑："到底成功没？"
```

**周边功能的失败，不该让核心功能跟着死**。

**但又不完全静默** —— 我们 print 到 **stderr**：
- 用户：主任务成功 ✓（不被打扰）
- 运维：stderr 有 WARN，可被 grep / 日志系统抓

🎯 **判断准则**：这个操作如果失败，**用户的预期是什么**？
- 期望"必然完成"（付款、写订单）→ 让异常抛出
- 期望"尽力而为"（缓存、日志、exemplar）→ silent fallback

---

## Section B：State 集成

### Q5：State 字段为什么是 `list[dict[str, Any]]` 而不是 `list[Exemplar]`？

💡 **答案**：**LangGraph 的 SqliteSaver 用 msgpack 序列化 state 写到 SQLite**，自定义类型（dataclass / Pydantic）默认不支持序列化。

**如果用 list[Exemplar]**：
```
节点返回 {"retrieved_exemplars": [Exemplar(...), ...]}
    ↓
LangGraph 想 msgpack.dumps(state)
    ↓
遇到 Exemplar 对象不知道怎么办 → 抛 TypeError → 第一次跑节点就崩
```

🎯 **DTO 模式**（Data Transfer Object）：**跨边界用基础类型，模块内部用富对象**。
- 模块内（exemplar_store）→ Exemplar dataclass（有方法、有类型）
- 跨边界（state）→ dict（保证 msgpack 可序列化）
- 需要时反序列化回 Exemplar（在 planner_node 里做）

类比：邮政系统。家里写信用普通信纸（Exemplar），寄出去要装信封（dict），收件人拆信封再读信纸。

---

### Q6：为什么 `retrieved_exemplars` 不需要 `Annotated[..., operator.add]` reducer？

💡 **答案**：因为它**只有一个节点写入**（`knowledge_retrieval_node`），不需要累积。

**reducer 判定准则**（从 Part 1 复习）：
- 单写多读 → 默认覆盖语义已经正确，不需要 reducer
- 多写 + 想保留全部 → 需要 reducer（如 operator.add）
- 多写 + 只关心最新 → 不需要 reducer（默认覆盖）

**如果硬加 operator.add 会怎样？**
```
Query 1 跑完：累积 5 条 exemplar
Query 2 跑完：8 条
Query 100 跑完：几百条
```
prompt 越来越长 → token 爆炸 → LLM 注意力被稀释。

🎯 **YAGNI 原则**：现在不用就别加。哪天真要"累积+去重"，再写自定义 reducer。

---

## Section C：Graph 接线

### Q7：为什么 RAG + exemplar 检索塞进**同一个** `knowledge_retrieval_node`？不分两个节点？

💡 **答案**：**LangGraph 节点之间有"重大 overhead"**。

每次节点转换 LangGraph 内部要做：
1. 节点返回 dict + reducer 合并进 state
2. **Checkpointer 把整个 state 序列化（msgpack）写到 SQLite**
3. 路由判断
4. 从 Checkpointer 读出 state（反序列化）

**两次检索分两个节点 = 多一次 SQLite I/O 写盘 + 读盘**（每次几十毫秒，可观）。

🎯 **Cohesion 原则**："**会一起变化的东西，应该放一起**"。

RAG + exemplar 检索：
- 同一时机（plan 之前）
- 同一目的（给 planner 上下文）
- 同一失败模式（都用 silent fallback）
- 互相**没数据依赖**

→ 高内聚 + 无依赖，**应该放一个节点**。

**何时该拆**？
- 它们之间**有数据依赖**（A 决定 B 怎么跑）
- 它们的**失败需要不同恢复策略**
- 它们的**触发时机不同**

---

### Q8：`planner_node` 里把 dict 列表"重新拼"成 Exemplar 对象列表，为什么？直接传 dict 不行吗？

```python
exemplars = [
    Exemplar(
        user_question=d["user_question"],
        execution_plan=d.get("execution_plan", []),
        ...
    )
    for d in exemplar_dicts
]
```

💡 **答案**：**DTO 模式的"反向"** —— Section B 讲了"为什么存 dict"，这里讲"用的时候转回来"。

**3 个具体原因**：
1. `build_planner` 内部期望 `list[Exemplar]` 类型契约（IDE / mypy 强约束）
2. Exemplar 有 `.to_prompt_block()` 方法，dict 没有
3. 让模块内部代码**享受类型安全和方法**，不用处理裸 dict 的脏活

🎯 **DTO 模式是双向的**：
- **出门**（写入 state）：富对象 → dict（保证可序列化）
- **进门**（从 state 读出）：dict → 富对象（恢复类型安全和方法）

类比：ORM。数据库存的是行（dict），程序内部用 Model 对象。每次查询时框架帮你做"升维"。

---

### Q9：reject 路径**不存** exemplar，为什么？把它存进去（标 `quality=0`）作为"反例"不行？

💡 **答案**：**Exemplar 是 few-shot 的正样本**，不是中性数据点。

LLM 看到 prompt 里"参考查询"**默认假设它该模仿**。所以**只能存值得被模仿的样本**。

**反例进入 pool 会发生什么？**
```
LLM 看 prompt：
   # 历史相似查询参考
   ## 参考 [1]
   SQL：SELECT ... （这是个被 reject 的烂 SQL）
   
LLM 想："噢这是参考样本，我也这么写"
   ↓
LLM 学着写错 SQL ❌
```

**3 个具体理由**：
1. **语义反向** —— 反例混进正例池，LLM 会无差别学习
2. **存储浪费** —— ChromaDB 每条都要 embed
3. **隐式信任脆弱** —— "存了但相信我会过滤"比"根本不存"更容易出 bug

🎯 **如果真要做"对比学习"**（Q vs A 的好坏对照），那是另一个独立 feature，需要专门的 prompt 结构。**现阶段 KISS。**

---

## Section D：4 个优化的细节

### Q10：weighted_rank 公式 —— 一个我们一起发现的真实 bug

```python
def weighted_rank(item):
    chroma_rank, ex = item
    quality_factor = 100 / max(ex.quality_score, 1)
    return chroma_rank * quality_factor   # ❌ bug！
```

跟踪 3 个候选：
| 候选 | chroma_rank | quality | final_rank |
|---|---|---|---|
| A | 0（最相似）| 30（差）| **0** |
| B | 1 | 100（满分）| 1 |
| C | 2 | 60 | 3.33 |

A 排第一 ❌ —— **质量很差但因为 chroma_rank=0，final_rank=0 永远赢**。

💡 **bug 根源**：`0 × anything = 0`。最相似的样本变成绝对赢家，质量分被完全忽略。

**修复**：加 +1 偏置
```python
return (chroma_rank + 1) * quality_factor   # ✓
```

修复后：
- A: (0+1) × 3.33 = 3.33
- B: (1+1) × 1 = 2     ← 满分质量战胜次相似度 ✓
- C: (2+1) × 1.67 = 5

🎯 **这是 sort fusion 算法的经典坑** —— `0 × X = 0` 让其中一个维度退化。**修法是加偏置或换 RRF（Reciprocal Rank Fusion）**。

---

### Q11：upvote +5 但 downvote -10，为什么不对称？

💡 **答案**：**不对称代价 → 不对称响应**。

| | upvote 误判 | downvote 误判 |
|---|---|---|
| 直接后果 | 烂例进 pool | 好例暂时缺席 |
| 恢复成本 | **难**（污染传播）| 易（之后改回来）|
| 发现难度 | **难**（隐式劣化）| 易（用户立刻发现）|
| 业务影响 | **持续性**（每次检索都中招）| 偶发 |

**留住烂 exemplar 是 heavy loss**（每次检索被毒）；**错过好 exemplar 是 mild loss**（少用一次）。

**类比金融**：巴菲特 "第一原则别亏钱" —— 不要错过赚钱机会（mild penalty），不要亏钱（heavy penalty）。

**还有一层**：**信号强度不对称**
- upvote 信号弱（用户随手点赞多）→ +5 合理
- downvote 信号强（用户特意点踩少）→ -10 合理

🎯 **设计原则**：**"刹车要比油门重"**。这是 ML 评分系统里 asymmetric cost 的典型应用。

---

### Q12：`validate_all_stale_candidates` 为什么默认每周跑而不是每天 / 每小时？

💡 **答案**：**频率应该匹配数据变化的真实速率**。

**真实失效场景**：
- schema 迁移（部署级，1-2 周）
- 业务定义变更（需求级，月度）
- 数据源切换（罕见）

**这些都是"发布周期级"事件，不是"小时级"事件**。

**频率选择矩阵**：
| 频率 | 优点 | 缺点 |
|---|---|---|
| 每小时 | 实时 | **资源浪费**（同一批 SQL 重复跑）|
| 每天 | 较及时 | 仍然过度 |
| **每周** ⭐ | 匹配发布周期 | 最坏情况脏数据存活 7 天 |
| 每月 | 资源最省 | 脏数据可能传播太久 |

🎯 **进阶方案：组合用**
- 后台每周定时（主动维护）
- 检索时按需懒验证（兜底）：`last_validated_at > 60 天 → 实时验证再返回`

类比缓存系统：TTL + on-access validation。

---

## Part 5 总结：4 个 Section 走过来的概念

| Section | 核心概念 | 题数 |
|---|---|---|
| **A. 存储层** | Bounded Context / 序列化 / 幽灵字段 / Best-effort side-effect | 4 |
| **B. State 集成** | DTO 模式 / YAGNI 原则 / Reducer 判定 | 2 |
| **C. Graph 接线** | 节点粒度 / DTO 双向 / Few-shot 正样本语义 | 3 |
| **D. 4 个优化** | Sort fusion 公式 + bug / 不对称代价响应 / 频率选择 | 3 |

---

# Part 6：metadata_explorer.py 走读（5 个 Q&A）

> 这个文件的职责是给 LLM 提供"看清数据的眼睛"：list_tables / describe_table / sample_rows。
> 看似简单但有几个**安全 + 性能 + LLM 友好**的精巧设计。

## Section A：白名单防 SQL 注入

### Q1：为什么 `describe_table` 用字符串拼接 SQL，不用参数化查询？

```python
# 我们的代码
sql = f"SELECT * FROM {table_name} ..."
```

不是这样：
```python
# 想象的错误写法
con.execute("SELECT * FROM ?", [table_name])
```

💡 **答案**：SQL 引擎**不允许**对 identifier（表名、列名）参数化，只允许对 value（值）参数化。这是**所有主流 SQL 引擎**（PostgreSQL / MySQL / DuckDB / SQLite）的共同设计。

**为什么 SQL 这样设计？**
- SQL 解析器先把 SQL 变成 AST → 再填参数
- 如果允许 `FROM ?`，解析时不知道是哪张表 → AST 没法构造 → 后续优化（如索引选择）瘫痪
- 所以参数化只能做"叶子节点"（具体的值），不能做结构性元素

**能 / 不能参数化的位置**：
| 位置 | 能 ? 吗 |
|---|---|
| `WHERE x = ?` | ✅ 能（值）|
| `LIKE ?` | ✅ 能（值）|
| `LIMIT ?` | ✅ 能（值）|
| `FROM ?` | ❌ 不能（表名）|
| `SELECT ?` | ❌ 不能（列名）|
| `ORDER BY ? ASC` | ❌ 不能（列名）|
| `ORDER BY x ?` | ❌ 不能（关键字 ASC/DESC）|

🎯 **简化记忆**：**SQL 的"骨架"（结构）不能参数化，只有"骨架里塞的值"能参数化**。

---

### Q2：那必须字符串拼接，怎么防注入？

💡 **答案**：**白名单 + 字符串拼接**。

```python
def describe_table(table_name: str) -> str:
    valid_tables = _get_all_table_names()    # ← 拉合法表名列表
    if table_name not in valid_tables:        # ← 白名单校验
        return "[错误] 表不存在"
    sql = f"SELECT * FROM {table_name}"      # ← 通过校验后才拼
```

**LLM 传 `"customers; DROP TABLE x"`**：
- 在白名单里查 → 不存在
- 直接 return 错误，**根本不会拼进 SQL**

🎯 **白名单是 SQL 安全的通用模式**。任何需要"动态 identifier"的场景（动态表名、动态 ORDER BY 列）都用这套：
```python
ALLOWED_COLS = {"name", "age", "created_at"}
if order_col not in ALLOWED_COLS:
    raise ValueError("Invalid column")
return f"SELECT * FROM users ORDER BY {order_col}"
```

---

### Q3：`_get_all_table_names()` 每次调用都拉一次 information_schema，vs 启动时缓存一次，优劣？

💡 **答案**：经典 cache 权衡题。

| 方案 | 优点 | 缺点 |
|---|---|---|
| **静态缓存** | 0 ms 开销，性能好 | ⚠️ schema 变了不知道 |
| **动态拉**（我们的）| ✅ 永远最新，正确性高 | ~5-10 ms / 次 |

**量化**：单次查询本身要 100-500ms，动态拉的相对开销只占 5-10%，可接受。

**生产环境最优解：带 TTL 的缓存**
```python
_cache = {"tables": None, "expires_at": 0.0}
_TTL = 60   # 秒

def _get_all_table_names():
    now = time.time()
    if _cache["tables"] and now < _cache["expires_at"]:
        return _cache["tables"]
    # ... 重新拉 ...
    _cache["expires_at"] = now + _TTL
```

60s TTL 的效果：性能比纯动态好 60 倍，时效比纯静态好（最坏 60s 漂移）。

🎯 **判断准则**：
- 读多写少 + schema 稳定 + 性能敏感 → 静态缓存
- 读写都有 + schema 可能变 + 正确性优先 → 动态拉
- 中间态 → TTL 缓存

---

## Section B：输出格式 + 采样

### Q4：为什么 `list_tables()` 返回 Markdown 字符串，不返回 `list[dict]`？

💡 **答案**：**工具消费者是 LLM，不是程序员**。

**3 方面优势**：
1. **Token 效率**：list[dict] str 化约 280 tokens；Markdown 表格约 220 tokens（省 20-30%）
2. **LLM 训练数据匹配**：模型在大量 Markdown 文档上训练过，对它"母语级理解"
3. **信号密度**：Markdown 标题 / 表格让 LLM 有"视觉锚点"，能精准定位特定字段

**反例（list[dict] str 化后 LLM 看到）**：
```
[{'name': 'customers', 'type': 'table', 'rows': 99441, ...}, {...}]
```
Python repr 风格、单引号、紧凑无格式、不易扫读。

**正例（Markdown）**：
```
| 名称 | 类型 | 行数 | 业务说明 |
|------|------|------|----------|
| customers | 表 | 99,441 | 客户维度表... |
```
**真实可读的表格**，结构清晰。

🎯 **设计原则**：**工具返回值的格式应该匹配消费者**。LLM 时代，工具应该返回"**结构化文档**"而不是"**结构化数据**"。

---

### Q5：`sample_rows` 为什么用 `USING SAMPLE 5 ROWS` 而不是 `LIMIT 5`？

💡 **答案**：**LIMIT 5 是有偏切片，USING SAMPLE 是真随机**。

**LIMIT 5（不带 ORDER BY）**：扫到 5 行就停 → 通常是"插入顺序最早的 5 条"

```
Olist 的 orders 表：
LIMIT 5 → 全是 2016-09-04 上线第一周的订单
       → 看不到"高峰期数据"或"分布全貌"
       → LLM 误判数据集范围 / 时间跨度
```

**USING SAMPLE 5 ROWS**：横跨整个表的随机采样

```
返回:
  2017-08-12 ...   ← 高峰期
  2018-04-23 ...
  2017-01-09 ...
  2016-11-15 ...   ← 黑五前后
  2018-09-01 ...
```

**LLM 真正"看见"数据全貌**。

**DuckDB 的 USING SAMPLE 还有高级用法**：
```sql
USING SAMPLE 1%                          -- 百分比采样
USING SAMPLE 1000 ROWS (system)          -- 系统采样（按页随机，更快）
USING SAMPLE 5 ROWS (reservoir, 42)      -- 可重现的随机（带种子）
```

🎯 **设计核心**：**LLM 看到什么样的样本，决定它怎么写下游 SQL**。LIMIT 给的"早期切片"会让 LLM 产生系统性偏见。

---

## Part 6 总结：metadata_explorer 通关

| 主题 | 核心 |
|---|---|
| **白名单防注入** | SQL 不允许 identifier 参数化 → 必须白名单 + 字符串拼接 |
| **缓存权衡** | 静态快但 stale；动态正确但慢；TTL 缓存是工程最优 |
| **输出格式** | LLM 是消费者，Markdown > list[dict]（token 省 + 训练数据匹配）|
| **采样策略** | USING SAMPLE 防"早期切片偏斜"，比 LIMIT 更代表数据分布 |

---

# Part 7：python_sandbox.py 走读（4 Section / 8 Q&A）

> 这个文件比 metadata_explorer 难一档 —— 涉及**进程隔离 + 跨进程通信 + 输出捕获**三主题。

## Section A：为什么是 subprocess

### Q1：subprocess vs exec() 的安全差异？

`exec(code)` 在主进程执行，5 类危险：
1. **异常传染** —— LLM 代码崩 → 主进程跟着崩
2. **变量污染** —— exec 共享命名空间，可改主进程变量
3. **死循环卡死** —— 同步阻塞，没法 timeout
4. **资源耗尽** —— OOM 后 OS 杀整个进程
5. **全局状态污染** —— `sys.path` / `os.environ` 等被改

🎯 **subprocess 的核心价值 = OS 进程边界**：独立内存 / 全局状态 / file descriptor / cwd。两进程**只能通过明确的通信渠道**（stdin/stdout/file/env）交互。

---

### Q2：subprocess 不保护什么？

subprocess 是**进程级**隔离，不是**权限级**隔离。子进程仍然：
- 用同一 uid（你的用户）
- 用同一文件系统（能读你的 SSH key）
- 用同一网络栈（能 HTTP 出去）

**升级链路**：
```
Level 0: exec()                  ← 啥都不防
Level 1: subprocess              ← 我们当前
Level 2: + chroot                ← 文件系统视图
Level 3: Docker container        ← 文件 + 网络 + cgroups
Level 4: gVisor / Kata           ← 内核 syscall 隔离
Level 5: VM (Firecracker)        ← 完全虚拟化
Level 6: 跨主机 (E2B / Modal)     ← 物理隔离 + 云托管
```

🎯 **判定**：威胁模型决定升级时机。InsightPilot 单用户 demo → subprocess 够用；生产对外服务 → 必须 Docker。

---

## Section B：PRELUDE 注入模式

### Q3：为什么要 SANDBOX_PRELUDE 前导脚本？

防 5 个 LLM 高频 bug：
1. **matplotlib 必崩**：没 `matplotlib.use("Agg")` → 无 GUI 沙盒里跑 plt.savefig 直接崩
2. **pandas 截断**：没 `display.max_columns=None` → LLM 看到 `...` 误以为有更多列
3. **数据加载路径**：LLM 不知道 INPUT_DATA_PATH 在哪，可能写 `pd.read_csv("data.csv")` 这种乱猜
4. **嵌套结构**：query_results 是 list of dict（含 sql/columns/rows），LLM 容易写 `pd.DataFrame(query_results[0])` 错
5. **Warning 噪声**：DeprecationWarning 污染 stdout

🎯 **通用模式**：让"应该总是发生"的事情自动发生，不依赖调用方记得做（类似 Jupyter `%matplotlib inline`、Django shell_plus）。

---

### Q4：scipy / sklearn / seaborn 怎么办？三种策略组合

```
策略 1：Prompt 教 LLM 自取  ← 灵活，但可能漏
策略 2：PRELUDE 全 import   ← 启动慢，PRELUDE 臃肿
策略 3：Lazy auto-import    ← 高级，行为有"魔法感"
```

🎯 **生产实战**：组合用
- PRELUDE 预 import 高频（pandas / matplotlib）
- Prompt 教 LLM 长尾包按需 import
- Sandbox 环境**预装好候选包**（不然 import 也失败）
- 极致：Docker image 预装"标准数据分析栈"

---

## Section C：跨进程数据传递

### Q5：JSON 文件 + env var 比 stdin pipe 强在哪？

3 个理由：

1. **数据大小**：OS pipe buffer 默认 64KB，超过就死锁
2. **调试便利**：input.json 留磁盘可手工 cat 看
3. **管道死锁陷阱**（最深层）：
   ```
   父进程 write 到 stdin（buffer 满，阻塞）
   子进程刚启动，import pandas 中（耗时 ~500ms）
   子还没读 stdin
   → 死锁
   ```
   Python `subprocess` 文档明确警告。

🎯 **代码品味分水岭**：senior 知道 pipe buffer 限制和死锁陷阱，**默认写文件**；junior 用 stdin pipe，"反正小数据测试通过了"。

---

### Q6：为什么 input 文件删但 chart 文件留？

**Transient input vs persistent output**：
- input 是数据传输的 carrier，跑完即删（state["query_results"] 还有副本）
- chart 是用户最终交付物，Reporter 报告里要 `![]()` 引用

🎯 **通用模式**：CI/CD 构建产物 vs 缓存依赖、MapReduce shuffle vs 输出、ETL 临时表 vs 结果表 —— **看是否有下游消费者**。

实现细节用 `try/except FileNotFoundError`（EAFP 风格防 race condition）。

---

## Section D：输出捕获与错误处理

### Q7：为什么用集合差集检测新生成图表，不让 LLM 自报？

LLM 自报有 5 种"谎言"方式：
1. **完全编造** —— 说生成了实际没写 savefig
2. **忘 savefig** —— 用了 plt.show()（沙盒无 GUI 不工作）
3. **savefig 静默失败** —— 磁盘满 / 权限 / 文件名非法
4. **plt.close() 太早** —— 文件存在但内容空
5. **保存到错误路径** —— /tmp/xxx 我们扫不到

集合差集 `files_after - files_before` **统一防御所有这些**：
- 不读 LLM 说什么
- 扫**文件系统的实际变化**

🎯 **通用准则 "Verify Behavior, Not Declarations"**：
- 测试：assert 实际行为，不信 test 名字
- 安全：检查 ACL，不信用户声明
- 监控：测真实指标，不信 SLA 文档
- 分布式：health check 节点，不信状态消息

---

### Q8：SQL 错误分类器和 Python 错误分类器为什么对称设计？

两者都做同一件事：

```
原始异常 → 按类型分类 → 翻译成"消费者语言" → + 具体修复建议
```

🎯 **反映通用原则 "Errors as Teachers"**：

| 默认错误 | 教程式错误 |
|---|---|
| `ValueError: could not convert string to float: 'abc'` | "类型转换失败...修复建议：用 pd.to_numeric(x, errors='coerce')" |
| LLM 看不懂 → 盲目重试 | LLM 看到具体 next action → 一次修复 |

**对 ReAct Agent 至关重要**：
```
Thought → Action → Observation → Thought ...
                          ↑
              "观察"质量决定后续质量
```

**好工具用错误教用户成长，烂工具用错误惩罚用户**。Rust 编译器是这条原则的极致代表。

🎯 **paper 启发**：可设计实验测试 "Error Quality Effect"：
- Group A: Agent 收原始异常
- Group B: Agent 收教程式错误
- 测重试成功率差异 → 5-20%（实证可发 paper）

---

## Part 7 总结

| Section | 核心 |
|---|---|
| A. **subprocess 选择** | 防 5 类问题（异常 / 变量 / 死循环 / OOM / 全局状态）；不防文件 / 网络 / 同 uid |
| B. **PRELUDE 注入** | 防 5 个 LLM 高频 bug；包管理三策略组合 |
| C. **跨进程数据传递** | JSON 文件 + env var > stdin pipe（pipe 死锁陷阱）；transient input vs persistent output |
| D. **输出捕获** | "Verify behavior, not declarations"；"Errors as Teachers" |

---

# 通用工程 vs Agent 专有知识分类

> 这份笔记 70+ 个概念里，**70% 是通用工程**（任何项目都能用），**30% 是 Agent 专有**（LLM 时代新范式）。

## 🟢 通用工程知识（永远值钱）

### 系统设计 / 架构
- Bounded Context（不同生命周期用不同存储）
- DTO 模式（双向，跨边界 dict / 内层富对象）
- Hexagonal Architecture（内层业务，外层适配）
- 节点 / 服务粒度（高内聚 + 无依赖）
- Single Source of Truth（默认值集中定义）

### 数据 / 数据库
- SQL identifier vs value 参数化
- 白名单 + 字符串拼接（防注入）
- TTL 缓存权衡
- LIMIT max+1 边界探测
- USING SAMPLE vs LIMIT
- transient input vs persistent output

### 安全 / 防御
- 纵深防御（多层）
- 正则白名单
- subprocess 进程边界
- subprocess 限制（文件 / 网络 / uid）
- Best-effort side-effect

### 并发 / 多线程
- thread.join(timeout=N)
- 跨线程异常不传播
- daemon=True
- try / except / finally
- EAFP 风格

### 进程间通信
- subprocess pipe 死锁
- 文件 + env var 传数据

### 算法 / 工程心智
- Sort fusion / RRF
- 不对称代价 → 不对称响应
- YAGNI / KISS / EAFP vs LBYL
- 频率匹配数据变化速率

## 🔵 Agent 专有知识（这一波风口）

### LangGraph 特定
- Annotated + operator.add reducer
- add_messages 智能 reducer
- TypedDict 作为 State
- Checkpointer + interrupt()
- Conditional edges

### Few-shot / RAG
- Few-shot 正样本语义
- 检索式 in-context learning
- 质量评分加权 retrieval

### LLM 工具设计
- PRELUDE 注入模式
- Markdown 为 LLM 优化
- list[dict] 给 LLM 看
- Errors as Teachers
- Verify behavior, not declarations

### Agent 评估
- Round-trip filtering
- Asymmetric metrics（FP vs FN）

## 关键洞察

**Agent 专有知识 ≠ 独立**，几乎都是"通用知识 + 一点 LLM 特异性"组合而成：

```
PRELUDE 注入   = 子进程执行（通用）+ 防 LLM 漏掉（LLM 特异）
Errors as Teachers = 错误信息分层（通用）+ ReAct 循环（LLM 特异）
DTO 跨边界    = 序列化（通用）+ LangGraph state（LLM 特异）
```

🎯 **senior 工程师转 AI 比 junior 容易**，因为 70% 通用工程是地基。**没有地基，30% Agent 知识也搭不稳**。

5 年后 LLM 范式可能变，但通用工程那 70% **永远值钱**。

---

# Part 8：lang_tools.py 走读（3 Section / 6 Q&A）

> 这个文件是 InsightPilot 的"适配层" —— 把核心工具包装成 LangChain @tool 给 Agent 用。
> 短小但密度高：**Hexagonal Architecture / @tool 魔法 / 闭包工厂模式** 三个核心概念。

## Section A：适配层为什么存在

### Q1：为什么不直接在 duckdb_executor.py 上加 @tool 装饰器？

3 层"为什么"递进：

**表层**：格式转换
- 核心 execute_sql 返回 QueryResult dataclass
- @tool 包装版返回 string
- 这层做了 `result.to_llm_string()` 转换

**中层**：framework decoupling
- @tool 在核心 → 核心代码依赖 langchain_core
- 没装 langchain 的环境（CLI / 单元测试 / 纯 SQL 脚本）import 不动
- LangChain 改 API（历史改过 5+ 次） → 核心代码跟着改

**底层**：**核心代码应该比框架活得更长**
- 框架（LangChain / LlamaIndex / AutoGen）寿命：1-3 年
- 业务逻辑（SQL 执行 / 安全防御）寿命：永远
- → 业务不应依赖某个特定框架

🎯 **Hexagonal Architecture / Ports and Adapters 模式**：
- 内层（Domain）：业务逻辑，不依赖外部框架
- 外层（Adapter）：把外部框架适配到 ports
- 框架：可替换的"插件"

**真实收益**：
- LangChain 改 API → 只改 lang_tools.py
- 想换 LlamaIndex → 加一个 llamaindex_tools.py，核心零改动
- 单元测试 → 不用装 LangChain

---

### Q2：什么时候适配层是"过度设计"？

**判定矩阵**：
```
                    框架稳定          框架不稳定
项目短期/抛弃式      ❌ 不要适配层    ❌ 不要适配层
项目长期/重要        ⚠️ 看情况        ✅ 一定要
```

**过度设计的具体场景**：
- 黑客松 / 一次性脚本 / Jupyter notebook / PoC
- 单人 + 不开源 + "第二个使用者"永远不出现

**值得做的具体场景**：
- 生产服务、开源库、长期项目（>1 年）
- 框架本身不稳定（**LangChain 就是！**）
- 跨团队代码 / 作品集

🎯 **判定准则 "第二用户"**：
- 第一个用户 = 你自己（关心开发速度）
- 第二个用户 = 别人 / 未来的你（关心接口稳定）
- **只要"第二个用户可能出现"，就该考虑加适配层**

🎯 **YAGNI vs Hexagonal**：判断变化的概率 × 不抽象时的修改成本 vs 抽象层的维护成本。
LangChain 改 API 概率 ≈ 100% → 适配层稳赚不赔。

---

## Section B：@tool 装饰器的"魔法"

### Q3：@tool 装饰一个函数，LLM 实际看到的是什么？

LLM 不直接调 Python 函数，通过 OpenAI **function calling API** 交互。@tool 自动生成 **JSON schema**：

```json
{
  "type": "function",
  "function": {
    "name": "describe_table",
    "description": "(完整 docstring)",
    "parameters": {
      "type": "object",
      "properties": {
        "table_name": {
          "type": "string",
          "description": "(from docstring Args)"
        }
      },
      "required": ["table_name"]
    }
  }
}
```

**@tool 自动从函数提取**：
| 函数元素 | → schema 的什么 |
|---|---|
| 函数名 | `function.name` |
| docstring 全部 | `function.description` |
| 参数 + 类型注解 | `properties.{name}.type` |
| docstring `Args` 段 | `properties.{name}.description` |
| 没默认值的参数 | `required` 列表 |

🎯 **完整 Agent 对话流程**：
```
1. 启动：LangChain 把所有工具 schema 塞进 OpenAI tools 参数
2. 第一次 LLM 调用：LLM 看到工具菜单
3. LLM 输出 tool_calls：决定调哪个工具
4. LangChain 解析 → 调真实函数 → 拿结果
5. 第二次 LLM 调用：把结果作为 ToolMessage 塞回对话历史
6. LLM 继续思考 / 调下一个工具 / 给最终答案
```

**关键**：LLM 没"读你的源码"，只通过 schema 认识你的工具。

---

### Q4：docstring 写得好坏对 Agent 行为有多大影响？

3 种"docstring 不好"的影响：
1. **工具选择混乱**：list_tables / describe_table / sample_rows 名字相似 → LLM 全调一遍 / 选错 / 跳过
2. **输出形态不清楚**：LLM 不知道返回 list 还是字符串 → 下游 Action 错乱
3. **触发时机模糊**：LLM 在每个 ReAct 循环都重复调 → 死循环风险

**研究数据（Gorilla / ToolBench 等）**：
| docstring 质量 | tool selection accuracy |
|---|---|
| 最差（一句话）| ~50-60% |
| 中等（含参数说明）| ~70-80% |
| **最佳（4 段式）** | **85-95%** |

**多写 100 tokens，准确率提升 30%+** —— LLM agent 工程里**最高 ROI 的优化**。

🎯 **4 段式模板**：
```
[1] 一句话功能（What it does）
[2] 何时使用（When to use）  ← 解决工具选择混乱
[3] 返回（What it returns）   ← 解决下游消费
[4] Args 参数说明              ← 解决调用错误
```

🎯 **意识转变**：
```
传统 docstring：写给"5 年后的程序员"看
LLM 时代 docstring：写给"运行时的 LLM"看
→ docstring = 硬代码，不是软文档
```

---

## Section C：静态 vs 动态工具的设计选择

### Q5：闭包 + 工厂模式 —— execute_sql 静态、run_python 工厂的本质

**闭包基础**（用最简单的例子）：

```python
def make_multiplier(factor):       # 外层函数
    def multiply(x):                # 内层函数
        return x * factor           # ← 用了外层 factor
    return multiply

times_3 = make_multiplier(3)
times_5 = make_multiplier(5)

times_3(10)   # 30，因为 times_3 "记住了" factor=3
times_5(10)   # 50，因为 times_5 "记住了" factor=5
```

**闭包 = 一个函数 + 它"记住的"外部变量**。
两个函数代码一样，但记住的 factor 不同。

---

**两个工具的差异不是功能不同，是"运行时需要的状态"不同**：

```
execute_sql 运行时需要：
  - sql 字符串（LLM 当前调用传入）
  - DuckDB 连接（每次现开）
  → 无状态（stateless）

run_python 运行时需要：
  - code 字符串（LLM 当前调用传入）
  - sandbox 环境（subprocess 现开）
  - query_results（前序节点跑出的数据！）  ← 状态！
  → 有状态（stateful）
```

**run_python 的 query_results 不能让 LLM 传**：
- 数据可能 100KB+ → token 爆炸
- LLM 不需要看这数据来决定调谁
- 数据复制可能损坏

---

**三种方案对比**：
| 方案 | 评价 |
|---|---|
| LLM 当参数传 | ❌ token 爆炸、可能损坏 |
| 全局变量 | ❌ 不可重入、并发会冲突 |
| **闭包工厂** | ✅ 每次构建独立实例、并发安全 |

```python
def make_run_python_tool(query_results, captures):
    @tool
    def run_python(code, step_id):
        # 闭包：能访问外层的 query_results 和 captures
        sandbox_input = SandboxInput(
            code=code,
            query_results=query_results,   # ← 闭包记住的，LLM 不知道
        )
        ...
    return run_python
```

🎯 **判定准则**：**工具需要"调用时刻无关的"上下文 → 必须工厂**。

---

### Q6：captures 是什么？为什么解决了 chart_paths bug？

**背景问题**：LangChain 工具必须返回字符串给 LLM，但 graph 层需要结构化的 AnalysisResult（含 chart_paths / code / stdout）。

**曾经的方案（有 bug）：事后从 messages 重跑**
```python
for msg in messages:
    if msg.tool_call.name == "run_python":
        code = msg.tool_call.args["code"]
        result = execute_python(SandboxInput(code=code, ...))   # 重跑
```

**致命 bug**：
```
第一次跑（agent.invoke 内部）：
   生成 chart.png → chart_paths 检测：[chart.png] ✓

第二次跑（事后重跑）：
   savefig("chart.png") 覆盖文件
   files_after - files_before = []  ← 文件已存在，不算"新增"！
```

**修复方案：captures（在工具调用源头捕获）**

```python
captures: list = []
agent = build_analysis_agent(qr, captures=captures)
                                  ↓ 闭包捕获 captures
agent.invoke(...)
   ↓ LLM 调 run_python：
   工具内部 captures.append(result)   # ← 直接写到外面的 list

# 跑完后
analysis_results = [c for c in captures if c.success]
```

**关键 trick**：`captures` 是 mutable list（可变对象），闭包记住的是 list 的**引用**。工具内部 append → 外面看到同一个 list 变长。

🎯 **这个 pattern 的名字**：**Result Collector via Closure**
- 适用：工具有副作用（生成文件 / 改数据库），不能事后重跑
- 适用：工具返回值是简化版，需要保留完整版给上层
- **don't do work twice** 的工程美学

---

## Part 8 总结

| Section | 核心 |
|---|---|
| A. 适配层为什么存在 | Hexagonal Architecture / 业务逻辑比框架活得长 / "第二用户"判定 |
| B. @tool 魔法 | docstring 是 prompt（运行时 LLM 看的 JSON schema）；4 段式模板影响 30%+ accuracy |
| C. 静态 vs 动态工具 | 闭包基础（多 factor multiplier 类比）+ Result Collector via Closure 模式 |

---

# 全文总结：8 大 Part 概念地图

| Part | 主题 | 核心概念 |
|---|---|---|
| 1 | Reducer | Annotated + operator.add，覆盖 vs 累积 |
| 2 | 正则 | `^\s*(--[^\n]*\n\s*)*(SELECT\|WITH)\b` 拆解 |
| 3 | 多线程超时 | thread + join(timeout) + 邮箱 + daemon |
| 4 | execute_sql | strip / LIMIT max+1 / try-finally / DTO |
| 5 | Execution Memory | Bounded Context / DTO / Few-shot 正样本 / 不对称代价 |
| 6 | metadata_explorer | 白名单 / TTL / Markdown 输出 / USING SAMPLE |
| 7 | python_sandbox | subprocess 隔离 / PRELUDE / 跨进程通信 / Errors as Teachers |
| 8 | lang_tools | Hexagonal Architecture / @tool 是 prompt / 闭包工厂模式 |

每一章都对应**面试可讲的工程直觉 + 代码级踩坑经验**。

---

# 跨 Part 的"通用基础概念"

读完 8 个 Part 后，你会发现一些概念**反复出现**，这些是"工程通用语言"：

| 概念 | 出现的 Part | 说明 |
|---|---|---|
| **闭包**（closure） | Part 5, 8 | 函数 + 它记住的环境 |
| **DTO 模式** | Part 4, 5, 8 | 跨边界用 dict / 模块内用富对象 |
| **Hexagonal Architecture** | Part 5, 8 | 内层业务、外层适配 |
| **Best-effort side-effect** | Part 5, 7 | 周边失败不害死核心 |
| **EAFP vs LBYL** | Part 4, 7 | Python 异常驱动 vs 预查 |
| **Verify behavior, not declarations** | Part 7, 8 | 测真实结果，不信声明 |
| **不对称代价 → 不对称响应** | Part 5 | 刹车比油门重 |

理解这 7 个概念 = 理解所有 8 个 Part 的**70% 通用工程**。剩下 30% 是 LangGraph / LangChain 的特异性（reducer / Annotated / @tool / interrupt 等）。
