# 🚀 AgentHub（test02 重构工作区）

> 本工作区由 `F:\book_data\test01` 迁移而来，用于前端 UI/UX 重构。test01 保留为当前可用版本，不再修改。
> 本文档以当前代码实际实现为准（分支 `codex/r8-19-memory-arch`），不描述未实现的功能。

## 1. 项目定位与目标

AgentHub 是一个以 Agent Core 为核心的对话 + 深度研究平台：普通 Chat、工具调用（web_search / book_search / weather / memory / research_read）、以及一键触发的 Deep Research（多轮检索 → 证据准入 → Reviewer 缺口驱动 → 编辑型最终报告）。记忆是"版本化语义记忆"：模型输出结构化断言，系统落库、版本化并支持检索。

## 2. 当前整体架构

```
frontend (React 19 + Vite 8, :5173)  --/api-->  backend (FastAPI, :8080)
                                                  |-- PostgreSQL + pgvector (:5433 本地 / :5432 docker)
                                                  |-- LLM Providers (DashScope / OpenRouter / OpenAI-compatible)
                                                  |-- External search (Tavily / DDGS / ...)
```

- 前端只通过 REST + SSE 与后端通信，不直连数据库。
- 后端按"Controller 提议 → Harness 执行 → Trusted 发布"组织对话；Deep Research 是独立运行时。
- 记忆、研究、检索均为后端服务；前端消费其 API 与流事件。

## 3. 前端架构

- 技术栈：React 19、TypeScript、Vite 8、Tailwind CSS 4、Radix/shadcn UI、react-markdown + remark-gfm + rehype-raw + shiki、Tiptap（编辑器）、react-router。
- 目录：`src/pages`（home-page）、`src/features/chat|kanban|research`（功能模块）、`src/components/ui|ai|user`、`src/lib/api.ts`（全部后端调用封装）、`src/types.ts`（前后端数据结构）、`src/contexts`、`src/hooks`、`src/i18n`、`src/channels/weixin`。
- 状态：组件内/Context 为主，无独立全局 store（重构时可用 Zustand/Redux 增强，但需保持后端契约）。
- 流式：`streamChat()` 读取 SSE，`parseStreamChunk` 解析事件；支持 token / thinking / step / done / error 事件。

## 4. 后端 / Agent Core 架构

- FastAPI 应用：`backend/app/main.py`，API 前缀 `/api/v1`。
- Agent Core（`app/services/agent_core/`）：
  - `controller_client`：模型一次决策（direct answer / capability proposals / clarification / task plan）。
  - `gateway`：请求构建 → TurnControllerLoop（多轮）→ 结果。
  - `harness`：校验提议 → 编译执行计划 → 运行工具 → 产生 receipt。
  - `trusted_stream`：SSE 流式发布；`publication/policy.py` 发布校验。
  - `capabilities/compiler`：工具注册与编译（web_search / book_search / weather / memory / research_read / plan_task）。
- Deep Research（`app/services/research/`）：`deep_research_runner`（多轮循环）、`source_visit`（Fetch）、`source_extraction`（抽取，含书单多条目）、`verifier/admission`（准入）、`reviewer`（Evolving Report + 缺口驱动）、`report_writer`（单次编辑型最终报告）、`read`（research_read）。
- Memory（`app/services/memory/`）：`canonicalizer`（断言→schema）、`intent`（意图路由）、`write_gate`、`version_store`（版本化）、`vector_recall`（语义检索）、`version_search`。

## 5. 普通 Chat 工作流

1. 前端 `POST /api/v1/chat/run/stream`（UserInput，`research_mode="chat"`）。
2. `TrustedControllerStream` → Controller 决策。
3. 若无工具：直接发布文本；若有工具：Harness 执行（web_search / memory 等）→ receipt。
4. `publication/policy` 校验后提交一条 `assistant_published`（journal 事件）。
5. SSE 推送 token / thinking / step 事件，前端实时渲染。

## 6. Search 工作流

- 模型在对话中按需调用 `web_search`（Tavily/DDGS 等外部 Provider，见 `external_search/gateway.py` 与 `providers/`）。
- 结果经 `content_quality_gate`、`source_visit`、`source_extraction`、`verifier/admission` 形成证据；综合回答时 `publication/policy` 校验 URL 真实性（允许不挂链接，拒绝编造 URL）。

## 7. Deep Research 工作流

1. 前端 `POST /api/v1/chat/run/stream`，`research_mode="deep_research"`。
2. `deep_research_runner` 创建 `research_runs`，最多 3 轮：
   - 搜索（每轮 ≤8 条）→ 垃圾门 → 去重 → 抽取（书单按书拆条，每文档 ≤12 条）→ 准入（每轮 ≤8 条）。
   - `reviewer` 维护 Evolving Report，判定 sufficient / insufficient，产出 subquestion 入队驱动下一轮。
3. 报告阶段：`report_writer` 单次模型调用（编辑人格 + Markdown + emoji + 对比表格，超时 90s + 重试一次），只读 evidence 直接取最终交付；空/异常才落确定性兜底。
4. 报告作为 assistant 消息提交；run 与 evidence 持久化供后续 `research_read`。

## 8. Memory 架构

- 写入：模型返回结构化断言（subject/predicate/value/qualifiers/evidence_quote）→ `canonicalizer` 按 schema 规范化 → `write_gate` 校验 → `version_store` 版本化落库 → 向量索引。
- `entity.name`：存储协议 schema（registry / write_gate / version_search / entity_normalizer 保留），实体命名事实由模型结构化输出（`predicate=entity_name`）写入，不做规则式自然语言猜测。
- 读取：`/memory/current` 当前事实、`/memory/history` 版本历史、语义 recall。

## 9. ResearchRun / Journal / research_read 的关系

- `ResearchRun`（`research_runs`）：目标、预算、停止条件、evidence、状态快照；`thread_id` 关联会话。
- `Journal`（`conversation_events`）：完整报告正文是普通 assistant 消息；消息按 thread 单调 sequence 排列。
- Chat 上下文只放紧凑指针；模型按需调用 `research_read(run_id, scope)` 从 ResearchRun 精确取证（报告/发现/来源/证据）。

## 10. 数据库及主要持久化

- PostgreSQL + pgvector；本地开发用 `agenthub-dev-db`（:5433），docker-compose 用 `agenthub-db`（:5432）。
- 主要表：`conversations`、`conversation_events`、`research_runs`、`research_evidence`、`research_state_snapshots`、`research_steps`、`memory_events`、`memory_management_events`、`users`、`models`、`providers`、`provider_connections`、`model_capability_checks`、`trace_executions`、`books`、`recommendation_events` 等。
- 迁移：`backend/scripts/sql/change_*.sql`（幂等变更脚本）。

## 11. 模型 / Provider 架构

- `models` 表（provider / model_id / thinking / active / default / connection_id）+ `providers` / `provider_connections`。
- `infra/llm/manager.py` 缓存模型配置；`factory.get_llm()` 按 provider 构建 ChatLiteLLM（DashScope / OpenRouter / OpenAI-compatible）。
- thinking 是模型级开关；DashScope qwen 强制 thinking=True。
- `model_candidates.py` 用于候选/观测；多模型自动回退已按决策移除，报告链路只用当前所选模型。

## 12. 当前已实现的主要功能

- 普通 Chat + SSE 流式（token / thinking / step / done / error）。
- 工具能力：web_search、book_search、weather_get、记忆读写、research_read、plan_task、request_clarification。
- Deep Research：多轮检索 → 书单多条目抽取 → 准入 → Reviewer / Evolving Report / Subquestion Queue → 编辑型最终报告（emoji / 对比表格 / 分层书单）。
- ResearchRun 持久化 + research_read 按需取证。
- 记忆：版本化写入/历史/检索，entity.name 结构化存储协议。
- 模型/Provider 管理 UI 与 API；认证（mock 本地用户）。
- 全量测试 395 例通过。

## 13. 当前限制 / 未完成能力

- 前端正在重构（本工作区目标），旧 UI 不作为最终形态。
- `books.py` 等旧功能仍存在但非主链路。
- 深搜报告质量依赖所选模型（qwen 表现最佳；部分模型能力不足，不自动回退）。
- 某些环境依赖的外部服务（Tavily/DDGS/天气）需要 API key/网络。
- 多用户/正式认证仍是 mock 级别。

## 14. 项目目录说明

```
test02/
├─ frontend/        React + Vite 前端（重构目标）
├─ backend/         FastAPI 后端（app/、tests/、scripts/、run_backend.py）
├─ docs/            frontend-backend-contract.md 前后端契约清单
├─ docker-compose*.yml
├─ memory-bank/     Agent 记忆工作区（代码库外记忆）
├─ local-dev/       本地开发辅助
└─ imgs/            文档图片
```

## 15. 本地开发与启动方式

前置：PostgreSQL + pgvector（本地 `docker start agenthub-dev-db`，:5433；或 `docker-compose up -d db`）。

后端：
```
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows；Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # 填入数据库与默认模型配置
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'; $env:UVICORN_RELOAD='false'
.venv\Scripts\python.exe run_backend.py   # http://127.0.0.1:8080
```

前端：
```
cd frontend
npm install
npm run dev                        # http://localhost:5173（/api 代理到 8080）
```

test02 与 test01 同时运行时，请错开端口：后端改 `PORT`（如 8090）、前端改 `VITE_DEV_PROXY_TARGET` 并用 `--port 5174`。

## 16. 环境变量与依赖说明

- 后端 `backend/.env`：`MODE`、`HOST`、`PORT`、`POSTGRES_*`、`SYSTEM_DEFAULT_LLM_MODEL/API_KEY/EMBEDDING_MODEL/BASE_URL`、`OPENROUTER_*`、`AGENT_CONTROLLER_V1_MODE`、`LITELLM_LOCAL_MODEL_COST_MAP` 等。
- 前端 `frontend/.env`：`VITE_DEV_PROXY_TARGET`。
- 模型 API key 主要经 Web UI（Provider 配置）维护，环境变量为系统默认。
- 依赖：后端 `requirements.txt`；前端 `package.json`（React 19、Vite 8、Tailwind 4、react-markdown、tiptap、shiki 等）。

## 17. 测试与验证方式

- 后端单测：`cd backend && .venv\Scripts\python.exe -m unittest discover -s tests -q`（当前 395 例全绿）。
- 深搜验证脚本：`backend/scripts/verify_dr_reviewer_evolution.py`、`verify_report_writer_e2e.py`、`probe_source_pipeline.py`（书单抽取流水线）。
- 记忆验证：`verify_memory_effect_eval.py`、`verify_memory_intent_batch.py`。
- 契约回归：`tests/test_dr_reviewer.py`、`tests/test_memory_*.py` 等；前端重构期间不得改动后端契约（见 `docs/frontend-backend-contract.md`）。