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
    MemoryWriteGateway,
    _normalize_evaluation,
    _normalize_reading_note,
    _normalize_reading_status,
)
from app.services.recommendation_projection import (
    RecommendationCandidateProjection,
    RecommendationProjector,
)
from app.services.books.reading_service import normalize_book_title
from app.services.books.reading_service import ReadingService
from app.services.agent_core.publication.memory_renderer import render_memory_mutation
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

    def test_reading_note_is_preserved_in_the_controller_batch(self):
        compiled = compiled_turn_from_assertions(
            [
                MemoryAssertionProposal(
                    subject="Python编程：从入门到实践",
                    predicate="reading_status",
                    value={"reading_status": "reading"},
                    evidence_quote="我正在读《Python编程：从入门到实践》",
                    domain="reading",
                    kind="state",
                    entity_type="book",
                ),
                MemoryAssertionProposal(
                    subject="Python编程：从入门到实践",
                    predicate="note",
                    value={"note": "学习python"},
                    evidence_quote="备注是学习python",
                    domain="reading",
                    kind="state",
                    entity_type="book",
                ),
            ],
            raw_text=(
                "我正在读《Python编程：从入门到实践》，"
                "备注是学习python"
            ),
        )

        self.assertEqual(len(compiled.facts), 1)
        self.assertEqual(
            compiled.facts[0].attributes,
            {"reading_status": "reading", "note": "学习python"},
        )

    def test_runtime_only_validates_canonical_values(self):
        self.assertEqual(_normalize_reading_status("read"), "read")
        self.assertEqual(_normalize_evaluation("disliked"), "disliked")
        self.assertIsNone(_normalize_reading_status("读完"))
        self.assertIsNone(_normalize_reading_status("finished"))
        self.assertIsNone(_normalize_evaluation("不喜欢"))
        self.assertIsNone(_normalize_evaluation("hate"))
        self.assertEqual(_normalize_reading_note("  学习python  "), "学习python")


class ReadingWriteExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_note_reaches_reading_service_and_user_visible_receipt(self):
        user_id = uuid4()
        entity_id = uuid4()
        shelf = SimpleNamespace(
            title="Python编程：从入门到实践",
            note="学习python",
            model_dump=Mock(
                return_value={
                    "title": "Python编程：从入门到实践",
                    "reading_status": "reading",
                    "evaluation": "liked",
                    "note": "学习python",
                }
            ),
        )
        service = Mock()
        service.upsert = AsyncMock(
            return_value=SimpleNamespace(shelf=shelf, events=[])
        )
        resolver = Mock()
        resolver.resolve = AsyncMock(
            return_value=SimpleNamespace(
                status="existing",
                entity_id=entity_id,
                question="",
            )
        )
        fact = SimpleNamespace(
            entity="Python编程：从入门到实践",
            attributes={
                "reading_status": "reading",
                "evaluation": "liked",
                "note": "学习python",
            },
            source_excerpt="备注是学习python",
        )
        gateway = MemoryWriteGateway(Mock())
        gateway.record_reading_memory = AsyncMock(
            return_value={"status": "noop", "mutations": []}
        )

        with (
            patch(
                "app.services.memory.write_gateway.EntityResolver",
                return_value=resolver,
            ),
            patch(
                "app.services.books.reading_service.ReadingService",
                return_value=service,
            ),
        ):
            outcome = await gateway._commit_reading(
                [fact],
                user_id=user_id,
                thread_id=None,
                raw_text="备注是学习python",
                source_event_id=uuid4(),
                receipt_id="reading-note-test",
                source_kind="user_message",
            )

        self.assertEqual(service.upsert.await_args.kwargs["note"], "学习python")
        self.assertEqual(
            render_memory_mutation(outcome),
            "已将《Python编程：从入门到实践》的阅读状态设置为“在读”。\n"
            "已将《Python编程：从入门到实践》的阅读评价更新为“喜欢”。\n"
            "已保存《Python编程：从入门到实践》的备注：“学习python”。",
        )


class ReadingRemovalConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_forgets_only_matching_reading_projections(self):
        user_id = uuid4()
        gateway = MemoryWriteGateway(Mock())
        gateway.forget = AsyncMock(
            return_value={"status": "forgotten", "receipt": {}}
        )
        current = [
            SimpleNamespace(
                memory_key="entity:book:reading_status",
                schema_key="reading.state",
                predicate="reading_status",
                value={"book_title": "Python编程：从入门到实践"},
            ),
            SimpleNamespace(
                memory_key="entity:book:evaluation",
                schema_key="reading.feedback",
                predicate="evaluation",
                value={"book_title": "《Python编程：从入门到实践》"},
            ),
            SimpleNamespace(
                memory_key="entity:other:reading_status",
                schema_key="reading.state",
                predicate="reading_status",
                value={"book_title": "流畅的Python"},
            ),
            SimpleNamespace(
                memory_key="general:note",
                schema_key="general.fact",
                predicate="note",
                value={"entity": "Python编程：从入门到实践"},
            ),
        ]

        with patch(
            "app.services.memory.write_gateway.MemoryVersionStore"
        ) as store_type:
            store_type.return_value.list_current = AsyncMock(return_value=current)
            await gateway.forget_reading_memory(
                user_id=user_id,
                thread_id=None,
                book_title="Python编程：从入门到实践",
                source_event_id=uuid4(),
                receipt_id="remove-reading-test",
                evidence_quote="移出书架：《Python编程：从入门到实践》",
            )

        self.assertEqual(
            gateway.forget.await_args.kwargs["memory_keys"],
            ["entity:book:reading_status", "entity:book:evaluation"],
        )

    async def test_shelf_delete_and_memory_forget_share_one_service_transaction(self):
        entry = SimpleNamespace(
            id=uuid4(),
            user_id=uuid4(),
            title="Python编程：从入门到实践",
        )
        session = Mock()
        session.get = AsyncMock(return_value=entry)
        session.delete = AsyncMock()
        session.flush = AsyncMock()
        memory_gateway = Mock()
        memory_gateway.forget_reading_memory = AsyncMock(
            return_value={"status": "forgotten"}
        )

        with patch(
            "app.services.memory.write_gateway.MemoryWriteGateway",
            return_value=memory_gateway,
        ):
            removed = await ReadingService(session).remove_entry(entry.id)

        self.assertTrue(removed)
        memory_gateway.forget_reading_memory.assert_awaited_once()
        session.delete.assert_awaited_once_with(entry)
        session.flush.assert_awaited_once()

    async def test_failed_memory_forget_does_not_delete_shelf_row(self):
        entry = SimpleNamespace(
            id=uuid4(),
            user_id=uuid4(),
            title="Python编程：从入门到实践",
        )
        session = Mock()
        session.get = AsyncMock(return_value=entry)
        session.delete = AsyncMock()
        memory_gateway = Mock()
        memory_gateway.forget_reading_memory = AsyncMock(
            side_effect=RuntimeError("version store unavailable")
        )

        with patch(
            "app.services.memory.write_gateway.MemoryWriteGateway",
            return_value=memory_gateway,
        ):
            with self.assertRaises(RuntimeError):
                await ReadingService(session).remove_entry(entry.id)

        session.delete.assert_not_awaited()


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
