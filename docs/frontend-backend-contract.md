# Frontend / Backend Contract (as-implemented)

This document is the authoritative frontend-backend alignment list for the
current codebase (branch `codex/r8-19-memory-arch`). UI refactoring must not
change these contracts; if a contract change is ever needed, list the blast
radius and rationale first.

## 1. Transport

- REST: `application/json`, base path `/api/v1` (FastAPI).
- Streaming: `POST /api/v1/chat/stream` -> `text/event-stream`.
- Dev proxy: frontend Vite dev server proxies `/api` to backend (default
  `http://127.0.0.1:8080`, overridable via `VITE_DEV_PROXY_TARGET`).

## 2. Common identity relationships

| Concept | Storage | Notes |
|---|---|---|
| user | `users.id` (UUID) | `?user_id=` query param on many reads |
| conversation / thread | `conversations.thread_id` (UUID) | one thread = one conversation |
| request | `request_id` | per turn, client or server generated |
| research run | `research_runs.id` | `research_runs.thread_id` links run to its thread; `research_runs.user_id` owner |
| journal event | `conversation_events.sequence_no` | per-thread monotonic sequence; messages are events |
| trace | `trace_executions` | execution DAG snapshot per request |

`UserInput` (POST body for chat):
`thread_id`, `user_id`, `request_id`, `content`, `model_uuid?`, `model_name?`,
`thinking_mode?`, `research_mode: "chat"|"deep_research"`, `timezone?`,
`options?`.

## 3. Chat

| Endpoint | Method | Purpose |
|---|---|---|
| `/chat/conversations` | GET | list conversations (`?user_id&limit&offset`) |
| `/chat/conversations` | POST | create conversation (`title`) |
| `/chat/conversations/{thread_id}` | DELETE | soft-delete |
| `/chat/conversations/{thread_id}/info` | GET | info |
| `/chat/conversations/{thread_id}/title` | GET/PATCH | title |
| `/chat/conversations/{thread_id}/title/generate` | POST | generate title |
| `/chat/history` | GET | messages (`?thread_id&user_id&...`) |
| `/chat/run/invoke` | POST | one-shot non-streaming turn (`UserInput` -> `ChatMessage`) |
| `/chat/run/stream` | POST | SSE turn (`UserInput`) |
| `/chat/stats` | GET | usage stats |

SSE events (data lines, JSON inside `data:`):
- `{"type":"token","content":"..."}` streaming text token
- `{"type":"message",...}`/`{"type":"done",...}`/`{"type":"error",...}` and
  related lifecycle events; stream ends with `data: [DONE]`.
- Thinking content and tool/step progress arrive as separate event types;
  frontend accumulates them for the "thinking / steps" panel.

Chat model flow: Controller proposes (direct answer / capability proposals /
clarification / task plan) -> Harness executes tools -> Trusted publication
commits one `assistant_published` event. Plain answers are validated by
`publication/policy.py` (no fabricated URLs, no empty text, no technical
leaks, no unbacked side-effect claims).

## 4. Capabilities (model tools via Agent Core)

Registered capabilities include: `web_search`, `book_search`, `weather_get`,
`remember_memory`/memory read/forget, `research_read`, `plan_task`,
`request_clarification`. Tool calls and receipts flow through
`agent_core/compiler.py` + `agent_runtime`; frontend receives them as step
events during streaming.

## 5. Deep Research

| Endpoint | Method | Purpose |
|---|---|---|
| `/research/runs` | GET | list runs (`?user_id`) |
| `/research/{run_id}` | GET | run + state (subquestions, known_facts, gaps, conflicts, evidence, steps) |
| `/research/{run_id}/report` | GET | final `ResearchReport` |
| `/research/start` | POST | start a run (used by explicit flows) |
| `/research/search` / `/research/visit` / `/research/evidence` | POST | research loop internals |
| `/research/state` / `/research/finish` | POST | state update / finish |

Primary user flow: frontend sets `research_mode: "deep_research"` on the
stream request. Backend runs `deep_research_runner.run_deep_research_turn`
(creates `research_runs` row, multi-round loop: search -> garbage gate ->
dedup -> claim extraction (booklist-aware) -> admission -> reviewer gap loop
-> final report writer), commits the report as the assistant message, and
exposes the run for later inspection.

- `ResearchRun` = persisted run (objective, budget, stop criteria, metadata,
  evidence, state snapshots).
- `Journal` = `conversation_events`; the final report text is a normal
  assistant message.
- `research_read(run_id, scope)` = capability the chat model can call to pull
  report/findings/sources/evidence on demand; chat context only holds a
  compact pointer, not the full report.

Report writer contract (final output): single model call; final answer taken
directly from the model text channel (bare-string final block supported);
editorial persona + Markdown + emoji + comparison tables encouraged; only
empty/exception falls back to deterministic publisher.

## 6. Memory

| Endpoint | Method | Purpose |
|---|---|---|
| `/memory/current` | GET | current memory facts (`?user_id&thread_id`) |
| `/memory/history` | GET | memory version history |
| `/memory/edit` | POST | create/revise fact (structured assertions) |
| `/memory/forget` | POST | forget |

- Write path: model returns structured semantic assertions
  (`MemoryAssertionProposal`: subject/predicate/value/qualifiers/evidence_quote)
  -> canonicalizer maps predicate to schema -> write_gate validation ->
  version store -> vector recall index.
- `entity.name` is a storage-protocol schema (registry + write_gate +
  version_search + entity_normalizer); entity naming facts are written from
  the model's structured output (`predicate=entity_name`), not from
  rule-based NL guessing.
- Read path: current facts + versioned history + semantic recall.

### 6.1 Unified domain + kind classification (frozen 2026-08-14)

- Every memory fact now carries two classification layers under one Memory:
  - `domain`：`reading / personal / possession / relationship / plan / general`
  - `kind`：`preference / state / feedback / fact / agreement / correction`
- `MemoryAdminFact` (GET `/memory/{user_id}/current` and `/history`) includes
  `domain` and `kind`; the memory panel should group by domain first, then kind.
- Reading reuses the same memory store (`memory_events`) with `domain=reading`
  (state/feedback kinds); **no separate ReadingMemory is created**.
- **Classification boundary**: for NEW user messages the LLM semantic
  interpreter directly proposes `domain` + `kind`; `classification.py` only
  validates enums, keeps legacy-field compatibility, and provides a
  deterministic fallback (it never re-guesses semantics from keywords/subject).
- `general` is a legitimate domain and is allowed; it alone never triggers a
  clarification.
- Chat write path adds a **Clarification Gate** before
  MemoryAdmission -> ConflictResolver -> Postgres. It only blocks on
  meaning-changing ambiguity (unknown object/state, LLM confirmation request,
  very low confidence). Blocked turns return `clarification_required` with a
  question and persist nothing.
- A model may never claim it remembered something without a real tool receipt:
  only `MemoryWriteOutcome` (committed / clarification_required) authorizes the
  published answer.

### 6.2 Entity model (frozen 2026-08-14)

- **Entity 定义**：具名、稳定、可跨轮次指代的对象（人/宠物/书/物品/地点/账号/项目）。
- **受控实体类型**：`book / person / pet / object / place / account / project / other`
  （`MEMORY_ENTITY_TYPES`）。模型按“对象本身是什么”判定，禁止按谓词/字段名猜：
  “我的猫叫咪咪”→ `pet`；“我在读《三体》”→ `book`；“我买了台电脑”→ `object`。
- **entity_type → domain**：book→reading；person/pet→relationship；object/account→possession；
  place→personal；project→plan；other→general。domain 由模型输出的实体类型推导。
- **指代消解**：它/这本/那只/这个人必须解析到唯一实体；解析不了 → `needs_confirmation`，
  由 Clarification Gate 追问，不落库。
- **非实体事实**：抽象偏好/情绪/临时状态不强造实体，domain=general/personal + 对应 kind。
- 实体事实（state_value 含 `entity`）缺少 `entity_type` 时，Clarification Gate 追问对象类型
  （书/人/宠物/物品/地点/账号/项目），避免“代指书还是动物”这种歧义入库。

### 6.3 Entity-centric turn pipeline (frozen 2026-08-14)

- `memory_entities` (user_id + entity_type + canonical_name + aliases + domain)
  is the stable entity registry; entity facts bind `entity_id` on `memory_events`.
- **TurnFactCompiler** runs before any write: deterministic reading pre-pass
  (book feedback always yields a book fact) + LLM 0..N atomic facts with
  entity/entity_type/domain/kind/attributes.
- **EntityResolver** maps one entity expression to existing / new / ambiguous:
  same type+canonical name reuses the id; aliases match; multiple hits or
  missing type -> clarification with ZERO write.
- Routing: reading facts -> ReadingService (bookshelf + audit + memory);
  other entity facts -> MemoryCommitter with entity_id bound; ambiguous ->
  Clarification Gate question, nothing persisted.
- Query: “我读了哪些书/读过什么” routes to `recommendation_history`
  (bookshelf + audit), not generic memory search.
- Acceptance: clear semantics auto-write; ambiguous zero-write; multi-fact
  preserved; same entity reuses one id; new entity facts always carry
  entity_id/domain/kind/entity_type (no NULL).

## 7. Models & Providers

| Endpoint | Method | Purpose |
|---|---|---|
| `/models` | GET/POST | list/create model configs |
| `/models/available` | GET | active models for UI |
| `/models/{id}` | PATCH/DELETE | update/delete |
| `/models/default` / `/models/default-thinking` | PATCH | set defaults |
| `/models/{id}/validate` / `/models/{id}/capability` | POST/GET | validation/probe |
| `/provider-configs` | GET/PATCH/POST | provider configs & health |

Model routing: DB-backed `models` table (provider/model_id/thinking/active/
default/connection_id) -> `ModelManager` cache -> `get_llm()` builds a
ChatLiteLLM (DashScope / OpenRouter / OpenAI-compatible). Thinking mode is a
per-model flag; DashScope qwen forces thinking=True.

## 8. Auth

`/auth/status`, `/auth/mock-users`, `/auth/mock-login`, `/auth/logout`
(mock/local auth for development; `users` table).

## 9. Other APIs

- `/books/*`: legacy book features (read-only usage in some views).
- `/traces/*`: execution DAG traces.
- `/weixin`: WeChat channel webhook.

## 10. Frontend dependency map (src/lib/api.ts -> endpoints)

`listConversations/loadMoreConversations/createConversation/deleteConversation`
-> `/chat/conversations*`; `getHistory` -> `/chat/history`;
`invoke/streamChat` -> `/chat/run/invoke|stream`; `getMemoryCurrent/History`,
`editMemory`, `forgetMemory` -> `/memory/*`; `listResearchRuns`,
`getResearchState`, `finishResearch`, `cancelResearchRun` -> `/research/*`;
`getAvailableModels/getAllModels/createModel/updateModel/...` -> `/models*`;
provider configs -> `/provider-configs`; auth helpers -> `/auth/*`.

## 11. Refactor guardrails

- Do not change backend contracts while refactoring UI.
- Preserve: plain chat, streaming events, web_search, Deep Research loop,
  Reviewer/Evolving Report/Subquestion Queue, final report output quality,
  ResearchRun persistence + research_read, Memory write/version/recall,
  entity.name structured storage.
- If a contract gap is discovered, write it here with impact + rationale
  before changing anything.
## 12. Bookshelf（阅读资产书架）— frozen v1

完整契约见 [docs/bookshelf-contract.md](bookshelf-contract.md)（2026-08-13 冻结，唯一权威）。

摘要：

- 阅读状态固定 `want_to_read / reading / read / dropped`；用户评价独立为 `liked / disliked / not_interested`（可空）；同一本书允许“已读 + 不喜欢”。
- Current Shelf（`user_book_shelf`）是当前阅读状态权威源；`recommendation_events` / `book_interactions` 仅作历史审计。
- 新增 API：`GET /books/shelf/{user_id}`、`POST /books/shelf`、`PATCH /books/shelf/{entry_id}`、`DELETE /books/shelf/{entry_id}`。
- `ReadingService` 是唯一业务写入口（Chat 反馈 / 推荐按钮 / Shelf API 都经它），Shelf + 阅读事件同事务原子；Memory 为派生写入，失败不阻断。
- 推荐链路（`search_books` + `RecommendationProjector`，含 `researched_recommendation`）读 Shelf 做抑制：`reading / read / dropped` 或 `disliked / not_interested` 过滤；Deep Research 不改。
- 前端书架页为一级独立页面，沿用 `mainView` 导航，不引入第二套路由；前端只依赖 `ShelfBook` 契约。
