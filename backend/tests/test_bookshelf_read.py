from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.models.memory import MemoryEventRecord
from app.schemas.book import ShelfBook
from app.services.agent_core.capabilities import CapabilityRegistry
from app.services.agent_core.compiler import WorkflowCompiler
from app.services.agent_core.contracts import (
    CapabilityProposalBatch,
    ControllerOutput,
    ControllerToolCall,
    ValidatedCapabilityProposal,
)
from app.services.agent_core.core_capabilities import CoreCapabilityAvailability
from app.services.agent_core.proposal_validator import ProposalValidator
from app.services.agent_core.publication.bookshelf_renderer import render_bookshelf_read
from app.services.agent_runtime.contracts import ExecutionContext
from app.services.books import read_runtime
from app.services.memory.version_store import MemoryVersionStore


USER_ID = uuid4()
THREAD_ID = uuid4()
NOW = datetime.now(timezone.utc)


def _shelf_book(
    title: str,
    status: str,
    evaluation: str | None,
) -> ShelfBook:
    return ShelfBook(
        id=uuid4(),
        user_id=USER_ID,
        book_id=None,
        title=title,
        authors=[],
        tags=[],
        cover_url=None,
        source_url=None,
        reading_status=status,
        evaluation=evaluation,
        note="",
        rating=None,
        last_event_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


class BookshelfCapabilityContractTests(unittest.TestCase):
    def test_registry_exposes_one_typed_bookshelf_read(self):
        registry = CapabilityRegistry(
            core_availability=CoreCapabilityAvailability.all_enabled()
        )
        self.assertIn("bookshelf_read", registry.enabled_names)
        schema = next(
            item["function"]
            for item in registry.tool_schemas()
            if item["function"]["name"] == "bookshelf_read"
        )
        self.assertIn("authoritative current Bookshelf", schema["description"])
        self.assertIn("Shelf membership, not status=read", schema["description"])
        self.assertFalse(schema["parameters"]["additionalProperties"])

    def test_compiler_maps_bookshelf_read_to_single_native_operation(self):
        plan = WorkflowCompiler().compile(
            CapabilityProposalBatch(
                proposals=[
                    ValidatedCapabilityProposal(
                        call_id="shelf",
                        capability="bookshelf_read",
                        arguments={
                            "scope": "current",
                            "statuses": [],
                            "evaluations": [],
                            "query": "",
                            "limit": 5000,
                        },
                        side_effect=False,
                    )
                ]
            ),
            goal="我的书架有哪些书？",
        )
        self.assertEqual(plan.response_mode, "receipt")
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0].operation, "bookshelf_read_v1")
        self.assertEqual(plan.actions[0].capability, "books")

    def test_validator_merges_duplicate_bookshelf_reads_and_filters(self):
        registry = CapabilityRegistry(
            core_availability=CoreCapabilityAvailability.all_enabled()
        )
        batch = ProposalValidator(registry).validate(
            ControllerOutput(
                mode="capability_proposals",
                tool_calls=[
                    ControllerToolCall(
                        call_id="titles",
                        name="bookshelf_read",
                        arguments={"statuses": ["read"]},
                    ),
                    ControllerToolCall(
                        call_id="states",
                        name="bookshelf_read",
                        arguments={"statuses": ["want_to_read"]},
                    ),
                ],
            )
        )
        self.assertEqual(len(batch.proposals), 1)
        self.assertEqual(batch.proposals[0].call_id, "titles")
        self.assertEqual(
            batch.proposals[0].arguments["statuses"],
            ["read", "want_to_read"],
        )
    def test_validator_suppresses_current_reading_memory_when_shelf_owns_read(self):
        registry = CapabilityRegistry(
            core_availability=CoreCapabilityAvailability.all_enabled()
        )
        batch = ProposalValidator(registry).validate(
            ControllerOutput(
                mode="capability_proposals",
                tool_calls=[
                    ControllerToolCall(
                        call_id="shelf",
                        name="bookshelf_read",
                        arguments={},
                    ),
                    ControllerToolCall(
                        call_id="derived-memory",
                        name="search_memory",
                        arguments={
                            "scope": "current",
                            "predicate": "evaluation",
                        },
                    ),
                ],
            )
        )
        self.assertEqual(
            [item.capability for item in batch.proposals],
            ["bookshelf_read"],
        )

    def test_validator_isolates_invalid_read_fallback_after_valid_shelf_owner(self):
        registry = CapabilityRegistry(
            core_availability=CoreCapabilityAvailability.all_enabled()
        )
        batch = ProposalValidator(registry).validate(
            ControllerOutput(
                mode="capability_proposals",
                tool_calls=[
                    ControllerToolCall(
                        call_id="shelf",
                        name="bookshelf_read",
                        arguments={},
                    ),
                    ControllerToolCall(
                        call_id="invalid-memory-fallback",
                        name="search_memory",
                        arguments={},
                    ),
                ],
            )
        )
        self.assertEqual(
            [item.capability for item in batch.proposals],
            ["bookshelf_read"],
        )

    def test_validator_rejects_invalid_read_without_authoritative_owner(self):
        registry = CapabilityRegistry(
            core_availability=CoreCapabilityAvailability.all_enabled()
        )
        with self.assertRaises(ValueError):
            ProposalValidator(registry).validate(
                ControllerOutput(
                    mode="capability_proposals",
                    tool_calls=[
                        ControllerToolCall(
                            call_id="invalid-memory",
                            name="search_memory",
                            arguments={},
                        )
                    ],
                )
            )
    def test_validator_suppresses_query_only_current_memory_fallback(self):
        registry = CapabilityRegistry(
            core_availability=CoreCapabilityAvailability.all_enabled()
        )
        batch = ProposalValidator(registry).validate(
            ControllerOutput(
                mode="capability_proposals",
                tool_calls=[
                    ControllerToolCall(
                        call_id="shelf",
                        name="bookshelf_read",
                        arguments={},
                    ),
                    ControllerToolCall(
                        call_id="semantic-fallback",
                        name="search_memory",
                        arguments={
                            "scope": "current",
                            "query": "current shelf overview",
                        },
                    ),
                ],
            )
        )
        self.assertEqual(
            [item.capability for item in batch.proposals],
            ["bookshelf_read"],
        )
    def test_validator_preserves_non_overlapping_or_historical_memory_reads(self):
        registry = CapabilityRegistry(
            core_availability=CoreCapabilityAvailability.all_enabled()
        )
        batch = ProposalValidator(registry).validate(
            ControllerOutput(
                mode="capability_proposals",
                tool_calls=[
                    ControllerToolCall(
                        call_id="shelf",
                        name="bookshelf_read",
                        arguments={},
                    ),
                    ControllerToolCall(
                        call_id="job",
                        name="search_memory",
                        arguments={
                            "scope": "current",
                            "predicate": "occupation",
                        },
                    ),
                    ControllerToolCall(
                        call_id="reading-history",
                        name="search_memory",
                        arguments={
                            "scope": "previous",
                            "predicate": "evaluation",
                        },
                    ),
                ],
            )
        )
        self.assertEqual(
            [item.call_id for item in batch.proposals],
            ["shelf", "job", "reading-history"],
        )
    def test_renderer_aggregates_each_shelf_entry_once(self):
        output = {
            "status": "completed",
            "total": 3,
            "items": [
                _shelf_book("财富自由之路", "want_to_read", "liked").model_dump(mode="json"),
                _shelf_book("失控", "want_to_read", "neutral").model_dump(mode="json"),
                _shelf_book("小狗钱钱", "read", "liked").model_dump(mode="json"),
            ],
        }
        rendered = render_bookshelf_read(output)
        self.assertIn("目前有 3 本书", rendered)
        self.assertEqual(rendered.count("《财富自由之路》"), 1)
        self.assertEqual(rendered.count("《失控》"), 1)
        self.assertEqual(rendered.count("《小狗钱钱》"), 1)
        self.assertIn("《失控》：想读；评价：一般", rendered)
        self.assertNotIn("长期记忆", rendered)


class BookshelfReadRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_reads_only_through_reading_service(self):
        session = object()

        @asynccontextmanager
        async def session_context():
            yield session

        database = SimpleNamespace(session=Mock(side_effect=session_context))
        service = SimpleNamespace(
            ensure_backfilled=AsyncMock(),
            list_entries=AsyncMock(
                return_value=([_shelf_book("失控", "want_to_read", "neutral")], 1)
            ),
        )
        context = ExecutionContext(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            request_id="bookshelf-read-test",
        )
        with (
            patch.object(read_runtime, "get_database", return_value=database),
            patch.object(read_runtime, "ReadingService", return_value=service),
        ):
            result = await read_runtime.execute_bookshelf_read({}, context=context)

        service.ensure_backfilled.assert_awaited_once_with(USER_ID)
        service.list_entries.assert_awaited_once()
        self.assertEqual(result["result_mode"], "bookshelf_read_receipt")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "失控")


class MemoryForgetTombstoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_tombstone_inherits_classification_and_entity(self):
        entity_id = uuid4()
        previous = MemoryEventRecord(
            id=uuid4(),
            user_id=USER_ID,
            thread_id=THREAD_ID,
            type="entity",
            subject="book",
            value='{"book_title":"失控"}',
            polarity="neutral",
            confidence=1.0,
            source="chat_turn",
            domain="reading",
            kind="state",
            entity_id=entity_id,
            metadata_json={
                "memory_v2": {
                    "subject": "book",
                    "predicate": "reading_status",
                }
            },
            chain_id=uuid4(),
            schema_key="reading.state",
            memory_key=f"entity:{entity_id}:reading_status",
            version_no=1,
            operation="create",
            schema_version=1,
            valid_from=NOW,
            is_deleted=False,
        )
        session = Mock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock()
        store = MemoryVersionStore(session)

        tombstone = await store._insert_tombstone(
            previous,
            evidence_quote="忘记这条",
            source_event_id=uuid4(),
            source_kind="admin_action",
            receipt_id="forget-test",
            user_id=USER_ID,
            thread_id=THREAD_ID,
        )

        self.assertEqual(tombstone.domain, "reading")
        self.assertEqual(tombstone.kind, "state")
        self.assertEqual(tombstone.entity_id, entity_id)
        self.assertEqual(
            tombstone.metadata_json["memory_v2"]["predicate"],
            "reading_status",
        )
        self.assertEqual(
            tombstone.metadata_json["memory_v2"]["provenance"]["source_kind"],
            "admin_action",
        )


if __name__ == "__main__":
    unittest.main()
