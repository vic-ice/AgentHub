from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.services.agent_core.prompt_composer import PromptComposer
from app.services.agent_core.prompt_contracts import (
    ControllerContextSnapshot,
    ControllerModelRequest,
)
from app.services.agent_core.publication.response_view import (
    AnswerSourceView,
    ExternalAnswerView,
)
from app.services.research.publication.report_writer import _prompt_freeform
from app.services.research.publication.report_writer import (
    build_research_presentation_contract,
)
from app.services.research.publication.packet import (
    build_research_publication_packet,
)
from app.services.research.publication.quality import evaluate_publication_axes
from app.services.user_answer_contracts import build_user_answer_brief


BACKEND_DIR = Path(__file__).resolve().parents[1]


class ResponsePresentationArchitectureTests(unittest.TestCase):
    def test_finance_book_request_uses_planned_dimensions_not_topic_patch(self) -> None:
        contract = build_research_presentation_contract(
            objective="推荐几本适合普通上班族的理财书籍",
            review={
                "objective_plan": {
                    "task_type": "book_recommendation",
                    "answer_depth": "deep",
                    "decision_dimensions": [
                        "风险观是否稳健",
                        "是否适用于中国读者",
                        "从知识到行动的可执行性",
                    ],
                    "critical_unknowns": ["读者当前是否有高息负债"],
                }
            },
            language="zh-CN",
            candidate_count=6,
        )

        self.assertEqual(contract["layout"], "adaptive_user_answer")
        self.assertEqual(
            contract["decision_dimensions"],
            [
                "风险观是否稳健",
                "是否适用于中国读者",
                "从知识到行动的可执行性",
            ],
        )
        self.assertIn("读者当前是否有高息负债", contract["critical_unknowns"])
        self.assertFalse(contract["route_required"])

    def test_general_research_gets_analytical_not_book_layout(self) -> None:
        contract = build_research_presentation_contract(
            objective="分析远程办公对研发团队协作质量的影响",
            review={
                "objective_plan": {
                    "task_type": "general_research",
                    "decision_dimensions": ["交付效率", "沟通成本", "创新质量"],
                }
            },
            language="zh-CN",
            candidate_count=0,
        )

        self.assertEqual(contract["layout"], "adaptive_user_answer")
        self.assertEqual(contract["table_columns"], [])
        self.assertEqual(contract["required_sections"], [])

    def test_reading_year_is_encoded_as_decision_horizon(self) -> None:
        brief = build_user_answer_brief(
            "2026年有什么值得看的个人成长书？",
            task_type="book_recommendation",
        )
        fresh = build_user_answer_brief(
            "推荐几本2026年新出版的个人成长书",
            task_type="book_recommendation",
        )

        self.assertEqual(brief.temporal_intent, "decision_horizon")
        self.assertEqual(fresh.temporal_intent, "publication_recency")

    def test_response_view_has_no_domain_execution_dependency(self) -> None:
        path = (
            BACKEND_DIR
            / "app/services/agent_core/publication/response_view.py"
        )
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        forbidden = (
            "app.infra",
            "app.crud",
            "recommendation_service",
            "external_search",
            "memory.write_gateway",
        )
        self.assertFalse(
            any(token in module for module in imported for token in forbidden),
            imported,
        )

    def test_controller_presentation_prompt_is_user_facing(self) -> None:
        prompt = PromptComposer().core_prompt()
        self.assertIn("warm, capable reading companion", prompt)
        self.assertIn("reference_titles", prompt)
        self.assertNotIn("State evidence limitations honestly", prompt)

    def test_synthesis_gets_compact_user_facing_view(self) -> None:
        messages = PromptComposer().compose(
            ControllerModelRequest(
                phase="synthesis",
                model_name="test",
                current_user_message="推荐几本类似的书",
                context=ControllerContextSnapshot(
                    response_view=ExternalAnswerView(
                        answer_type="book_recommendation",
                        status="ready",
                        books=[
                            AnswerSourceView(
                                title="候选书",
                                url="https://book.example/candidate",
                                summary="适合入门。",
                            )
                        ],
                        user_answer_brief=build_user_answer_brief(
                            "推荐几本类似的书",
                            task_type="book_recommendation",
                            decision_dimensions=["入门难度"],
                        ),
                    )
                ),
            )
        )
        content = "\n".join(str(message.content) for message in messages)
        self.assertIn("trusted_response_view", content)
        self.assertIn("候选书", content)
        self.assertIn("Never omit every admitted title", content)
        self.assertIn("one comparison block", content)
        self.assertIn("only one detailed description", content)
        self.assertIn("response_depth", content)
        self.assertIn("coverage", content)
        self.assertIn("user_answer_brief", content)
        self.assertIn("入门难度", content)
        self.assertIn("final answer the user should read", content)
        self.assertIn("not the boundary of your intelligence", content)
        self.assertIn("stable conceptual knowledge", content)
        self.assertIn("externally verified fact lane", content)
        self.assertIn("do not let that gap erase", content)
        self.assertIn("Honor user_answer_brief.temporal_intent exactly", content)
        self.assertNotIn("Available high-level capabilities", content)
        self.assertNotIn("trusted_receipts", content)
        self.assertEqual(len(messages), 3)

    def test_synthesis_without_evidence_requests_final_markdown_not_json(self) -> None:
        messages = PromptComposer().compose(
            ControllerModelRequest(
                phase="synthesis",
                model_name="test",
                current_user_message="推荐几本个人成长书",
                context=ControllerContextSnapshot(),
            )
        )
        content = "\n".join(str(message.content) for message in messages)
        self.assertIn("retrieval attempt returned no usable evidence", content)
        self.assertIn("final Markdown answer", content)
        self.assertIn("Never return JSON", content)
        self.assertNotIn("trusted_response_view", content)

    def test_deep_research_defaults_to_senior_editor_not_evidence_template(self) -> None:
        common = {
            "objective": "帮我找几本适合入门的书",
            "language": "zh-CN",
            "evidence": [
                {
                    "claim": "《候选书》适合作为入门读物。",
                    "source_title": "示例来源",
                    "source_url": "https://example.com/book",
                }
            ],
            "limitations": [],
        }
        conversational = _prompt_freeform(
            **common,
            review={
                "objective_plan": {
                    "task_type": "book_recommendation",
                    "response_style": "conversational",
                }
            },
        )
        formal = _prompt_freeform(
            **common,
            review={
                "objective_plan": {
                    "task_type": "book_recommendation",
                    "response_style": "formal_report",
                }
            },
        )
        self.assertIn("深度研究报告的主编", conversational)
        self.assertIn("一个信息只完整表达一次", conversational)
        self.assertIn("结构服从问题", conversational)
        self.assertIn("唯一的发布数据包", conversational)
        self.assertNotIn("深度研究报告编辑", conversational)
        self.assertIn("深度研究报告编辑", formal)

    def test_deep_writer_never_receives_raw_reviewer_workspace(self) -> None:
        prompt = _prompt_freeform(
            objective="分析远程办公对研发协作的影响",
            language="zh-CN",
            evidence=[
                {
                    "claim": "异步协作可以降低会议依赖。",
                    "source_title": "研究来源",
                    "source_url": "https://example.com/research",
                    "quality": "high",
                    "research_round": 3,
                    "relevance": 5,
                }
            ],
            limitations=[],
            review={
                "objective_plan": {
                    "task_type": "general_research",
                    "decision_dimensions": ["交付效率", "沟通成本"],
                },
                "known_summary": "INTERNAL_REVIEW_SENTINEL",
                "missing_questions": ["INTERNAL_GAP_SENTINEL"],
                "provider": "runtime_llm",
                "round_index": 3,
            },
        )

        self.assertIn("交付效率", prompt)
        self.assertNotIn("INTERNAL_REVIEW_SENTINEL", prompt)
        self.assertNotIn("INTERNAL_GAP_SENTINEL", prompt)
        self.assertNotIn('"research_round"', prompt)
        self.assertNotIn('"relevance"', prompt)

    def test_publication_packet_contains_user_contract_not_run_state(self) -> None:
        brief = build_user_answer_brief(
            "推荐适合普通上班族的理财书，并说明怎么选",
            task_type="book_recommendation",
            decision_dimensions=["风险观", "可执行性"],
            answer_depth="deep",
        )
        packet = build_research_publication_packet(
            brief=brief,
            evidence=[
                {
                    "claim": "《候选书》介绍基础资产配置。",
                    "source_title": "目录页",
                    "source_url": "https://example.com/book",
                    "quality": "medium",
                    "research_round": 4,
                    "provider": "internal",
                }
            ],
            allowed_recommendation_entities=["候选书"],
        )
        payload = packet.model_dump(mode="json")

        self.assertEqual(payload["user_answer_brief"]["primary_goal"], brief.primary_goal)
        self.assertEqual(payload["allowed_recommendation_entities"], ["候选书"])
        self.assertNotIn("research_round", str(payload))
        self.assertNotIn("provider", str(payload))

    def test_quality_axes_do_not_conflate_research_and_delivery(self) -> None:
        axes = evaluate_publication_axes(
            research_sufficient=False,
            render_ok=True,
            entity_safe=True,
            format_ok=True,
            candidate_quality_ok=True,
            answer_quality_ok=True,
            delivered=True,
            quality_reasons=["coverage_partial"],
        )

        self.assertFalse(axes.research_sufficient)
        self.assertTrue(axes.publication_safe)
        self.assertTrue(axes.answer_quality_pass)
        self.assertTrue(axes.delivery_succeeded)

    def test_format_and_breadth_failure_do_not_make_safe_answer_unpublishable(self) -> None:
        axes = evaluate_publication_axes(
            research_sufficient=True,
            render_ok=True,
            entity_safe=True,
            format_ok=False,
            candidate_quality_ok=False,
            answer_quality_ok=True,
            delivered=True,
            quality_reasons=["requested_format_missing", "portfolio_too_narrow"],
        )

        self.assertTrue(axes.publication_safe)
        self.assertFalse(axes.answer_quality_pass)
        self.assertTrue(axes.delivery_succeeded)

    def test_publication_policy_does_not_require_fixed_markdown_shape(self) -> None:
        path = BACKEND_DIR / "app/services/agent_core/publication/policy.py"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("_MARKDOWN_STRUCTURE_RE", source)
        self.assertNotIn("long external synthesis must use structured", source)


if __name__ == "__main__":
    unittest.main()
