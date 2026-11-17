from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from langchain_core.messages import AIMessage

from app.services.research.objective_planner import (
    RecommendationCandidateHypothesis,
    ResearchObjectivePlan,
    candidate_search_hints,
    candidate_verification_queries,
    fallback_research_objective_plan,
    plan_research_objective,
)


class ResearchObjectivePlannerTests(unittest.TestCase):
    def test_planner_prefers_tool_payload_and_does_not_parse_thinking(self) -> None:
        payload = {
            "task_type": "book_recommendation",
            "subject_type": "books",
            "candidate_titles": ["候选甲", "候选乙", "候选丙"],
            "recommended_candidate_count": 3,
            "evidence_strategy": {
                "facets": [{"name": "实际适配", "candidate_specific": True}]
            },
        }

        class ToolModel:
            tool_choice = ""

            def bind_tools(self, schemas, **kwargs):
                self.schemas = schemas
                self.tool_choice = kwargs.get("tool_choice")
                return self

            def bind(self, **kwargs):
                self.bound = kwargs
                return self

            async def ainvoke(self, prompt):
                self.prompt = prompt
                return AIMessage(
                    content=[{"type": "thinking", "thinking": "internal reasoning"}],
                    tool_calls=[
                        {
                            "name": "submit_research_plan",
                            "args": payload,
                            "id": "plan-1",
                            "type": "tool_call",
                        }
                    ],
                )

        model = ToolModel()
        with mock.patch(
            "app.services.research.objective_planner._resolve_model_id",
            return_value="model-id",
        ), mock.patch(
            "app.infra.llm.get_llm",
            return_value=model,
        ), mock.patch(
            "app.services.research.objective_planner.report_model_completion",
        ):
            plan = asyncio.run(
                plan_research_objective("推荐三本技术书", model_id="model-id")
            )

        self.assertEqual(model.tool_choice, "auto")
        self.assertEqual(model.bound["max_tokens"], 3200)
        self.assertEqual(plan.provider, "runtime_llm")
        self.assertEqual(plan.candidate_titles, ["候选甲", "候选乙", "候选丙"])
        self.assertEqual(plan.recommended_candidate_count, 3)

    def test_planner_accepts_structured_json_from_provider_reasoning_field(self) -> None:
        payload = {
            "task_type": "book_recommendation",
            "subject_type": "books",
            "candidate_titles": ["候选甲", "候选乙", "候选丙"],
            "recommended_candidate_count": 3,
            "evidence_strategy": {
                "facets": [{"name": "实际适配", "candidate_specific": True}]
            },
        }

        class ReasoningJsonModel:
            def bind_tools(self, tools, **kwargs):
                return self

            def bind(self, **kwargs):
                return self

            async def ainvoke(self, prompt):
                return AIMessage(
                    content="",
                    additional_kwargs={
                        "reasoning_content": (
                            "internal planning, followed by structured data:\n"
                            + json.dumps(payload, ensure_ascii=False)
                        )
                    },
                )

        with mock.patch(
            "app.services.research.objective_planner._resolve_model_id",
            return_value="reasoning-provider",
        ), mock.patch(
            "app.infra.llm.get_llm",
            return_value=ReasoningJsonModel(),
        ), mock.patch(
            "app.services.research.objective_planner.report_model_completion",
        ):
            plan = asyncio.run(
                plan_research_objective(
                    "推荐三本机器学习书",
                    model_id="reasoning-provider",
                )
            )

        self.assertEqual(plan.provider, "runtime_llm")
        self.assertEqual(plan.error, "")
        self.assertEqual(plan.recommended_candidate_count, 3)

    def test_model_owns_candidate_roles_and_recommendation_breadth(self) -> None:
        plan = ResearchObjectivePlan(
            task_type="book_recommendation",
            subject_type="books",
            candidate_portfolio=[
                RecommendationCandidateHypothesis(
                    title="候选甲",
                    portfolio_role="理论主线",
                    rationale="建立概念框架",
                ),
                RecommendationCandidateHypothesis(
                    title="候选乙",
                    portfolio_role="实践主线",
                    rationale="形成可运行项目",
                ),
                RecommendationCandidateHypothesis(
                    title="候选丙",
                    portfolio_role="参考手册",
                    rationale="用于查漏补缺",
                ),
            ],
            recommended_candidate_count=3,
        )

        self.assertEqual(plan.candidate_titles, ["候选甲", "候选乙", "候选丙"])
        self.assertEqual(plan.recommended_candidate_count, 3)
        self.assertEqual(plan.candidate_portfolio[1].portfolio_role, "实践主线")

    def test_planner_drops_search_headlines_from_candidate_portfolio(self) -> None:
        plan = ResearchObjectivePlan(
            task_type="book_recommendation",
            subject_type="books",
            candidate_portfolio=[
                RecommendationCandidateHypothesis(
                    title="2024年最佳个人理财书籍",
                    portfolio_role="理财入门",
                ),
                RecommendationCandidateHypothesis(
                    title="金钱心理学",
                    portfolio_role="金钱观",
                ),
            ],
            candidate_titles=[
                "2026 年最值得读的 10 本成长书籍推荐",
                "原子习惯",
            ],
            recommended_candidate_count=4,
        )

        self.assertEqual(plan.candidate_titles, ["金钱心理学", "原子习惯"])
        self.assertEqual(plan.recommended_candidate_count, 2)

    def test_long_explicit_book_request_has_deterministic_book_contract(self) -> None:
        plan = fallback_research_objective_plan(
            "请推荐至少4本适合零基础读者的人工智能入门书。"
            "请按书名、作者、推荐理由、适合人群、来源链接的5列表格输出，"
            "不要推荐虚构书目。"
        )

        self.assertEqual(plan.task_type, "book_recommendation")
        self.assertEqual(plan.subject_type, "books")

    def test_worth_reading_book_request_is_recommendation_not_fact_check(self) -> None:
        plan = fallback_research_objective_plan(
            "独立深度研究近期值得阅读的个人成长图书，并给出有来源的报告。"
        )

        self.assertEqual(plan.task_type, "book_recommendation")
        self.assertEqual(plan.subject_type, "books")

    def test_normalization_keeps_diverse_hypotheses_and_bounds_frontier(self) -> None:
        from app.services.research.objective_planner import _normalize_plan

        plan = _normalize_plan(
            ResearchObjectivePlan(
                task_type="book_recommendation",
                subject_type="books",
                seed_entities=["穷查理宝典"],
                candidate_titles=[
                    "原则",
                    "穷查理宝典：查理·芒格的智慧箴言录",
                    "原则：生活与工作",
                    "影响力",
                ],
                search_queries=[
                    "原则 瑞达利欧",
                    "穷查理宝典 查理芒格",
                    "原则生活与工作 瑞达利欧",
                    "影响力 罗伯特西奥迪尼",
                ],
            )
        )
        self.assertEqual(plan.candidate_titles, ["原则", "穷查理宝典：查理·芒格的智慧箴言录", "影响力"])
        self.assertEqual(plan.search_queries, ["原则 瑞达利欧", "穷查理宝典 查理芒格", "原则生活与工作 瑞达利欧"])
        self.assertEqual(
            list(candidate_search_hints(plan)),
            ["原则", "穷查理宝典：查理·芒格的智慧箴言录", "影响力"],
        )

    def test_model_only_seeds_are_downgraded_to_candidate_hypotheses(self) -> None:
        from app.services.research.objective_planner import (
            _merge_with_fallback,
            _normalize_plan,
        )

        objective = "推荐几本适合普通上班族的稳健理财书"
        fallback = fallback_research_objective_plan(objective)
        merged = _merge_with_fallback(
            _normalize_plan(
                ResearchObjectivePlan(
                    task_type="book_recommendation",
                    subject_type="books",
                    seed_entities=["小狗钱钱", "指数基金投资指南"],
                    candidate_titles=["通往财富自由之路"],
                )
            ),
            fallback,
            objective=objective,
        )

        self.assertEqual(merged.seed_entities, [])
        self.assertEqual(
            merged.candidate_titles,
            ["通往财富自由之路", "小狗钱钱", "指数基金投资指南"],
        )

    def test_candidate_hint_removes_schema_scaffolding(self) -> None:
        plan = ResearchObjectivePlan(
            task_type="book_recommendation",
            candidate_titles=["原则：生活与工作"],
            search_queries=['"原则" 瑞·达利欧 subtitle "生活与工作"'],
        )
        self.assertEqual(
            candidate_search_hints(plan)["原则：生活与工作"],
            "原则：生活与工作 原则 瑞·达利欧 生活与工作",
        )

    def test_bilingual_display_titles_are_normalized_before_verification(self) -> None:
        from app.services.research.objective_planner import ResearchObjectivePlan

        plan = ResearchObjectivePlan(
            task_type="book_recommendation",
            subject_type="books",
            seed_entities=["关键对话 (Crucial Conversations)"],
            candidate_titles=[
                "非暴力沟通 (Nonviolent Communication)",
                "Thinking, Fast and Slow",
            ],
        )
        self.assertEqual(plan.seed_entities, ["关键对话"])
        self.assertEqual(
            plan.candidate_titles,
            ["非暴力沟通", "Thinking, Fast and Slow"],
        )

    def test_bracketed_title_discards_trailing_author_hint(self) -> None:
        plan = ResearchObjectivePlan(
            task_type="book_recommendation",
            subject_type="books",
            candidate_titles=[
                "《富爸爸穷爸爸》 - 罗伯特·清崎",
                "《小狗钱钱》— 博多·舍费尔",
            ],
        )

        self.assertEqual(plan.candidate_titles, ["富爸爸穷爸爸", "小狗钱钱"])

    def test_candidate_titles_drop_author_suffixes_and_bilingual_aliases(self) -> None:
        plan = ResearchObjectivePlan(
            task_type="book_recommendation",
            subject_type="books",
            candidate_titles=[
                "Deep Learning (Ian Goodfellow, Yoshua Bengio)",
                "动手学深度学习 / Dive into Deep Learning",
                "Deep Learning with Python - François Chollet",
                "Deep Learning with Python (2nd Edition)",
            ],
        )

        self.assertEqual(
            plan.candidate_titles,
            [
                "Deep Learning",
                "动手学深度学习",
                "Deep Learning with Python",
                "Deep Learning with Python (2nd Edition)",
            ],
        )

    def test_one_semantic_plan_separates_reading_year_from_publication_year(self) -> None:
        payload = {
            "task_type": "book_recommendation",
            "subject_type": "books",
            "themes": ["沟通", "财商", "个人成长"],
            "seed_entities": ["示例甲", "示例乙"],
            "candidate_titles": [
                "候选一",
                "候选二",
                "候选三",
                "候选四",
                "候选五",
                "候选六",
            ],
            "publication_recency_required": False,
            "response_style": "conversational",
            "search_queries": ["沟通与财商图书推荐"],
            "interpretation_note": "年份表示阅读计划，而非出版年份。",
        }

        class FakeModel:
            async def ainvoke(self, prompt):
                self.prompt = prompt
                return type("Response", (), {"content": json.dumps(payload, ensure_ascii=False)})()

        with mock.patch(
            "app.services.research.objective_planner._resolve_model_id",
            return_value="model-id",
        ), mock.patch(
            "app.infra.llm.get_llm",
            return_value=FakeModel(),
        ), mock.patch(
            "app.services.research.objective_planner.report_model_completion",
        ):
            plan = asyncio.run(
                plan_research_objective(
                    "今年想读一些和示例甲、示例乙相近的书",
                    model_id="model-id",
                )
            )

        self.assertEqual(plan.provider, "runtime_llm")
        self.assertEqual(plan.task_type, "book_recommendation")
        self.assertFalse(plan.publication_recency_required)
        self.assertEqual(plan.response_style, "conversational")
        self.assertEqual(
            plan.candidate_titles,
            ["候选一", "候选二", "候选三", "候选四", "候选五", "候选六"],
        )
        self.assertEqual(plan.search_queries, ["沟通与财商图书推荐"])
        self.assertEqual(len(candidate_verification_queries(plan)), 2)

    def test_invalid_model_plan_falls_back_without_inventing_candidates(self) -> None:
        class InvalidModel:
            async def ainvoke(self, prompt):
                del prompt
                return type("Response", (), {"content": "not json"})()

        with mock.patch(
            "app.services.research.objective_planner._resolve_model_id",
            return_value="model-id",
        ), mock.patch(
            "app.infra.llm.get_llm",
            return_value=InvalidModel(),
        ), mock.patch(
            "app.services.research.objective_planner.report_model_completion",
        ):
            plan = asyncio.run(
                plan_research_objective(
                    "推荐几本适合今年阅读的书",
                    model_id="model-id",
                )
            )

        self.assertEqual(plan.provider, "deterministic")
        self.assertEqual(plan.task_type, "book_recommendation")
        self.assertEqual(plan.candidate_titles, [])
        self.assertTrue(plan.error)
        self.assertEqual(plan.response_style, "conversational")

    def test_formal_report_style_requires_the_semantic_plan_to_request_it(self) -> None:
        conversational = ResearchObjectivePlan()
        formal = ResearchObjectivePlan(response_style="formal_report")
        self.assertEqual(conversational.response_style, "conversational")
        self.assertEqual(formal.response_style, "formal_report")


if __name__ == "__main__":
    unittest.main()
