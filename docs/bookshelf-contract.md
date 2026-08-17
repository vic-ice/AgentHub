# Bookshelf（阅读资产书架）契约 — frozen v1

冻结日期：2026-08-13
状态：已确认，作为前端 Mock 与后端实现的唯一权威依据。如要变更契约，先在此文档更新并写明影响面。

## 0. 决策基线（产品确认）

1. **阅读状态与用户评价正交**：
   - 阅读状态固定为 `want_to_read / reading / read / dropped`；
   - 用户评价单独为 `liked / disliked / not_interested`（可为空）；
   - 同一本书允许同时“已读 + 不喜欢”（`read` + `disliked`）。
2. **Current Shelf 是“当前阅读状态”的权威查询源**；`RecommendationEvent` / `BookInteraction` 只保存历史审计。
3. **ReadingService 是唯一业务写入口**：Chat 反馈、推荐卡片按钮、Shelf API 一律经它写入；状态↔事件↔记忆↔旧 Interaction 的映射逻辑只存在于 ReadingService 一份。
4. **事务保证**：Shelf 行 + 阅读事件（RecommendationEvent）在同一数据库事务内原子写入；BookInteraction 兼容行作为审计的一部分尽量同事务；Memory 是派生能力，写失败不能阻止核心阅读状态更新（post-commit 尽力写入，失败仅日志）。
5. **推荐消费**：`researched_recommendation` 仍属于 `search_books` + `RecommendationProjector` 链路，跟随 Shelf 过滤；真正的 Deep Research 保持现状，本次不允许改动。
6. **前端**：书架是一级独立页面，技术上沿用现有 `mainView` 导航模式（chat / research / bookshelf），不引入第二套路由体系；前端只依赖本契约中的 `ShelfBook` 类型，不得依赖 `BookInteraction` / `RecommendationEvent` 的底层结构。

## 1. 枚举（唯一权威定义）

### ReadingStatus（必填）

| 值 | 中文 | 含义 |
|---|---|---|
| `want_to_read` | 想读 | 在愿望清单 |
| `reading` | 在读 | 正在阅读 |
| `read` | 已读 | 已读完 |
| `dropped` | 弃读 | 中途放弃/不再打算读 |

### BookEvaluation（可空）

| 值 | 中文 | 含义 |
|---|---|---|
| `liked` | 喜欢 | 正面评价 |
| `disliked` | 不喜欢 | 负面评价 |
| `not_interested` | 不感兴趣 | 拒绝/回避 |
| `null` | 未评价 | 默认值 |

### 组合约束

- `reading_status` 必填：进入书架即存在状态。
- `evaluation` 可空，与 `reading_status` 完全独立，任何组合合法（`read + disliked` 合法且常见）。
- `DELETE` 表示从书架移除（回到“无状态”），移除不产生软删除标记；审计保留在事件里。

## 2. ShelfBook 契约（前端唯一依赖）

```jsonc
{
  "id": "uuid",                      // 书架条目 id
  "user_id": "uuid",
  "book_id": "uuid | null",          // null = 尚未关联到 books 缓存（title 快照条目）
  "title": "string",                 // 必填
  "authors": ["string"],
  "tags": ["string"],
  "cover_url": "string | null",
  "source_url": "string | null",
  "reading_status": "want_to_read | reading | read | dropped",
  "evaluation": "liked | neutral | disliked | not_interested | null",
  "note": "string",                  // 用户备注，可空
  "rating": "number | null",         // 1..5，可空
  "created_at": "ISO datetime",
  "updated_at": "ISO datetime",
  "last_event_at": "ISO datetime | null"  // 最近一次阅读/评价事件时间，用于 UI 排序
}
```

前端类型定义冻结于 `frontend/src/types.ts`（`ShelfBook` / `ReadingStatus` / `BookEvaluation` 等），API 封装冻结于 `frontend/src/lib/api.ts`。

## 3. REST API（/api/v1/books/shelf）

| 方法 | 路径 | 用途 | 返回 |
|---|---|---|---|
| GET | `/books/shelf/{user_id}` | 列出书架（可按状态/评价/关键词过滤） | `BookshelfListResponse` |
| POST | `/books/shelf` | 新增/幂等 upsert 一本书到书架 | `ShelfBook` |
| PATCH | `/books/shelf/{entry_id}` | 修改状态/评价/备注/评分 | `ShelfBook` |
| DELETE | `/books/shelf/{entry_id}` | 从书架移除 | `204` |

### GET /books/shelf/{user_id}

Query：`status`（ReadingStatus，可多值逗号分隔）、`evaluation`（BookEvaluation）、`q`（标题/作者模糊）、`limit`（默认 5000，≤5000；书架页全量加载，不做固定分页）、`offset`。

```jsonc
{
  "user_id": "uuid",
  "items": [ShelfBook],
  "total": 0,
  "status": "ok"
}
```

### POST /books/shelf

```jsonc
{
  "user_id": "uuid",
  "book_id": "uuid | null",          // 可选；优先关联 books 元数据
  "title": "string",                 // 可选；book_id 未命中或缺失时按标题归一化匹配，未命中则创建 title 快照条目
  "reading_status": "want_to_read | reading | read | dropped",  // 缺省 want_to_read
  "evaluation": "liked | neutral | disliked | not_interested | null",
  "note": "string",
  "rating": "number | null"
}
```

- `book_id` 与 `title` 至少提供其一。
- 同用户同书重复 POST = upsert（更新状态/评价），返回更新后的条目。

### PATCH /books/shelf/{entry_id}

```jsonc
{
  "reading_status": "reading",       // 可选
  "evaluation": "disliked",          // 可选；显式传 null 清除评价
  "note": "string",
  "rating": "number | null"
}
```

至少一项；`404` 当条目不存在。

### DELETE /books/shelf/{entry_id}

成功返回 `204`；`404` 当条目不存在。

## 4. ReadingService 责任边界

实现位置：`backend/app/services/books/reading_service.py`（新）。

### 4.1 唯一写入口（必须经此）

- Chat 自然语言反馈（`extract_book_feedback` / `profile_memory_capture` 中的想读/在读/已读/弃读/评价路径）
- `record_book_feedback` 工具
- `record_recommendation_signal` 工具中涉及阅读状态/评价的语义
- 推荐卡片反馈按钮（前端 → POST/PATCH Shelf API → ReadingService）
- 书架页面手动修改（POST/PATCH/DELETE Shelf API → ReadingService）

### 4.2 职责

1. upsert 当前状态到 `user_book_shelf`；
2. 同事务写入 `RecommendationEvent` 审计（事件类型扩展见 §6）；
3. 同事务写入 `BookInteraction` 兼容行（审计的一部分，默认开启，供 legacy 消费者）；
4. 事务提交后尽力写入 Memory（`reading_state` / `feedback`），失败仅捕获日志，不阻断主流程；
5. 提供 `backfill_from_history(user_id)`：按每本书最后一次有效事件 last-write-wins 生成初始 Shelf，幂等可重跑；
6. 拥有全部映射的唯一实现：`reading_status ↔ event_type ↔ memory polarity ↔ legacy interaction_type`。

### 4.3 禁止

- 任何其他模块直接 INSERT / UPDATE / DELETE `user_book_shelf`；
- 任何其他模块自行维护“状态 ↔ 事件”映射；
- 前端读写 `BookInteraction` / `RecommendationEvent` 表结构。

### 4.4 事务保证

- 核心原子集：`user_book_shelf` + `recommendation_events`（+ `book_interactions` 兼容行）在同一个数据库事务内；
- Memory 派生写入在事务提交后执行，失败不影响核心结果返回；
- 若外部约束导致无法共享同一事务，最低保证为 **Shelf + 阅读事件原子一致**。


## 5. 推荐消费规则（Shelf + 已读风格锚点 → Projection）

### 5.1 候选抑制（不重复推荐）

- **硬抑制（过滤出候选）**：普通“推荐新书”模式下，`reading_status ∈ {want_to_read, reading, read, dropped}` 的已知书全部过滤。
- **书级负面评价**：`evaluation ∈ {disliked, not_interested}` 的候选书本身被抑制（用户已拒绝/不爱读），同时该读书记为**负样本**：对风格相似的其他候选做**谨慎软降权**（tag -0.10 / author -0.15，聚合限幅 -0.3），绝不把整类风格拉黑。
- **风格级避雷（唯一硬性风格排除）**：只有用户明确表达“不喜欢某风格”（记忆偏好 polarity=avoid/dislike 的 tag）时，匹配该风格的候选才被真正抑制（`avoided_style`）。
- **want_to_read**：在普通“推荐新书”模式中同样抑制，避免把用户已知/已收藏的书再次包装成新发现；明确查询或书架回顾使用 `mode=lookup`，不套新书过滤。

### 5.2 相似推荐（基于已读风格锚点）

- 已读书籍（`reading_status == read`）是**风格锚点**，其 tags/authors 用于对候选书做相似度加权：
  - `liked`：tag +0.22 / author +0.30（强正向）
  - `neutral`（一般）：tag +0.08 / author +0.10（弱正向）
  - 无评价：tag +0.15 / author +0.20（读过即默认轻度正向）
  - `disliked / not_interested`：tag -0.10 / author -0.15（负样本，软降权）
- 聚合限幅：单候选锚点总分 ∈ [-0.3, +0.6]，避免锚点淹没查询/来源相关性。
- 约束层会把已读锚点的 top tags 并入 `effective_query`（≤ max_search_terms），让普通 Search 主动找相似风格。

### 5.3 读取顺序

- `RecommendationProjector` 读取顺序：Shelf 为当前状态权威；事件仅用于时效性评分（decay），不再回放计算状态。
- `researched_recommendation` 走 `search_books` + `RecommendationProjector`，自动继承上述过滤与锚点加权。
- Deep Research 不读取 Shelf，不改动。

## 6. 事件类型扩展

`RECOMMENDATION_EVENT_TYPES` 增加两个事件类型：`reading`、`dropped`。

| 书架变更 | RecommendationEvent |
|---|---|
| status → want_to_read | `want_to_read` |
| status → reading | `reading`（新增） |
| status → read | `read` |
| status → dropped | `dropped`（新增） |
| evaluation → liked | `liked` |
| evaluation → disliked | `disliked` |
| evaluation → not_interested | `not_interested` |
| evaluation → null | 不写评价事件（仅更新 Shelf） |

## 7. 并行工作包（冻结后）

- **A. 前端 Mock**：`BookshelfView`（mainView 第三分支）+ mock adapter；UI 只认 `ShelfBook` 类型，不依赖后端表结构。
- **B. 后端核心**：`user_book_shelf` 表 + `ReadingService` + 4 个 Shelf API + 事件类型扩展 + 历史回填。
- **C. 接入改造**：`record_book_feedback` / `record_recommendation_signal` / Chat NL 反馈改走 ReadingService；`RecommendationProjector` 改读 Shelf；推荐卡片反馈按钮走新 API。
- **D. 回归验证**：Deep Research 保持现状，加入回归断言（Shelf 变更不影响 deep_research 路径）。
