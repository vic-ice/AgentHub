from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from app.services.research.objective_planner import (
    ResearchObjectivePlan,
    candidate_search_hints,
    candidate_verification_queries,
    plan_research_objective,
)


class ResearchObjectivePlannerTests(unittest.TestCase):
    def test_candidate_aliases_and_seed_variants_are_removed_with_aligned_hints(self) -> None:
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
        self.assertEqual(plan.candidate_titles, ["原则：生活与工作", "影响力"])
        self.assertEqual(
            candidate_search_hints(plan),
            {
                "原则：生活与工作": "原则生活与工作 瑞达利欧",
                "影响力": "影响力 罗伯特西奥迪尼",
            },
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
        queries = candidate_verification_queries(plan)
        self.assertEqual(len(queries), 2)
        self.assertIn("《候选一》", queries[0])
        self.assertIn("《候选六》", queries[1])

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
