# SYSTEM_ARCHITECTURE — 效果驱动单基线（2026-08-23）

> **2026-08-26 效果基线裁决：** 当前代码恢复与对照基线为 `e746714`；
> 它不等于已重新通过当前环境的效果验收。
> `codex/research-controller-experiment-archive` / `211f61b` 是经真实 E2E
> 否决的研究控制器实验，只能用于分析或选择性重做，禁止整体合并、禁止作为
> “更新架构”恢复到生产。决策证据、禁止项和重新晋级条件见
> [ADR-2026-08-26：恢复效果基线并隔离退化研究实验](./ADR-2026-08-26-EFFECT-BASELINE-RECOVERY.md)。

本文件是生产架构的权威说明。新功能先找到下表中的唯一 Owner 并扩展它；禁止从 Controller、API、Tool、后台任务直接拼装第二条完整业务链。

## 1. 不变原则

生产调用统一遵循：

```text
用户 / Chat / API / Tool / 后台任务
  → Semantic / Intent（每个任务最多一次 LLM 语义理解）
  → 唯一 Domain Service / Gateway
  → 内部业务组件
  → Runtime / Store / Provider
  → PostgreSQL / 外部系统
```

- LLM/Controller 只理解用户意图，可在一次输出中给出 0..N 个结构化业务动作。
- 规则只校验结构化枚举、来源、权限和完整性；不得用关键词规则补造或改写业务语义。
- Service/Gateway 拥有业务决策、编排、事务边界和跨组件顺序。
- Store/Provider 只做版本、持久化、完整性与外部 I/O，不导入 LLM、semantic interpreter 或 canonicalizer。
- 兼容代码允许保留能力，但不得从当前生产入口可达，也不得独立完成同一业务链。

## 2. 领域唯一 Owner 与 Source of Truth

| 领域 | 唯一业务 Owner | 权威读模型 / Source of Truth |
|---|---|---|
| Chat | `ChatService → AgentChatEntry/AgentControllerGateway` | Conversation Journal |
| Semantic / Intent | Agent Core `ControllerClient` | 当前 Controller 的结构化输出 |
| Memory 写 | `MemoryWriteGateway` | `memory_events` 的 current version heads |
| Memory 读 / Profile 聚合 | `MemoryReadGateway` | Memory current/history/Profile projection |
| Entity 绑定 | `EntityResolver`（Memory Gateway 内部组件） | entity + alias + entity current facts |
| Reading 写 | `ReadingService` | `user_book_shelf` 当前行；RecommendationEvent 为历史审计 |
| Reading 读 | `ReadingService` Shelf/Event read methods | Shelf / Reading Events |
| 普通推荐 | `RecommendationService` | Search candidates + Memory constraints + Shelf projection |
| 外部搜索 | `SearchGateway`；图书目录 Provider 为 `DoubanCatalogProvider` | 外部搜索结果与 books cache |
| Deep Research | `DeepResearchRunner / Research Orchestrator` | ResearchRun + evidence + report |
| Tool 执行 | `SystemRuntime` + external capability runtime | action receipt / execution ledger |
| SSE / Trace | Trusted publication / trace projection | 已提交 Journal + execution receipt；只读展示 |
| Provider / Model 配置 | Model management API + `ModelManager` | PostgreSQL provider/model rows；`ModelManager` 是已提交配置的运行时缓存 |

## 3. 权威生产主链

### 3.1 Chat 中一次理解、多动作落地

```text
/chat/invoke 或 /chat/stream
  → ChatService（先提交 user conversation event）
  → AgentControllerGateway
  → ControllerClient：一次理解整条自然语言
       输出完整 assertions[] / capability batch
       例：A=read + disliked；B=reading
  → WorkflowCompiler（只编排，不重新理解）
  → SystemRuntime
  → execute_remember_memory
  → compiled_turn_from_assertions（确定性结构适配，不调用 LLM）
  → MemoryWriteGateway
       ├─ reading facts → EntityResolver → ReadingService
       │    → Shelf + RecommendationEvent + compatibility interaction
       │    → MemoryWriteGateway.record_reading_memory
       │    → VersionStore → PostgreSQL
       └─ other facts → EntityResolver → VersionStore → PostgreSQL
  → trusted receipt publication
```

同一句可同时包含多本书、多维度、混合状态和评价。下游若收到非 canonical 枚举只能拒绝/澄清，不能用中文或英文关键词再次猜测。

非 Reading 事实以 Controller 已输出的 `domain / kind / predicate` 为当前写协议，Gateway 负责实体绑定与稳定 `memory_key`；它不从原始文本重猜语义。`MemoryReadGateway + VersionedMemorySearch` 同时按结构化 predicate 和旧 canonical schema 做协议归一化，保证新 Gateway 行与历史行都可读。遗忘动作由 `MemoryWriteGateway.forget_targets` 将 Controller/admin 的结构化 predicate + identity 对齐到当前 version head，再交给 VersionStore 写 tombstone；Chat/API 不得自行计算另一套 memory key。

### 3.2 前端书架与 Tool

```text
Bookshelf POST/PATCH / book feedback Tool
  → ReadingService
  → 同事务 Shelf + Reading Event（+ compatibility interaction）
  → 使用真实 event/interaction provenance
  → MemoryWriteGateway.record_reading_memory
  → VersionStore → PostgreSQL
```

ReadingService 不直接调用 Memory Provider。派生 Memory 必须重新进入 MemoryWriteGateway。DELETE 只表示移出 Shelf；如果未来要求同时遗忘 Memory，必须在 ReadingService/Gateway 中增加正式动作，不得在 Controller/API 写 SQL。

`ReadingService.ensure_backfilled` 使用按用户的事务级 advisory lock，并且写入口也必须先调用它。这样“首次 Chat 写 Shelf → 立即 GET Shelf”和并发首次访问只初始化一次，不会把刚写入的 Reading Event 再回放成重复 Shelf 行。

### 3.2.1 Chat 中权威书架读取

```text
用户询问自己的书架 / 数量 / 阅读状态 / 评价
  → Controller：一次语义理解，输出 bookshelf_read（含 canonical filters）
  → ProposalValidator：按 Read Owner 合并同轮重复 Shelf 调用
       ├─ 合并重复 statuses / evaluations
       ├─ 丢弃 current reading Memory 派生读与 query-only Memory fallback
       └─ 无效只读 fallback 仅在有效 Shelf Owner 已存在且无人依赖时隔离；其余 fail-closed
  → bookshelf_read_v1
  → ReadingService.ensure_backfilled + list_entries
  → Shelf current rows
  → deterministic Bookshelf renderer
```

`bookshelf_read` 是当前 Shelf 聚合的唯一模型可见入口，一次返回每本书的一行、状态和评价。`search_memory` 不能回答当前 Shelf；`book_search` 只搜索外部图书目录，不能读取用户自己的收藏。历史 Memory 查询和带明确非阅读 predicate 的复合 Memory 查询不会被 Shelf Owner 吞并。API/Controller 不得自行查询 `user_book_shelf` 或拼接 Memory 证据。
“已加入/已收藏/在书架”表达的是 Shelf membership，不等于 `read`；只有用户明确说已读完/读过才能输出 `statuses=["read"]`。这一边界由 LLM 在唯一语义理解中判断，下游不做关键词分流。
### 3.3 普通 Chat / Search 推荐

```text
Controller decision（一次，可输出至多一个 book_search recommendation）
  → BookSearchInput
      ├─ query / genres / audience：整体发现目标
      ├─ themes + theme_match：维度及 any/all 关系
      ├─ reference_titles：用户给出的比较锚点
      └─ excluded_titles：明确不应作为新推荐的作品
  → ProposalValidator
      ├─ 同批多个 book_search 合并为一次 Owner 调用
      └─ 隔离并行 web_search 推荐候选旁路
  → book_search_v1(mode=recommendation)
  → RecommendationService
      ├─ 一个完整目标主 Query；仅首轮零候选时允许一个补充 Query
      ├─ response_depth：quick / balanced / deep
      └─ theme_match 准入 + coverage 覆盖状态
  → MemoryReadGateway.profile_memories + ReadingService.reading_anchors
  → DoubanCatalogProvider → SearchGateway fallback / books cache（书目身份召回）
  → request exclusion（参考书/明确排除书，按标准作品标题归一化）
  → RecommendationProjector（Shelf fail-closed）
  → 按 book id、标准标题、明确版本后缀归一化过滤 Shelf 已知书
  → RecommendationService 内部 SearchGateway federated enrichment
      ├─ Tavily / DDGS / AnySearch 有界并发
      ├─ URL 规范化去重 + Provider 间轮询融合
      └─ balanced 前 2 本单 Provider / deep 前 4 本三 Provider 增强，其余保留目录事实
  → 排序并返回 BookEvidence
  → Response presentation（无 Tool schema，只能基于 trusted receipts 表达）
```

`mode=lookup` 用于明确查某本书，不套“新书推荐”过滤。`/books/search`、兼容 book Tool 也只能作为 RecommendationService adapter，不能自己复制约束、搜索、Shelf 过滤和排序链。普通推荐若需要时效检索，Provider fan-out 必须在 RecommendationService/SearchGateway 内部完成；Controller 并行 `web_search` 不能作为第二个未经过 Shelf/Memory 投影的候选入口。

`reference_titles` 与 `excluded_titles` 是正式业务契约，不是 Prompt 中的一句软约束。Controller 在唯一语义理解中填充字段，RecommendationService 在 Owner 边界执行排除；规则只对结构化标题做标准化比较，不从原始自然语言再次猜书名。来源标题中的明确页面后缀（例如 `- 读书`）在比较前移除，参考书因此不会重新进入候选集合。

`themes + theme_match` 是正式语义契约。Controller 在本轮唯一语义调用中输出简短主题，并用 `theme_match=all` 表达“每一本都必须同时覆盖全部主题”，用 `any` 表达替代项或均衡覆盖；Service 不得从原始文本重新猜该关系。RecommendationService 保留一个完整目标主 Query：`all` 不拆成多个主题 Query，避免把分别命中的候选错误拼成“同时满足”；`any` 只有在主 Query 零候选时才允许一个 `OR` 补充 Query，不能因为未达到候选上限就重复扩展。所有查询仍属于同一个 Owner 调用，随后统一去重、排除 Reference/Shelf/Memory 并做结构化主题准入。

`DoubanCatalogProvider` 只是只读目录执行器：接受已结构化查询，读取豆瓣公开书目建议数据并 allowlist 书名、作者、封面来源、出版年和 `/subject/<id>` 链接；复合前缀无结果或首批结果不足时，只做有限退避。它不读取原始用户消息，不选择推荐、不排序、不写 Shelf/Memory。目录身份优先于通用网页片段；目录召回失败时，通用 SearchGateway fallback 仍只接纳具体书目页。强儿童包装候选只有在结构化受众明确包含儿童时才能进入成人推荐，规则只做 fail-closed 校验，不替代语义理解。候选通过 Shelf/Memory、显式排除与主题合同校验后，RecommendationService 对排序最前的 balanced 2 本各使用一个最快可用内容来源并行增强；deep 才扩大到前 4 本、最多三个 Provider。目录发现本身仍为多源联邦搜索，未增强候选必须原样保留目录事实，禁止因预算截断最终书单或对每本无限扇出。SearchGateway 的 `federated` 策略对 Tavily、DDGS、AnySearch 做有界并发、过滤、URL 去重和轮询融合。远程封面进入 Shelf 后由 ReadingService 下载到应用缓存，长期展示不依赖豆瓣图床。

普通推荐的“多源化”是 RecommendationService 内部能力，不是 Controller 再规划多个 `web_search`。`response_depth`、`themes` 与 `theme_match` 来自本轮同一次 LLM 语义理解；Service 只执行结构化查询预算和候选校验，不根据原始自然语言二次判断。`coverage` 与 `theme_match` 必须进入可信 Response View：`all` 下缺任一主题的候选不准入，`any` 下某主题缺少候选则诚实说明，不能用另一个主题冒充。balanced 默认解释每本已准入候选；deep 还应给出比较、取舍和阅读顺序。所有理由只能来自 BookEvidence 中的目录字段与已接纳公开来源。

`publication_year_from/to` 是强契约：RecommendationService 必须把它们传入发现查询，并且只保留能从书目元数据或带“出版年/出版时间”等上下文的来源文本中核验的候选。搜索网页自身的抓取/发布日期不等于图书出版年份，二者必须分开存储。语言、类型、作者、受众若来源元数据不足，只能标注为 discovery constraint，不能宣称已严格核验。

普通外部能力采用固定两阶段协议：

```text
decision phase（有 Tool schema，理解一次并形成完整动作批次）
  → SystemRuntime 执行一次
  → response presentation phase（不暴露 Tool schema，不得再次规划或检索）
```

Response Presenter 可以使用 LLM 组织语言，但没有语义解释、候选选择、搜索或写入权。工具执行后，系统先将已签名的 Book/Web receipt 投影为紧凑 `ExternalAnswerView`，并把它作为表达模型唯一的用户可见事实输入；模型不再从嵌套运行时回执中自行寻找候选。发布策略把同一已接纳来源的无害 URL 变体归一到权威 URL，只移除真正未接纳的链接，不能因为引用格式差异丢弃整篇丰富回答。若表达模型失败、输出工具标记或仍未通过可信发布校验，再由同一用户视图确定性渲染自然回答；不得把回执、错误码、请求 ID、证据校验术语或 Provider dump 暴露到主回答。

### 3.4 Deep Research

```text
用户显式 research_mode=deep_research
  → DeepResearchRunner / Orchestrator
  → ResearchObjectivePlan（唯一语义理解，含 response_style / answer_depth）
  → 独立规划、SearchGateway federated 检索、证据、验证
  → Research Response Presenter
      ├─ conversational（默认）
      └─ formal_report（用户明确要求正式报告时）
  → ResearchRun
```

默认 `personalization_mode=off`，不读取 Memory 或 Shelf。只有显式个性化模式才通过 MemoryReadGateway 读取；不得隐式混入普通推荐画像。

Deep Research 是研究能力，不等于固定报告文体。默认回答必须先自然回应用户，再按内容使用少量 Emoji、短分组和来源；只有唯一语义计划明确输出 `response_style=formal_report` 时才能使用正式报告章节。`answer_depth` 默认 deep。ResearchObjectivePlan 固定 Goal、显式约束、种子实体与 2–3 个初始搜索方向，`candidate_titles` 在生产规划中必须为空：模型不得在检索前预猜 8–10 本书。第一轮从原始目标宽口径发现；每轮未准入来源仅作为 `discovery_lead` 提供给 Reviewer，用来登记新专业术语、新候选或新 Gap，不能进入 known facts 或单独触发 sufficient。Reviewer 新 Query 进入下一轮队首，因此第一轮不能锁死搜索空间。每轮通过同一 SearchGateway 做 federated 融合；单 Provider 失败只写 Trace。普通网页研究最多并发阅读前五个接纳页面；最终合成最多使用十八条材料。`publishable` 的 uncertain evidence 可以用“资料显示/可能/线索”保守呈现，不得支持确定评分、最优断言或强因果；不可发布证据仍被排除。证据不足和执行失败只用日常语言说明。

Research 证据覆盖率与回答交付状态是两个维度：只要存在可发布内容和至少一条已接纳证据，带诚实限制的部分回答就是成功交付，`objective_satisfied=false` 仅写入 ResearchRun/Trace；只有没有任何可发布内容或运行异常才标记失败。来源 URL 在发布前校验协议、主机和百分号编码完整性；畸形来源及其引用被丢弃，不得把错误码、请求 ID 或失败弹层拼入已有回答。

### 3.5 回答呈现边界

```text
Domain result / trusted receipts / ResearchBrief
  → Evidence policy（决定什么可以说）
  → Response View Model（用户可见事实）
  → Response Presenter（只决定怎么说）
  → Markdown / SSE
```

真实性校验只检查来源准入、候选身份、URL、技术泄漏和副作用声明，不得以“必须有固定标题/固定段落数”等规则绑死表达。Emoji、标题和列表为可读性服务，不能成为业务判断。前端只渲染已发布内容和进度事件，不自行拼装领域答案。

### 3.6 读侧

- Memory 当前/历史/Profile：只经 `MemoryReadGateway`。
- Reading 当前状态/历史：只经 `ReadingService` 的 Shelf/Event read methods。
- Entity 当前属性：由 `MemoryReadGateway` 分发到 entity current facts。
- 普通推荐：只经 `RecommendationService`。
- Deep Research：只读 ResearchRun/evidence/report。
- API 只做协议校验和 DTO 映射，不自行重建领域查询。

### 3.7 Model / Provider 配置发布

```text
/models 或 /models/connections 变更
  → CRUD 在请求事务中 flush
  → commit（数据库成为可见 Source of Truth）
  → ModelManager.refresh（用独立会话读取已提交状态）
  → 后续 Chat 立即使用新配置
```

禁止在 commit 之前刷新 `ModelManager`；否则 API 返回值会显示新默认模型，但真实 Chat 仍使用旧缓存。`backend/tests/test_model_configuration_cache.py` 将“先提交、后刷新”和“提交失败不发布缓存”作为常驻回归契约。

## 4. Provenance / Source contract

合法来源：

- `user_message`
- `reading_event`
- `tool_execution`
- `admin_action`
- `correction`
- `system_derived`

Chat 使用已提交 conversation user event；Reading 使用真实 RecommendationEvent/interaction；管理端使用 MemoryManagementEvent。VersionStore 只校验 provenance、版本链、supersede、幂等和完整性，不重新理解证据文本。

管理端 edit/forget 必须显式传 `source_kind=admin_action`。创建/修订判定、实际 `memory_key` 和 forget target 解析均取 Gateway 回执；API 只能把回执投影回 `MemoryManagementEvent`，不得在调用 Gateway 前复制这套业务判断。

## 5. Legacy 处置

| 组件 | 状态 | 约束 |
|---|---|---|
| `write_coordinator.py` / `MemoryCommitter` | deprecated compatibility | 当前 Chat/API/Tool/Agent Core 不得 import；不得加入 capability registry |
| `process_memory_write_request` 与旧 routing/planning | deprecated compatibility | 仅旧离线/兼容模块可见；生产 `SystemRuntime` 不注册、不执行 |
| `MemoryCanonicalizer` | admin 结构 adapter / shadow / offline eval | 无独立写入权；最终写入必须进 Gateway |
| `TurnFactCompiler._llm_facts` | standalone/offline | Controller 生产链不得调用；生产只使用 Controller assertions 的确定性适配 |
| legacy LangGraph book tools | compatibility | 当前 Chat 入口不调用；新推荐能力只扩展 RecommendationService |
| `MemoryOrchestrator` / legacy provider committer | deprecated lazy compatibility | 仅在显式读取兼容导出时惰性加载；导入当前 Memory 子模块不得加载它，不得从生产入口可达 |
| 旧 `verify_memory_*` / flowchart / book-turn / research-report 入口 | compatibility command aliases | 文件名可供旧 CI 调用，但只委托当前 Gateway/Agent Core/Recommendation/Research 验收，不再执行旧写链 |

“保留代码”不等于“保留权力”。架构测试锁定当前生产入口的禁入边界。

## 6. 禁止调用

- API/Controller/Tool/业务服务直接 `MemoryVersionStore.commit/forget`。
- MemoryWriteGateway 以外调用 VersionStore 写方法。
- Read Gateway 以外的业务层直接 import Memory Store/Provider。
- ReadingService 或 API 直接写 Memory Provider。
- 业务层 raw `INSERT/UPDATE/DELETE` 核心表：`memory_events`、`user_book_shelf`、`recommendation_events`、`book_interactions`。
- Store/Provider 导入 LLM、semantic interpreter、canonicalizer、turn compiler。
- 当前生产入口 import deprecated memory/routing baseline。
- 用关键词规则从原始自然语言恢复 Controller 遗漏的书名、状态或评价。
- 普通推荐复制第二套 Search + Shelf filter + ranking。
- Evidence/Store/Provider 直接生成面向用户的完整业务回答，或把回执、错误码、请求 ID 暴露到主回答。
- 用固定“研究结论/证据限制”模板绑死所有 Deep Research 输出；正式报告必须由一次语义计划显式选择。

## 7. 新功能扩展位置

- 新 Memory 来源：扩展 provenance contract 和 MemoryWriteGateway adapter。
- 新阅读动作/状态：扩展 ReadingService 与 canonical schema；Controller 只输出新结构值。
- 新推荐策略：扩展 RecommendationService/Projector，不改 Controller/API。
- 新回答风格：扩展 Response Presenter/View Model，不得在 Store、Provider、Controller 或前端复制业务判断。
- 新 Memory/Profile 查询：扩展 MemoryReadGateway。
- 新研究能力：扩展 Research Orchestrator/ResearchRun，不复用普通推荐的隐式画像。
- 新工具：只增加 capability schema + runtime adapter，adapter 必须调用领域 Owner。

## 8. 常驻防回归与验收

`backend/tests/test_memory_architecture.py` 使用 AST/import allowlist 检查：

1. production surface 不得导入 deprecated baseline；
2. 业务层不得直接 import Memory Store/Provider；
3. VersionStore 写入只能从 MemoryWriteGateway 发起；
4. 核心业务表不得出现 raw SQL 写；
5. Store/Provider 不得获得 semantic/LLM 依赖；
6. Reading 派生 Memory 必须引用 MemoryWriteGateway；
7. book runtime adapter 必须只调用 RecommendationService；
8. 管理端、Chat forget 与 Reading 写均经 Gateway/Service；
9. 当前书架读只经 `bookshelf_read_v1 → ReadingService`，不得回退 Memory；同 Owner 重复调用与派生读必须在 proposal 边界收口。

`backend/scripts/verify_r8_legacy_exit.py` 从 8 个真实生产入口构建传递 AST import graph，遍历所有可达的本地 `app.*` 模块；任何间接导入 retired runtime/orchestrator/routing baseline 都会使 CI 失败。函数内的显式 deprecated adapter 是惰性边，不计为模块初始化时的生产可达边。

`backend/tests/test_effect_driven_reading.py` 验证一次 Controller batch 可投影多个书籍动作、同书多断言合并、规则仅接受 canonical 枚举、所有 Shelf 状态不作为“新书”、Deep Research 默认不读画像。

`backend/tests/test_recommendation_control_flow.py` 与 `test_recommendation_architecture.py` 锁定：一次推荐只进入一个 RecommendationService Owner；完整目标主 Query 不按主题串行扇出；最多一次候选不足补充查询；`theme_match=all` 只准入真正同时覆盖全部结构化主题的候选；Web 不能绕开推荐投影；参考书/明确排除书在 Owner 边界过滤；出版年份真实执行；显式版本后缀仍按 Shelf 已知作品抑制；普通 RecommendationService 不得导入或调用 GoalResearch / QueryFrontier。

`backend/tests/test_external_search.py` 以行为测试锁定 federated Provider 的轮询融合与跨 Provider URL 去重；`test_recommendation_architecture.py` 以 AST 检查锁定生产 `book_search_v1` adapter 必须开启 RecommendationService 内部证据增强，且只能由该 Owner 创建 federated 内容搜索请求。`test_execution_progress.py` 锁定用户看到的是候选数、主题覆盖和公开页面数量，而不是 Provider 参数或内部回执。

`backend/tests/test_response_presentation_architecture.py`、`test_dr_reviewer.py`、`test_research_objective_planner.py`、`test_research_architecture.py` 与 `verify_research_publication_architecture.py` 锁定：证据投影不拥有搜索/推荐/写入能力；Deep Research 默认 conversational；ObjectivePlanner 不预选候选；Reviewer 发现的新 Gap 可扩展 Frontier，含书名的新 Query 不再自动降级为豆瓣单站；图书研究和通用研究都读取来源正文；搜索使用 federated；发布校验不以固定 Markdown 形状代替真实性校验。

`backend/scripts/verify_reading_effect_e2e.py` 使用真实 HTTP、真实 Controller 模型和 PostgreSQL，随机用户执行多组不同自然语言、多实体、多动作，以及 Bookshelf POST→PATCH；只读取最终 Shelf 与 Memory 验收，不匹配伪造回答文本。每次运行后只清理本次 mock user。

`backend/scripts/verify_bookshelf_read_e2e.py` 为当前 Shelf 读侧黑盒门禁：固定产品代码后，用 24 种自然表达、24 个隔离会话调用真实 Chat SSE；逐条断言权威 Shelf 总数/状态/评价、书名不重复、无“长期记忆”fallback，并读取 Trace 证明每轮恰好执行一次 `bookshelf_read_v1`。

`backend/scripts/verify_memory_admin_http_flow.py` 真实执行 admin edit → current → forget → history，验证 `admin_action` provenance、Gateway 生成的实际 key 和 tombstone 版本链。`backend/scripts/verify_recommendation_chat_e2e.py` 用真实推荐问法验证只有一个 `book_search_v1` 候选 Owner、没有并行 `web_search` 旁路且不返回轮数上限。`backend/scripts/verify_deep_research_routing_http_flow.py` 只通过显式 `research_mode=deep_research` 启动，并核对真实 ResearchRun、steps、evidence 和最终报告，不再验证已退役的 Chat 内 research tool baseline。

## 9. Book metadata and cover cache

Author and cover enrichment belongs to the Reading domain and does not add a second Shelf owner:

```text
Bookshelf GET
  → ReadingService.enrich_missing_metadata
  → book_metadata (SearchGateway discovery + Douban subject parsing)
  → download cover once to backend/data/book_covers/<sha256>.<ext>
  → ReadingService persists Book + UserBookShelf metadata
  → /api/v1/books/covers/<sha256>.<ext>
```

`book_metadata` is an internal I/O component only: it must not import SQLAlchemy, `Book`, or `UserBookShelf`, and it cannot persist business state. `ReadingService` remains the only component allowed to attach resolved metadata to Shelf rows. The database stores only application-owned cover URLs; frontend/API contracts must never persist or render a Douban image host as the long-term cover URL. Subject pages are provenance (`source_url`) rather than an image hotlink. Lookup is bounded, concurrent, fail-open, and versioned so a provider outage cannot block reading-state writes and a resolver upgrade can retry an old negative cache.

New metadata sources extend `book_metadata`; new Shelf decisions extend `ReadingService`. Do not add metadata writes in Controller, API, Tool, frontend, or Search Provider.
