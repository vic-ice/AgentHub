from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.models.book import Book
from app.services.memory.turn_compiler import (
    TurnFactCompiler,
    compiled_turn_from_assertions,
)
from app.services.memory.version_contracts import MemoryAssertionProposal
from app.services.memory.write_gateway import (
    _normalize_evaluation,
    _normalize_reading_status,
)
from app.services.recommendation_projection import (
    RecommendationCandidateProjection,
    RecommendationProjector,
)
from app.services.books.reading_service import normalize_book_title
from app.services.research import report as report_module


class SingleSemanticMultiActionTests(unittest.TestCase):
    def test_one_controller_batch_projects_multiple_books_and_dimensions(self):
        source = (
            "刚读完《海边的卡夫卡》，没太喜欢；"
            "接下来开始看《献给阿尔吉侬的花束》。"
        )
        assertions = [
            MemoryAssertionProposal(
                subject="海边的卡夫卡",
                predicate="reading_status",
                value={
                    "reading_status": "read",
                    "evaluation": "disliked",
                },
                evidence_quote="刚读完《海边的卡夫卡》，没太喜欢",
                domain="reading",
                kind="state",
                entity_type="book",
            ),
            MemoryAssertionProposal(
                subject="献给阿尔吉侬的花束",
                predicate="reading_status",
                value={"reading_status": "reading"},
                evidence_quote="接下来开始看《献给阿尔吉侬的花束》",
                domain="reading",
                kind="state",
                entity_type="book",
            ),
        ]

        with patch.object(
            TurnFactCompiler,
            "_llm_facts",
            side_effect=AssertionError("a second semantic pass is forbidden"),
        ) as semantic_call:
            compiled = compiled_turn_from_assertions(
                assertions,
                raw_text=source,
            )

        semantic_call.assert_not_called()
        self.assertEqual(len(compiled.facts), 2)
        by_title = {fact.entity: fact for fact in compiled.facts}
        self.assertEqual(
            by_title["海边的卡夫卡"].attributes,
            {"reading_status": "read", "evaluation": "disliked"},
        )
        self.assertEqual(
            by_title["献给阿尔吉侬的花束"].attributes,
            {"reading_status": "reading"},
        )
        self.assertEqual(
            by_title["海边的卡夫卡"].source_excerpt,
            "刚读完《海边的卡夫卡》，没太喜欢",
        )

    def test_same_book_separate_assertions_merge_without_dropping_other_book(self):
        assertions = [
            MemoryAssertionProposal(
                subject="The Left Hand of Darkness",
                predicate="reading_status",
                value={"status": "read"},
                evidence_quote="finished The Left Hand of Darkness",
                domain="reading",
                entity_type="book",
            ),
            MemoryAssertionProposal(
                subject="The Left Hand of Darkness",
                predicate="evaluation",
                value={"evaluation": "liked"},
                evidence_quote="really enjoyed it",
                domain="reading",
                kind="feedback",
                entity_type="book",
            ),
            MemoryAssertionProposal(
                subject="Never Let Me Go",
                predicate="reading_status",
                value={"status": "want_to_read"},
                evidence_quote="Never Let Me Go is next",
                domain="reading",
                entity_type="book",
            ),
        ]

        compiled = compiled_turn_from_assertions(
            assertions,
            raw_text=(
                "Finished The Left Hand of Darkness and really enjoyed it; "
                "Never Let Me Go is next."
            ),
        )

        self.assertEqual(len(compiled.facts), 2)
        by_title = {fact.entity: fact.attributes for fact in compiled.facts}
        self.assertEqual(
            by_title["The Left Hand of Darkness"],
            {"reading_status": "read", "evaluation": "liked"},
        )
        self.assertEqual(
            by_title["Never Let Me Go"],
            {"reading_status": "want_to_read"},
        )

    def test_runtime_only_validates_canonical_values(self):
        self.assertEqual(_normalize_reading_status("read"), "read")
        self.assertEqual(_normalize_evaluation("disliked"), "disliked")
        self.assertIsNone(_normalize_reading_status("读完"))
        self.assertIsNone(_normalize_reading_status("finished"))
        self.assertIsNone(_normalize_evaluation("不喜欢"))
        self.assertIsNone(_normalize_evaluation("hate"))


class RecommendationKnownBookTests(unittest.TestCase):
    def test_every_authoritative_shelf_status_is_not_a_new_recommendation(self):
        projector = RecommendationProjector(None)
        for status in ("want_to_read", "reading", "read", "dropped"):
            with self.subTest(status=status):
                book = Book(id=uuid4(), title=f"candidate-{status}")
                projection = RecommendationCandidateProjection(
                    book_id=book.id,
                    book_title=book.title,
                )
                projector._apply_shelf_state(
                    projection,
                    book=book,
                    shelf={
                        f"title:{normalize_book_title(book.title)}": (
                            status,
                            None,
                        )
                    },
                )
                self.assertTrue(projection.suppressed)
                self.assertIn(
                    f"shelf_status_{status}",
                    projection.suppression_reasons,
                )


class DeepResearchIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_report_does_not_read_personal_memory(self):
        orchestrator = SimpleNamespace(
            inspect_research_state=AsyncMock(return_value=object())
        )
        memory_reader = AsyncMock(
            side_effect=AssertionError(
                "Deep Research must not read Memory unless explicitly requested"
            )
        )
        report_builder = Mock(return_value="isolated-report")

        with (
            patch.object(
                report_module,
                "get_research_orchestrator",
                return_value=orchestrator,
            ),
            patch.object(
                report_module,
                "_build_memory_context",
                memory_reader,
            ),
            patch.object(
                report_module,
                "build_research_report_from_state",
                report_builder,
            ),
        ):
            result = await report_module.build_research_report(
                user_id=uuid4(),
                run_id=uuid4(),
            )

        self.assertEqual(result, "isolated-report")
        memory_reader.assert_not_awaited()
        report_builder.assert_called_once()
        memory_context = report_builder.call_args.kwargs["memory_context"]
        self.assertEqual(memory_context.memory_ids, [])
        self.assertEqual(memory_context.constraints, [])
