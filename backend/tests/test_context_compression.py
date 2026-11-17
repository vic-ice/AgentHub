from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.agent_core.context_assembler import (
    ContextAssembler,
    ContextCompressionRequired,
    ConversationContextMaterial,
)
from app.services.agent_core.context_coordinator import (
    ControllerContextCoordinator,
)
from app.services.conversation.journal_contracts import ConversationJournalEvent
from app.services.conversation.summary_builder import (
    SummaryBuildError,
    SummaryBuilder,
)
from app.services.conversation.summary_contracts import (
    ConversationSummary,
    ConversationSummaryDraft,
    StructuredConversationSummary,
    SummaryStatement,
)


USER_ID = uuid.uuid4()
THREAD_ID = uuid.uuid4()


def _event(
    sequence: int,
    role: str,
    *,
    exchange_id: uuid.UUID,
    content: str | None = None,
) -> ConversationJournalEvent:
    return ConversationJournalEvent(
        id=uuid.uuid4(),
        user_id=USER_ID,
        thread_id=THREAD_ID,
        request_id=f"request-{exchange_id}",
        exchange_id=exchange_id,
        sequence_no=sequence,
        event_type="user_message" if role == "user" else "assistant_published",
        role=role,
        content=content or f"{role}-{sequence}",
        receipt_refs=["receipt-1"] if role == "assistant" else [],
        created_at=datetime.now(timezone.utc),
    )


def _events(exchange_count: int) -> list[ConversationJournalEvent]:
    events: list[ConversationJournalEvent] = []
    for index in range(exchange_count):
        exchange_id = uuid.uuid4()
        events.extend(
            [
                _event(index * 2 + 1, "user", exchange_id=exchange_id),
                _event(index * 2 + 2, "assistant", exchange_id=exchange_id),
            ]
        )
    return events


class _SummaryProvider:
    def __init__(self, *, invalid_sequence: int | None = None) -> None:
        self.invalid_sequence = invalid_sequence

    async def summarize(self, *, previous, events):
        source = self.invalid_sequence or events[-1].sequence_no
        prior = (
            list(previous.structured_content.user_requests)
            if previous is not None
            else []
        )
        return ConversationSummaryDraft(
            model_id="fixture-summary-model",
            structured_content=StructuredConversationSummary(
                user_requests=[
                    *prior,
                    SummaryStatement(
                        text=f"覆盖到事件 {events[-1].sequence_no}",
                        source_sequences=[source],
                    ),
                ],
                receipt_refs=["receipt-1"],
            ),
        )


class _DroppingProvider:
    async def summarize(self, *, previous, events):
        return ConversationSummaryDraft(
            model_id="fixture-summary-model",
            structured_content=StructuredConversationSummary(),
        )


class SummaryBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_is_cumulative_and_source_hashed(self) -> None:
        first = await SummaryBuilder(_SummaryProvider()).build(
            previous=None,
            events=_events(2),
        )
        self.assertEqual((first.from_sequence, first.to_sequence), (1, 4))
        self.assertEqual(len(first.source_hash), 64)
        self.assertEqual(first.structured_content.receipt_refs, ["receipt-1"])

        previous = ConversationSummary(
            id=uuid.uuid4(),
            created_at=datetime.now(timezone.utc),
            **first.model_dump(mode="python"),
        )
        next_events = _events(1)
        shifted = [
            event.model_copy(update={"sequence_no": event.sequence_no + 4})
            for event in next_events
        ]
        second = await SummaryBuilder(_SummaryProvider()).build(
            previous=previous,
            events=shifted,
        )
        self.assertEqual((second.from_sequence, second.to_sequence), (1, 6))
        self.assertEqual(second.previous_summary_id, previous.id)
        self.assertEqual(
            len(second.structured_content.user_requests),
            2,
        )
        self.assertNotEqual(first.source_hash, second.source_hash)

    async def test_cumulative_summary_cannot_drop_prior_items(self) -> None:
        first = await SummaryBuilder(_SummaryProvider()).build(
            previous=None,
            events=_events(1),
        )
        previous = ConversationSummary(
            id=uuid.uuid4(),
            created_at=datetime.now(timezone.utc),
            **first.model_dump(mode="python"),
        )
        shifted = [
            event.model_copy(update={"sequence_no": event.sequence_no + 2})
            for event in _events(1)
        ]
        with self.assertRaisesRegex(
            SummaryBuildError,
            "dropped prior user_requests",
        ):
            await SummaryBuilder(_DroppingProvider()).build(
                previous=previous,
                events=shifted,
            )

    async def test_summary_rejects_unsupported_citation(self) -> None:
        with self.assertRaisesRegex(
            SummaryBuildError,
            "outside covered range",
        ):
            await SummaryBuilder(
                _SummaryProvider(invalid_sequence=99)
            ).build(previous=None, events=_events(1))

    async def test_summary_must_end_at_published_assistant(self) -> None:
        events = _events(1)[:-1]
        with self.assertRaisesRegex(
            SummaryBuildError,
            "published assistant",
        ):
            await SummaryBuilder(_SummaryProvider()).build(
                previous=None,
                events=events,
            )


class ContextAssemblerTests(unittest.TestCase):
    def test_twenty_exact_exchanges_fit_without_summary(self) -> None:
        result = ContextAssembler(
            recent_exchange_limit=20,
            token_budget=50_000,
        ).assemble(
            ConversationContextMaterial(events=_events(20)),
            current_user_message="current",
        )
        self.assertEqual(result.exact_exchange_count, 20)
        self.assertEqual(len(result.snapshot.conversation), 40)
        self.assertFalse(result.summary_used)

    def test_uncovered_old_exchange_requires_summary(self) -> None:
        with self.assertRaises(ContextCompressionRequired) as raised:
            ContextAssembler(
                recent_exchange_limit=20,
                token_budget=50_000,
            ).assemble(
                ConversationContextMaterial(events=_events(21)),
                current_user_message="current",
            )
        self.assertEqual(raised.exception.through_sequence, 2)

    def test_summary_and_recent_exact_events_are_distinct(self) -> None:
        events = _events(1)
        candidate = ConversationSummary(
            id=uuid.uuid4(),
            user_id=USER_ID,
            thread_id=THREAD_ID,
            from_sequence=1,
            to_sequence=2,
            structured_content=StructuredConversationSummary(
                user_requests=[
                    SummaryStatement(
                        text="旧请求摘要",
                        source_sequences=[1],
                    )
                ]
            ),
            source_hash="a" * 64,
            model_id="fixture",
            prompt_version="conversation-summary-v1",
            created_at=datetime.now(timezone.utc),
        )
        shifted = [
            event.model_copy(update={"sequence_no": event.sequence_no + 2})
            for event in events
        ]
        result = ContextAssembler(
            token_budget=50_000
        ).assemble(
            ConversationContextMaterial(
                summary=candidate,
                events=shifted,
            ),
            current_user_message="current",
        )
        self.assertTrue(result.summary_used)
        self.assertEqual(
            result.snapshot.conversation_summary.user_requests,
            ["旧请求摘要"],
        )
        self.assertEqual(len(result.snapshot.conversation), 2)


class _FailingSummaryService:
    async def summarize_next(self, *args, **kwargs):
        raise SummaryBuildError("fixture summary model unavailable")


class _FixtureLoader:
    def __init__(self, material: ConversationContextMaterial) -> None:
        self.material = material

    async def load(self, db, **kwargs):
        return self.material


class _ProgressiveLoader:
    """Return the full material first, then a shrunk one after summarization."""

    def __init__(
        self,
        full: ConversationContextMaterial,
        reduced: ConversationContextMaterial,
    ) -> None:
        self.full = full
        self.reduced = reduced
        self.calls = 0

    async def load(self, db, **kwargs):
        self.calls += 1
        return self.reduced if self.calls > 1 else self.full


class _RecordingSummaryService:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize_next(self, *args, **kwargs):
        self.calls += 1


class ContextCoordinatorDegradationTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_failure_degrades_to_recent_window(self) -> None:
        material = ConversationContextMaterial(events=_events(21))
        coordinator = ControllerContextCoordinator(
            loader=_FixtureLoader(material),
            assembler=ContextAssembler(
                recent_exchange_limit=20,
                token_budget=50_000,
            ),
            summaries=_FailingSummaryService(),
        )
        result = await coordinator.prepare(
            db=None,  # fixture loader ignores the session
            user_id=USER_ID,
            thread_id=THREAD_ID,
            current_request_id="degraded-request",
            current_user_message="当前输入",
            summary_provider=object(),
        )
        self.assertTrue(result.compression_degraded)
        self.assertIn(
            "summary_failed",
            result.degradation_reason or "",
        )
        self.assertEqual(result.exact_exchange_count, 20)
        self.assertIn(
            "context_visibility_note",
            result.snapshot.model_dump(mode="json"),
        )
        self.assertFalse(result.summary_used)

    async def test_summary_rounds_exhausted_degrades(self) -> None:
        material = ConversationContextMaterial(events=_events(21))
        coordinator = ControllerContextCoordinator(
            loader=_FixtureLoader(material),
            assembler=ContextAssembler(
                recent_exchange_limit=20,
                token_budget=50_000,
            ),
            summaries=_RecordingSummaryService(),
            max_summary_rounds=1,
        )
        result = await coordinator.prepare(
            db=None,
            user_id=USER_ID,
            thread_id=THREAD_ID,
            current_request_id="degraded-request",
            current_user_message="当前输入",
            summary_provider=object(),
        )
        self.assertTrue(result.compression_degraded)
        self.assertIn(
            "compression_rounds_exhausted",
            result.degradation_reason or "",
        )
        self.assertEqual(result.exact_exchange_count, 20)

    async def test_summary_success_path_is_not_degraded(self) -> None:
        full = ConversationContextMaterial(events=_events(21))
        reduced = ConversationContextMaterial(events=_events(1))
        summaries = _RecordingSummaryService()
        coordinator = ControllerContextCoordinator(
            loader=_ProgressiveLoader(full, reduced),
            assembler=ContextAssembler(
                recent_exchange_limit=20,
                token_budget=50_000,
            ),
            summaries=summaries,
        )
        result = await coordinator.prepare(
            db=None,
            user_id=USER_ID,
            thread_id=THREAD_ID,
            current_request_id="summary-ok-request",
            current_user_message="当前输入",
            summary_provider=object(),  # the recording service ignores it
        )
        self.assertEqual(summaries.calls, 1)
        self.assertFalse(result.compression_degraded)
        self.assertEqual(result.exact_exchange_count, 1)


if __name__ == "__main__":
    unittest.main()
