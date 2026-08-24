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


BACKEND_DIR = Path(__file__).resolve().parents[1]


class ResponsePresentationArchitectureTests(unittest.TestCase):
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
                    )
                ),
            )
        )
        content = "\n".join(str(message.content) for message in messages)
        self.assertIn("trusted_response_view", content)
        self.assertIn("候选书", content)
        self.assertIn("Never omit every admitted title", content)
        self.assertIn("present every admitted book exactly once", content)
        self.assertIn("response_depth", content)
        self.assertIn("coverage", content)
        self.assertNotIn("Available high-level capabilities", content)
        self.assertNotIn("trusted_receipts", content)
        self.assertEqual(len(messages), 3)

    def test_deep_research_defaults_to_conversation_not_report_voice(self) -> None:
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
        self.assertIn("真诚、有判断力、懂阅读体验", conversational)
        self.assertIn("不写“研究结论”", conversational)
        self.assertNotIn("深度研究报告编辑", conversational)
        self.assertIn("深度研究报告编辑", formal)

    def test_publication_policy_does_not_require_fixed_markdown_shape(self) -> None:
        path = BACKEND_DIR / "app/services/agent_core/publication/policy.py"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("_MARKDOWN_STRUCTURE_RE", source)
        self.assertNotIn("long external synthesis must use structured", source)


if __name__ == "__main__":
    unittest.main()
