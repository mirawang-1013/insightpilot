# InsightBench 论文计划

> 全职 solo 第一篇 arXiv + 会议论文计划
> 启动日：**2026-05-06**
> 目标完成日：**2026-08-15**（3.3 个月）

---

## 一、研究方向（草稿）

**工作标题**：
> *InsightBench: A Multi-turn Multi-dimensional Benchmark for Conversational Data Analysis Agents*

**核心贡献**（4 项）：
1. **Dataset** —— 200-500 个端到端数据分析任务（多轮、多难度等级 L1-L5）
2. **Evaluation Protocol** —— 多维度评估（计划合理性 / SQL 正确性 / 业务口径 / 图表恰当 / 结论质量 / 安全审批 / 效率成本）
3. **Failure Mode Taxonomy** —— 系统化的 agent 失败模式分类（提供研究路线图）
4. **Baseline Comparison** —— InsightPilot vs LangChain SQL Agent vs 纯 Prompting

**Differentiation**（和现有 benchmark 的区别）：
- Spider / BIRD 只测 SQL → 我们测整条 agent 流水线（plan + SQL + analysis + chart + conclusion）
- 现有 benchmark 都是单轮 → 我们覆盖多轮对话场景
- 现有英文为主 → 我们 bilingual（中 + 英），跟 chinese-agentic-eval 项目联动

---

## 二、纯时间投入（全职）

| 阶段 | 全职小时 | 全职周数 |
|---|---|---|
| 文献综述 | 60-80 h | 1.5-2 周 |
| 研究设计 + RQ 定稿 | 30-40 h | 1 周 |
| **数据集构造**（最累）| 80-120 h | 2-3 周 |
| Baseline 实现 + 实验 | 40-50 h | 1 周 |
| 图表 + Tables | 20-30 h | 0.5 周 |
| 写作 v1 | 80-100 h | 2-3 周 |
| 同行预审 + 改写 | 40-60 h | 1-2 周 |
| 打磨 + 投稿格式 | 20-30 h | 0.5-1 周 |
| 缓冲（必有）| 60-80 h | 1.5-2 周 |
| **总计** | **430-590 h** | **11-15 周（≈ 3.3 个月）** |

**心理预警**：
- 数据集标注那 2-3 周最容易让人放弃 → 设里程碑（每 50 个任务奖励自己）
- 写作 Week 1 会发现想得不清楚 → 这是正常的，预算里要留返工时间
- 修改阶段朋友的反馈可能撕碎你的论文逻辑 → 这是好事

---

## 三、详细时间表（从 2026-05-06 起）

| 阶段 | 起止日期 | 周数 | 关键产出 |
|---|---|---|---|
| 文献综述 + 研究设计 | 2026-05-06 → 2026-05-26 | 3 周 | RQ 定稿 + related work draft |
| 数据集构造 | 2026-05-27 → 2026-06-16 | 3 周 | 200+ 任务 + ground truth |
| 跑实验 | 2026-06-17 → 2026-06-30 | 2 周 | InsightPilot + 2 baselines 跑完 |
| Draft v1 写作 | 2026-07-01 → 2026-07-22 | 3 周 | 8 页全文 |
| 预审 + 改 | 2026-07-23 → 2026-08-08 | 2.5 周 | 找 2-3 朋友 review |
| 打磨投稿 | 2026-08-09 → 2026-08-15 | 1 周 | 准备投稿格式 |
| **完成** | **2026-08-15** | | 投 AAAI + arXiv |

---

## 四、会议投递计划

### 主投：AAAI 2027 ⭐⭐⭐

| | 日期 |
|---|---|
| 摘要 deadline | ~2026-08-08 |
| 全文 deadline | ~2026-08-15 |
| 通知 | ~2026-11 |
| 会议 | 2027-02（加拿大 / TBD）|
| 接受率 | ~22% |

**为什么是首选**：你 8/15 完成正好赶上；分量够（A* 会议）但比 NeurIPS / ACL 友好；范围广，agent / NLP / DB / system 都收。

### 并投：EMNLP 2026 Industry Track / Demo Track ⭐⭐⭐

| | 日期 |
|---|---|
| Industry / Demo deadline | ~2026-08 |
| 通知 | ~2026-09 |
| 会议 | 2026-11（中国 / TBD）|
| 接受率 | Industry ~40%, Demo ~50% |

**为什么并投**：
- 8 月 deadline 跟 AAAI 时间表完全吻合
- **2026 年内就能见结果！**心理 ROI 极高
- Industry / Demo 不算主会双投，可与 AAAI 同时投
- 必须确认每会议的 dual submission policy

### Fallback 1：SIGMOD 2027 Round 1

| | 日期 |
|---|---|
| Round 1 deadline | ~2026-09-15 |
| 通知 | ~2026-12 |
| 会议 | 2027-06（美国 / TBD）|
| 接受率 | ~20% |

**触发条件**：8/15 没赶上 AAAI，时间 +1 个月用在补实验 / 加任务上。SIGMOD 是数据库顶会，对 Text-to-SQL + benchmark 主题超合适。

### Fallback 2：ICLR 2027

| | 日期 |
|---|---|
| 摘要 deadline | ~2026-09-19 |
| 全文 deadline | ~2026-09-26 |
| 接受率 | ~32% |

**注意**：ICLR 偏理论 / 方法 novelty，benchmark 类会被诟病"贡献不足"。**优先级低**于 SIGMOD。

### Fallback 3：NeurIPS 2026 Workshops

| | 日期 |
|---|---|
| Workshop paper deadline | ~2026-09（不同 workshop 不同）|
| 篇幅 | 4-6 页 short paper |
| 会议 | 2026-12 |

**用法**：把主投 paper 的 4 页精简版本投相关 workshop（数据 / agent / foundation models 主题），增加 visibility。

### ❌ 不推荐尝试

- **NeurIPS 2026 D&B**（5 月 deadline，太紧）
- **EMNLP 2026 主会 / Findings**（6/15 deadline，赶不及）
- **CIKM 2026**（5 月 deadline 已过）

⚠️ **所有日期需在投递前去会议官网二次验证**。

---

## 五、推荐组合

### 组合 A（双投保险，强烈推荐）⭐⭐⭐

```
主投：AAAI 2027（8/15）
并投：EMNLP 2026 Industry Track（8 月）  ← 2026 年内见结果
arXiv：8 月底立即挂上
```

**优点**：学术分量足 + 当年内有反馈 + 保险

### 组合 B（数据库人设）

```
主投：SIGMOD 2027 Round 1（9/15）
并投：EMNLP 2026 Demo Track（8 月）
```

### 组合 C（保守 fallback）

```
只投：AAAI 2027 + EMNLP Demo + arXiv 早发
```

---

## 六、双投合规说明

学术界**禁止"同篇文章双投"**（同时投两个主会）。下面**是允许的**：
- 投会议 + 同时挂 arXiv（**普遍做法**）
- 主会拒稿 → 投 workshop 或下一个会议
- 投 demo / industry / system track（这些通常不算主会 paper）

**投递前必须**仔细看每个会议的 dual submission policy。

---

## 七、Claude 能帮你做的具体事

| 阶段 | Claude 角色 | 你的工作 |
|---|---|---|
| 文献综述 | 列出 30 篇候选 + 一句话摘要 | 读细节，写 related work |
| 研究设计 | brainstorm RQ，质疑方案 | 拍板 final design |
| 数据集 | 写标注规范 + 模板生成代码 | 实际标注（quality control 必须人做）|
| 实验代码 | 写 baseline + eval 框架 boilerplate | 跑实验、调参、debug |
| Draft 写作 | abstract / intro / related work；改语言 | method / experiments / analysis 主体 |
| 修改阶段 | 修语病、逻辑漏洞、figure 设计 | 接受 / 反驳建议 |
| 投稿前 | LaTeX 格式 / 引用 / 附录 verify | 最终拍板 |

❌ **Claude 不能做**：跑实验、标注 ground truth、做 co-author、保证最近研究都准确（可能漏过去 6 个月的论文）。

---

## 八、第一周建议安排（启动专用）

### Day 1（今天 2026-05-06）
- [ ] 30 分钟：和 Claude 一起写 RQ + Contributions 草稿
- [ ] 1 小时：列你已知的 5-10 篇相关论文

### Day 2-3
- [ ] Claude 给你列 30 篇候选 + 摘要总结
- [ ] 你按"必读 / 选读"分类

### Day 4-7
- [ ] 深读 10 篇必读论文，Notion / Obsidian 做笔记
- [ ] 期间随时找 Claude 讨论"这篇和我方向的关系"

### Week 1 末
- [ ] RQ 草稿 v1 出炉
- [ ] 30 篇相关工作整理完
- [ ] 进入 Week 2：实验设计

---

## 九、心理建设

### 第一次发论文，几个真相

1. **第一篇被拒概率 50%+** —— 拒稿是正常的，被拒后改一改转投下个会议，3 个月内能到下个 deadline
2. **不必追求完美再投** —— 8/15 有 draft 就投，reviewer 反馈让你成长更快
3. **不必同时投顶会** —— AAAI / EMNLP Findings / SIGMOD 已经分量足够
4. **arXiv 不是降级** —— 提前挂 arXiv 是建立 priority 的标准做法

### 你比一般博士生 / 工程师的 advantages

```
你比一般博士生多的：5 年大厂数据经验（业务感）
                  你已有 working system（InsightPilot）

你比一般工程师多的：NUS Master + 论文经历（学术品味）
                  chinese-agentic-eval 的 niche 占位

= 极适合做"工业级 evaluation paper"
  发出来对面试 + 名声 + freelance 接单都有杠杆
```

---

## 十、下一步立即行动

要做的第一件事：**写 Research Question + Contributions 草稿**。

骨架定下来后，后面 3 个月就有方向感。回头跟 Claude 一起做这件事。

---

## 附：参考资料 / 相关 benchmark（待 Claude 补全）

- Spider / Spider 2.0
- BIRD
- WikiSQL
- AgentBench
- TaskBench
- ToolBench
- AppWorld

（这部分等启动时 Claude 会列详细清单）
