# 毕业论文实现证据包

论文题目：**《基于 AI 大模型图书馆智能书籍管理系统设计与实现》**

本目录依据当前工作区的真实生产入口、ORM、SQL 迁移、前端组件和服务代码整理，用于后续修改论文，不代表新增功能，也不以 legacy/deprecated 代码证明当前能力。

## 1. 取证基准

| 项目 | 取证结果 |
|---|---|
| Git 分支 | `codex/search-deepresearch-quality` |
| 生产实现提交 | `054198dd5ff6cd761377066a3cc95daf010f039f` |
| 后端验证 | `691 passed, 38 subtests passed` |
| 前端验证 | `npm run build` 成功 |
| 取证日期 | 2026-08-28（Asia/Shanghai） |

> 论文材料位于生产实现提交之后的证据提交中；论文涉及功能实现时，应引用上表的生产实现提交，不应把材料整理提交写成业务代码基线。

## 2. 当前生产架构结论

### 2.1 普通 Chat

真实链路为：

`POST /api/v1/chat/stream` → `ChatStreamingService` → `TrustedControllerStream` → `AgentChatEntry` → `AgentControllerGateway` → Controller 一次语义决策 → `ProposalValidator/Compiler` → `AgentCoreHarness/SystemRuntime` 执行业务能力 → typed receipts/evidence → `TrustedPublisher` 综合 → `TurnPublicationCommitter` 持久化。

非流式入口 `POST /api/v1/chat/invoke` 由 `ChatService.invoke()` 进入同一 `AgentChatEntry`。当前服务明确没有 legacy runtime fallback。

### 2.2 一次语义理解、多业务事实执行

Controller 一次输出结构化动作和 `assertions[]`。`compiled_turn_from_assertions()` 逐条适配这些断言，不再调用第二个模型重新理解原句；阅读事实、一般事实等随后由不同业务所有者执行。因此“一句话包含多项事实”应表现为一次理解结果中的多条原子事实，而不是多个互相覆盖的正则或二次 LLM 解析。

### 2.3 长期记忆

权威写链为 `MemoryWriteGateway → EntityResolver + ModelFactExecutor → MemoryVersionStore → memory_events`。`MemoryVersionStore` 负责版本链、幂等、修订和 tombstone；它不承担语义理解。

权威读链为 `MemoryReadGateway → VersionedMemorySearch/MemoryVersionStore → current_projection`。当前实现中的“CurrentProjection”是 `current_projection.py` 提供的投影函数集合，并非一个同名 ORM 实体或数据库表。它只对“当前事实”折叠兼容性命名槽位，不删除历史版本。

### 2.4 阅读资产与 Memory 协同

`ReadingService` 是 `user_book_shelf` 的业务写入所有者，书架保存当前阅读状态、评价、备注等阅读资产。阅读动作需要形成长期事实时，经 `write_reading_memory_best_effort()` 重新进入 `MemoryWriteGateway.record_reading_memory()`，再写入版本化 Memory。

两者职责不同：Shelf 是当前阅读状态的权威来源；Memory 是跨会话长期事实和偏好的版本化来源。移出书架会同步把该书当前阅读投影写成 tombstone，使其退出 CurrentProjection；历史 Memory 版本仍保留用于审计，因此不等价于物理删除历史。

### 2.5 图书查找与个性化推荐

`RecommendationService` 同时承接两种明确模式：

- `lookup`：按书名/条件查找目录，不启用个性化候选抑制；
- `recommendation`：构建个性化约束，读取当前 Memory、Shelf、交互和推荐事件，对候选重排，并排除所有已在 Shelf 中的书以及明确负反馈候选。

推荐请求还会由 `ControllerRequestBuilder` 加载有界 Shelf 快照，使模型在一次语义决策中直接看到用户当前阅读资产并产生新候选；`RecommendationService` 使用正向书架条目扩展发现查询，工具核验候选后由 `TrustedPublisher` 综合。最终覆盖校验只补回已核验但被模型遗漏的候选，不替代模型的判断与表达。

### 2.6 普通 Search

普通 Search 不是“直接返回搜索列表”，而是 Controller 对用户问题做一次高质量理解，SearchGateway 按策略执行 failover 或 federated provider 检索，业务过滤后将 evidence/receipts 交给最终综合模型，由 Publisher 生成面向用户的回答。联邦模式通过并行 provider portfolio 合并结果。

### 2.7 Deep Research

用户显式设置 `research_mode=deep_research` 时进入独立的 `DeepResearchRunner`，不等于固定轮次的普通 Search。其流程包括：目标规划、候选组合、并行搜索分支、内容质量门、证据采纳、确定性缺口评估、Reviewer 生成下一轮子问题、边际信息增益停止条件、PublicationPacket/报告编辑模型生成最终 Markdown。

当前安全边界是：工具增加信息和可核验性，但外部证据不足不能无条件删除模型已有的候选知识；最终交付仍由面向用户的报告模型负责组织、判断和表达。

### 2.8 当前前端主要功能

- 用户身份进入、会话列表、新建/重命名/删除会话；
- 普通 Chat、模型选择、思考模式、深度搜索开关、流式结果；
- 本轮执行步骤、完整执行图、Token 用量；
- 我的书架：新增、搜索、状态筛选、阅读状态、评价、备注、移除；
- 记忆中心：按语义类别查看当前事实、编辑、查看版本时间线、遗忘；阅读记忆引导回书架管理；
- 图书查找、个性化推荐及推荐反馈；
- 深度研究运行列表、状态、已知事实、证据、步骤和最终报告；
- 模型与 Provider 连接配置、应用服务配置、连接健康检查；
- 微信扫码入口和 WebSocket 登录支持。

## 3. 现有材料不一致与论文风险

| 风险来源 | 旧描述/风险 | 当前处理建议 |
|---|---|---|
| `README.md`、`README.zh.md` | 仍声明分支 `codex/r8-19-memory-arch`；仍把 canonicalizer/write_gate/向量索引写成当前主写链 | 论文以本证据包和当前生产源码为准，不复制该旧链 |
| `docs/frontend-backend-contract.md` | 仍描述 TurnFactCompiler 预处理、canonicalizer 和 MemoryCommitter | 改为 Controller assertions → 确定性适配 → MemoryWriteGateway → ModelFactExecutor/ReadingService → MemoryVersionStore |
| `docs/SYSTEM_ARCHITECTURE.md` | 结构大体接近现状，但文首仍保留历史效果裁决 `e746714` | 历史裁决可作为演进背景；论文当前实现必须引用 `054198d` 和本证据包 |
| `memory-bank/*` | 多处仍写 LangGraph Store + PGVector 是长期记忆主存储 | 当前权威事实是 PostgreSQL `memory_events` 版本链；向量召回只能写成辅助能力 |
| legacy/deprecated 文件 | `MemoryCommitter`、`write_coordinator.py`、旧 canonicalizer 路径、`TurnFactCompiler._llm_facts()` 仍可能存在 | 只能作为迁移/兼容背景，不得画入当前生产主流程，也不得作为第五章实现代码 |
| ORM 与 SQL 差异 | ORM 的 `memory_events.source_event_id` 仍声明关联 `conversation_events`，但迁移 031 已删除物理外键以支持多种 provenance | E-R 图不得把它画成强制物理外键；字段表需标为“逻辑来源标识” |
| 书架关系 | `user_book_shelf.book_id` 可空，允许仅有标题的书架记录 | 概念上是用户与图书的 M:N 关联实体，但论文须注明数据库允许尚未绑定 `books` 的条目 |

## 4. 文件索引

- `01_function_modules.md`：一级/二级功能模块、模块职责和功能模块图；
- `02_usecase.md`：真实 Actor、Use Case、include/extend 关系及 PlantUML 草稿；
- `03_er_and_database.md`：10 个核心实体、规范关系、E-R 草稿和字段表；
- `04_code_snippets.md`：第五章 8 个真实核心代码片段与流程图建议；
- `05_screenshot_plan.md`：真实运行截图清单、状态要求、论文小节和脱敏要求。
- `00_verified_technical_facts.md`：当前代码基准、测试结果和不可混淆的技术事实。
- `screenshots/`：6 张已脱敏的真实系统运行 PNG。

## 5. 使用边界

1. 图稿是论文重绘依据；定稿时统一字体、线宽、编号和中文命名。
2. 字段表以 SQLAlchemy ORM 与现行 SQL 迁移共同核验；迁移覆盖 ORM 时，以数据库迁移后的物理约束为准。
3. 代码片段的路径和行号以生产实现提交 `054198d` 为准；生产代码后续变化时需要重新核验。
4. 截图来自真实系统运行过程，仅使用公开测试数据；本包不包含 API Key、Token、Cookie、密码、日志或数据库导出。
5. 压缩包是论文核验副本，不应反向作为生产部署源。
