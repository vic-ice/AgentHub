# 已核验技术事实

## 1. 取证基准

| 项目 | 结果 |
|---|---|
| 生产代码分支 | `codex/search-deepresearch-quality` |
| 生产实现提交 | `283db474065eae0d6959122ed1bdf9eeadd42975` |
| 后端验证 | `693 passed, 38 subtests passed` |
| 前端验证 | `npm run build` 成功 |
| 取证日期 | 2026-08-28（Asia/Shanghai） |

论文中的架构、代码路径、行号和运行结果均以该生产实现提交为准。论文材料和截图可以位于后续证据提交中，但不得把后续仅用于整理材料的提交误写成生产功能基线。

## 2. 当前真实实现事实

1. 普通 Chat 由 Controller 完成一次语义决策，业务能力只执行结构化动作和原子 assertions，最终通过 typed receipt/evidence 进入可信发布链。
2. `MemoryWriteGateway` 是长期记忆统一写入入口，`MemoryVersionStore` 保存版本、修订和遗忘 tombstone，`current_projection.py` 只投影当前有效事实。
3. `ReadingService` 与 `user_book_shelf` 是当前阅读资产的权威所有者；阅读类长期记忆是从阅读动作派生的跨会话事实，不是书架表的替代存储。
   移出书架会在同一事务中定向遗忘该书当前阅读投影，但保留历史版本。
4. 个性化推荐同时读取 Memory、Shelf、推荐事件和交互信号。Controller 在推荐请求中获得有界书架快照，模型负责产生候选智能，检索负责核验和补充信息。
5. Shelf 中的已知书在“推荐新书”模式下统一排除；正向书架条目可以作为发现锚点，但不能再次成为最终候选。
6. 发布阶段保留模型的面向用户综合，同时通过 `complete_book_candidate_coverage()` 防止已经核验的候选被静默压缩丢失。
7. 普通 Search 是“一次语义理解 + 并行/故障转移检索 + 一次最终综合”；Deep Research 使用独立的目标规划、证据缺口、Reviewer 和报告发布流程。
8. 本轮未修改 ORM 或 SQL 迁移，因此核心 E-R 实体、字段和基数关系没有变化。

## 3. 交付边界

- 不包含 `.env`、API Key、Token、Cookie、数据库密码、日志、缓存、`.git`、`node_modules`、`.venv`、`dist` 或真实隐私数据。
- `backend/scripts/probe_book_search_owner.py`、`.codex-artifacts/`、`.codex-review/` 属于本地调试材料，不作为论文生产实现证据。
- 截图只保留 `screenshots/` 根目录下 6 张最终 PNG；`browser_check/` 和 `thesis_run/` 不属于正式交付。
