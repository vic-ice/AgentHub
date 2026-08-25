# ADR-2026-08-26：恢复效果基线并隔离退化研究实验

- 状态：**Accepted / 生产基线约束**
- 决策日期：2026-08-26
- 恢复基线：`e746714`（`checkpoint: stabilize recommendation research and UX`）
- 恢复分支：`codex/effect-baseline-recovery`
- 否决实验分支：`codex/research-controller-experiment-archive`
- 否决实验提交：`211f61b`（`archive: preserve rejected goal research experiment`）

- 当前验收状态：**代码已恢复；当前环境效果验收未通过**
## 1. 决策摘要

生产开发回到 `e746714`。提交 `211f61b` 完整保留，确保代码、测试与失败实现
均可追溯，但它不是候选生产基线，不得整体 merge、rebase、cherry-pick 或按
“提交更新所以架构更新”的理由恢复。

以后若复用该实验中的某项能力，必须从 `e746714` 重新实现，并单独证明它同时：

1. 改善真实回答效果；
2. 不显著增加普通 Chat/Search 延迟；
3. 不建立第二套业务 Owner 或 Research Controller；
4. 在固定代码下通过换表达、换实体的黑盒 E2E。

## 2. 为什么否决 `211f61b`

该实验相对恢复基线涉及 55 个文件，新增 11205 行、删除 319 行。确定性测试
曾有 47 项核心测试和 238 项跨域测试通过，但真实效果验收失败，说明测试主要
证明了机制按设计执行，没有证明设计对用户有效。

### 2.1 普通 Chat / Search 实测退化

- 普通推荐真实耗时约 80–120 秒，明显失去交互性。
- 同一“某年值得阅读”表达曾在 `publication_year_intent=none` 与
  `published_in_range` 之间漂移。
- 更换参考书和措辞后，回答落入“可继续核验 / 没有找到足够可靠候选”的机械兜底。
- 候选出现弱相关或脏目录标题，逐主题搜索结果合并后缺少可靠的主题归属与覆盖。
- Tavily 额度不可用时虽降级到 DDGS，但豆瓣结果又被研究级 Evidence Admission
  全部拒绝，形成“Provider 找到、用户仍得到空结果”。

### 2.2 Deep Research 实测退化

一次真实书籍研究运行产生以下状态：

```text
source_count = 10
promising_candidate_count = 10
qualified_candidate_count = 0
stored_evidence_count = 0
frontier_growth = false
goal_research_error = "goal research model unavailable"
```

最终报告没有交付任何书目。首轮噪声候选包括企业预算管理类书籍，后续 Gap
又围绕这些错误候选扩张；第二轮退化为拼接长篇 facet 说明，而没有发现新的
专业术语、实体或搜索空间。这违反了“Goal 不可变，但 Research Model 可以成长”
以及“缺证据不等于不符合”的产品原则。

### 2.3 架构原因

- 普通推荐被扩展成 Controller → 多 Query → 多 Provider → Goal Research Model
  → Query Frontier → 逐本验证 → Evidence Admission → Presenter 的长链。
- Deep Research 已有 ObjectivePlanner/Orchestrator，RecommendationService 内部
  又运行一套完整 Goal Research，形成重复规划、重复 Gap 与重复停止条件。
- 研究目标的详细解释被直接拼入 Provider Query，验收条件替代了高召回搜索词。
- Search source、candidate lead、admitted evidence 和 publishable conclusion 没有
  分层；弱来源既不能成为结论，也不能有效推动下一轮。
- Publication Guard 拒绝自然稿件后，确定性 fallback 质量不足，向用户暴露审计语气。
- 路由/步骤完成被当作 E2E 通过，未要求候选增长、证据沉淀、答案有效与延迟可接受。

### 2.4 回退后的当前环境复测（2026-08-26）

恢复分支加载 `e746714 + 本 ADR/门禁` 后，立即运行该基线自带的真实
`verify_recommendation_chat_e2e.py`，结果为失败：

```text
elapsed_ms = 124860
operations = [bookshelf_read_v1, book_search_v1,
              bookshelf_read_v1, book_search_v1, bookshelf_read_v1]
expected book_search_v1 count = 1
actual book_search_v1 count = 2
```

因此 `e746714` 是相对于 `211f61b` 的恢复与对照锚点，不是当前环境已经通过
效果验收的最终生产版本。普通推荐门禁失败后没有继续运行高成本 Deep Research
E2E，避免用额外时间掩盖已明确的首要失败。后续任何“已恢复正常”结论必须引用
新的冻结代码黑盒结果，不能只引用提交名或历史测试。

## 3. 恢复后的权威边界

### 3.1 普通 Chat / Search


```text
一次 Controller 语义理解
  → 唯一 RecommendationService / SearchGateway
  → 一次有界发现（确有覆盖缺口时最多一次定向补搜）
  → Shelf / Memory 排除与排序
  → 基于可信结果的自然表达
```

普通链路禁止启动 GoalResearch、QueryFrontier、ResearchRun 或第二个 Research
Controller。多源能力属于 SearchGateway 内部 I/O 策略，不等于增加规划轮次。

### 3.2 Deep Research

```text
显式 Deep Research
  → 唯一 Research Orchestrator
  → Goal / Frontier / Gap / round budget
  → 调用领域 Owner 执行检索或取证
  → Evidence Admission
  → ResearchRun + 用户回答
```

Research Orchestrator 是研究轮次、Frontier、Gap 和停止条件的唯一 Owner。
RecommendationService 可以作为图书领域执行能力被调用，但不得在 ResearchRun
内部再启动第二套完整研究控制器。

### 3.3 来源分层

```text
Search source → candidate lead → verified evidence → publishable conclusion
```

- 搜索片段或书评可以作为 lead，推动候选登记与下一轮 Query。
- 只有满足证据准入的内容才能支撑强结论。
- 缺证据的候选保持 promising，并触发定向核验；不得直接等价成 rejected。
- 明确反证才淘汰候选。
- Provider 失败或额度耗尽必须降级，不得把内部错误码写进答案。

## 4. 明确禁止重新引入

以下任一项出现即视为基线回归，不能以“研究更严谨”为理由豁免：

1. 普通 Chat/Search 默认进入多轮 Goal Research 或 Query Frontier。
2. DeepResearchRunner 与 RecommendationService 同时拥有完整 Frontier/Gap/停止条件。
3. 将 facet 定义、解释段落或验收说明原样拼成 Provider Query。
4. 用第一轮候选生成的 Gap 占满 Frontier，阻止发现新术语和新实体。
5. 把 `missing evidence` 直接映射为 `not qualified` 并向用户返回空答案。
6. 普通搜索复用研究报告级证据门槛，造成“搜到但不可说”。
7. Presenter 失败后返回回执、审计、证据准入或“可继续核验”式系统语气。
8. 仅以单测、路由完成、ResearchRun completed 或步骤数证明效果通过。
9. 为通过示例而硬编码书名、句式、关键词路由或固定答案。
10. 未测量真实端到端延迟就增加模型调用、Provider fan-out 或研究轮次。

## 5. 未来改动的晋级门槛

候选实现必须在同一份冻结代码上完成黑盒矩阵，运行过程中不得修改代码后继续
累计通过次数。

### 普通 Chat / Search

- 至少覆盖阅读年份、明确出版年份、相似书、排除参考书、多个软主题、严格交集。
- 换表达和换实体后，结构化意图、Shelf 排除和结果类型保持稳定。
- 回答包含可用候选与逐项理由，不以错误码、研究回执或空泛兜底代替。
- 记录端到端耗时，并与 `e746714` 同环境基线比较；没有明确效果收益时不得接受延迟回归。

### Reading / Memory / Bookshelf

- 一句话多书、多状态、多评价只做一次 LLM 语义理解。
- 最终 Shelf、Reading Event 与 MemoryWriteGateway 结果全部正确。
- 当前书架读取只经 ReadingService，书名不重复，状态和评价完整。

### Deep Research

- 答案型基准必须产生真实候选和至少一条已接纳证据，不能只验证路由和步骤完成。
- Trace 必须证明 Frontier 出现新术语、新实体或明确缩小的 Gap；重复原 Query 不算增长。
- 同时测试 Provider 部分失败与 Research Model 不可用的降级结果。
- 允许诚实的部分结论，但 `promising > 0` 时不得无解释地发布为“什么也没找到”。
- 默认保持 conversational；正式报告只由用户显式选择。

## 6. 实验代码的使用规则

查看否决实验：

```bash
git show --stat 211f61b
git diff e746714..211f61b
```

禁止直接执行：

```bash
git merge codex/research-controller-experiment-archive
git cherry-pick 211f61b
```

如需复用某个想法，应在恢复分支新写最小实现，并在 PR/提交说明中引用：

- 原实验文件与提交；
- 本次只吸收的具体能力；
- 为什么现有 Owner 无法承担；
- 黑盒效果对比与延迟对比；
- 为什么没有恢复被否决的第二套 baseline。

## 7. 文档优先级

新会话按以下顺序判断真实生产基线：

1. `docs/SYSTEM_ARCHITECTURE.md`；
2. 本 ADR；
3. 当前生产分支上的架构测试；
4. 冻结代码后的真实黑盒 E2E 结果。

提交时间新于 `e746714` 不代表它自动成为架构基线。`211f61b` 的存在只用于
留痕和复盘，不构成生产授权。
