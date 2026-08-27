from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
from datetime import datetime, timezone

from app.services.agent_core.publication.memory_renderer import (
    render_memory_mutation,
)
from app.services.memory.turn_compiler import AtomicFact
from app.services.memory.write_gateway import MemoryWriteGateway
from app.services.memory.version_contracts import MemoryVersionRecord


class _Receipt:
    def model_dump(self, *, mode: str) -> dict:
        assert mode == "json"
        return {
            "result_mode": "memory_mutation_receipt",
            "receipt_id": "test-receipt",
            "status": "committed",
            "mutations": [],
        }


def _named_cat_facts() -> list[AtomicFact]:
    return [
        AtomicFact(
            entity="三花猫咪",
            subject="self",
            entity_type="pet",
            domain="personal",
            kind="state",
            predicate="has",
            attributes={"entity": "三花猫咪"},
            summary="我有一只三花猫咪",
            source_excerpt="我有一只三花猫咪，名字叫小花",
        ),
        AtomicFact(
            entity="三花猫咪",
            subject="self",
            entity_type="",
            domain="general",
            kind="fact",
            predicate="entity_name",
            attributes={"entity": "三花猫咪", "name": "小花"},
            summary="三花猫咪名字叫小花",
            source_excerpt="我有一只三花猫咪，名字叫小花",
        ),
    ]


def test_gateway_executes_model_predicates_with_one_shared_entity_chain() -> None:
    entity_id = uuid4()
    resolver = Mock()
    resolver.resolve = AsyncMock(
        return_value=SimpleNamespace(
            status="created",
            entity_id=entity_id,
            entity_type="pet",
            question="",
        )
    )

    with (
        patch(
            "app.services.memory.write_gateway.EntityResolver",
            return_value=resolver,
        ),
        patch("app.services.memory.write_gateway.MemoryVersionStore") as store_type,
    ):
        store_type.return_value.commit = AsyncMock(return_value=_Receipt())
        store_type.return_value.list_current = AsyncMock(return_value=[])
        gateway = MemoryWriteGateway(Mock())
        asyncio.run(
            gateway.commit_compiled_facts(
                _named_cat_facts(),
                user_id=uuid4(),
                thread_id=uuid4(),
                source_event_id=uuid4(),
                receipt_id="named-cat",
                raw_text="我有一只三花猫咪，名字叫小花",
            )
        )

    command = store_type.return_value.commit.await_args.args[0]
    facts = {fact.schema_key: fact for fact in command.facts}
    assert set(facts) == {"possession.entity", "entity.name"}
    assert facts["possession.entity"].value["entity_id"] == str(entity_id)
    assert facts["entity.name"].value["entity_id"] == str(entity_id)
    assert facts["entity.name"].value["name"] == "小花"
    resolver.resolve.assert_awaited_once()


def test_rename_uses_the_same_canonical_entity_name_key() -> None:
    entity_id = uuid4()
    resolver = Mock()
    resolver.resolve = AsyncMock(
        return_value=SimpleNamespace(
            status="existing",
            entity_id=entity_id,
            entity_type="pet",
            question="",
        )
    )

    with (
        patch(
            "app.services.memory.write_gateway.EntityResolver",
            return_value=resolver,
        ),
        patch("app.services.memory.write_gateway.MemoryVersionStore") as store_type,
    ):
        store_type.return_value.commit = AsyncMock(return_value=_Receipt())
        store_type.return_value.list_current = AsyncMock(return_value=[])
        gateway = MemoryWriteGateway(Mock())
        asyncio.run(
            gateway.commit_compiled_facts(
                _named_cat_facts(),
                user_id=uuid4(),
                thread_id=None,
                source_event_id=uuid4(),
                receipt_id="cat-first-name",
                raw_text="我有一只三花猫咪，名字叫小花",
            )
        )
        asyncio.run(
            gateway.commit_compiled_facts(
                [
                    AtomicFact(
                        entity="三花猫咪",
                        subject="self",
                        entity_type="pet",
                        domain="general",
                        kind="correction",
                        predicate="entity_name",
                        attributes={"entity": "三花猫咪", "name": "咪咪"},
                        summary="现在改名为咪咪",
                        source_excerpt="现在改名为咪咪",
                    )
                ],
                user_id=uuid4(),
                thread_id=None,
                source_event_id=uuid4(),
                receipt_id="cat-renamed",
                raw_text="现在改名为咪咪",
            )
        )

    first_command = store_type.return_value.commit.await_args_list[0].args[0]
    rename_command = store_type.return_value.commit.await_args_list[1].args[0]
    first_name = next(
        fact for fact in first_command.facts if fact.schema_key == "entity.name"
    )
    renamed = rename_command.facts[0]
    assert renamed.schema_key == "entity.name"
    assert renamed.memory_key == first_name.memory_key
    assert renamed.value["name"] == "咪咪"


def test_renderer_combines_named_possession_and_renders_rename() -> None:
    created = {
        "mutations": [
            {
                "status": "created",
                "version": {
                    "schema_key": "possession.entity",
                    "predicate": "has",
                    "subject": "self",
                    "value": {"entity": "三花猫咪"},
                },
            },
            {
                "status": "created",
                "version": {
                    "schema_key": "entity.name",
                    "predicate": "entity_name",
                    "subject": "self",
                    "value": {"entity": "三花猫咪", "name": "小花"},
                },
            },
        ]
    }
    renamed = {
        "mutations": [
            {
                "status": "revised",
                "version": {
                    "schema_key": "entity.name",
                    "predicate": "entity_name",
                    "subject": "self",
                    "value": {"entity": "三花猫咪", "name": "咪咪"},
                },
                "previous": {
                    "schema_key": "general.fact",
                    "predicate": "entity_name",
                    "subject": "self",
                    "value": {"entity": "三花猫咪", "name": "小花"},
                },
            }
        ]
    }

    assert render_memory_mutation(created) == "记住了：你有三花猫咪，名字叫小花。"
    assert (
        render_memory_mutation(renamed)
        == "已把三花猫咪的名字从“小花”更新为“咪咪”。"
    )


def test_rename_continues_newest_legacy_chain_without_deleting_history() -> None:
    entity_id = uuid4()
    user_id = uuid4()
    resolver = Mock()
    resolver.resolve = AsyncMock(
        return_value=SimpleNamespace(
            status="existing",
            entity_id=entity_id,
            entity_type="pet",
            question="",
        )
    )
    current = MemoryVersionRecord(
        id=uuid4(),
        user_id=user_id,
        chain_id=uuid4(),
        schema_key="general.correction",
        memory_key=f"entity:{entity_id}:entity_name",
        version_no=1,
        operation="create",
        source_event_id=uuid4(),
        receipt_id="legacy-name",
        canonical_hash="a" * 64,
        schema_version=1,
        subject="self",
        predicate="entity_name",
        value={"entity": "三花猫咪", "name": "咪咪"},
        evidence_quote="现在改名为咪咪",
        valid_from=datetime.now(timezone.utc),
    )

    with (
        patch(
            "app.services.memory.write_gateway.EntityResolver",
            return_value=resolver,
        ),
        patch("app.services.memory.write_gateway.MemoryVersionStore") as store_type,
    ):
        store_type.return_value.list_current = AsyncMock(return_value=[current])
        store_type.return_value.commit = AsyncMock(return_value=_Receipt())
        asyncio.run(
            MemoryWriteGateway(Mock()).commit_compiled_facts(
                [
                    AtomicFact(
                        entity="三花猫咪",
                        subject="self",
                        entity_type="pet",
                        domain="general",
                        kind="correction",
                        predicate="entity_name",
                        attributes={"entity": "三花猫咪", "name": "花花"},
                        summary="现在改名为花花",
                        source_excerpt="现在改名为花花",
                    )
                ],
                user_id=user_id,
                thread_id=None,
                source_event_id=uuid4(),
                receipt_id="cat-renamed-again",
                raw_text="现在改名为花花",
            )
        )

    command = store_type.return_value.commit.await_args.args[0]
    assert command.facts[0].memory_key == current.memory_key
    assert command.facts[0].value["name"] == "花花"
    assert not hasattr(store_type.return_value, "forget") or not store_type.return_value.forget.called
