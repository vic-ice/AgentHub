# SYSTEM_ARCHITECTURE — 效果驱动单基线（2026-08-17）

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
| 外部搜索 | `SearchGateway` | 外部搜索结果与 books cache |
| Deep Research | `DeepResearchRunner / Research Orchestrator` | ResearchRun + evidence + report |
| Tool 执行 | `SystemRuntime` + external capability runtime | action receipt / execution ledger |
| SSE / Trace | Trusted publication / trace projection | 已提交 Journal + execution receipt；只读展示 |
| Provider 配置 | Model resolver/manager | provider configs |

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
### 3.3 普通 Chat / Search 推荐

```text
book_search_v1(mode=recommendation)
  → RecommendationService
  → MemoryReadGateway.profile_memories + ReadingService.reading_anchors
  → SearchGateway / books cache
  → RecommendationProjector（Shelf fail-closed）
  → 过滤 Shelf 中 want_to_read / reading / read / dropped 的已知书
  → 排序并返回证据
```

`mode=lookup` 用于明确查某本书，不套“新书推荐”过滤。Controller/API/Tool 不得自己复制约束、搜索、Shelf 过滤和排序链。

### 3.4 Deep Research

```text
用户显式 research_mode=deep_research
  → DeepResearchRunner / Orchestrator
  → 独立规划、检索、证据、验证、报告
  → ResearchRun
```

默认 `personalization_mode=off`，不读取 Memory 或 Shelf。只有显式个性化模式才通过 MemoryReadGateway 读取；不得隐式混入普通推荐画像。

### 3.5 读侧

- Memory 当前/历史/Profile：只经 `MemoryReadGateway`。
- Reading 当前状态/历史：只经 `ReadingService` 的 Shelf/Event read methods。
- Entity 当前属性：由 `MemoryReadGateway` 分发到 entity current facts。
- 普通推荐：只经 `RecommendationService`。
- Deep Research：只读 ResearchRun/evidence/report。
- API 只做协议校验和 DTO 映射，不自行重建领域查询。

## 4. Provenance / Source contract

合法来源：

- `user_message`
- `reading_event`
- `tool_execution`
- `admin_action`
- `correction`
- `system_derived`

Chat 使用已提交 conversation user event；Reading 使用真实 RecommendationEvent/interaction；管理端使用 MemoryManagementEvent。VersionStore 只校验 provenance、版本链、supersede、幂等和完整性，不重新理解证据文本。

## 5. Legacy 处置

| 组件 | 状态 | 约束 |
|---|---|---|
| `write_coordinator.py` / `MemoryCommitter` | deprecated compatibility | 当前 Chat/API/Tool/Agent Core 不得 import；不得加入 capability registry |
| `process_memory_write_request` 与旧 routing/planning | deprecated compatibility | 仅旧离线/兼容模块可见；生产 `SystemRuntime` 不注册、不执行 |
| `MemoryCanonicalizer` | admin 结构 adapter / shadow / offline eval | 无独立写入权；最终写入必须进 Gateway |
| `TurnFactCompiler._llm_facts` | standalone/offline | Controller 生产链不得调用；生产只使用 Controller assertions 的确定性适配 |
| legacy LangGraph book tools | compatibility | 当前 Chat 入口不调用；新推荐能力只扩展 RecommendationService |
| `MemoryOrchestrator` / legacy provider committer | deprecated internal compatibility | 不得从生产入口可达；不得成为新功能依赖 |

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

## 7. 新功能扩展位置

- 新 Memory 来源：扩展 provenance contract 和 MemoryWriteGateway adapter。
- 新阅读动作/状态：扩展 ReadingService 与 canonical schema；Controller 只输出新结构值。
- 新推荐策略：扩展 RecommendationService/Projector，不改 Controller/API。
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

`backend/tests/test_effect_driven_reading.py` 验证一次 Controller batch 可投影多个书籍动作、同书多断言合并、规则仅接受 canonical 枚举、所有 Shelf 状态不作为“新书”、Deep Research 默认不读画像。

`backend/scripts/verify_reading_effect_e2e.py` 使用真实 HTTP、真实 Controller 模型和 PostgreSQL，随机用户执行多组不同自然语言、多实体、多动作，以及 Bookshelf POST→PATCH；只读取最终 Shelf 与 Memory 验收，不匹配伪造回答文本。每次运行后只清理本次 mock user。

`backend/scripts/verify_bookshelf_read_e2e.py` 为当前 Shelf 读侧黑盒门禁：固定产品代码后，用 24 种自然表达、24 个隔离会话调用真实 Chat SSE；逐条断言权威 Shelf 总数/状态/评价、书名不重复、无“长期记忆”fallback，并读取 Trace 证明每轮恰好执行一次 `bookshelf_read_v1`。

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