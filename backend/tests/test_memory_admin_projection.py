from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.api.v1.memory import _to_admin_fact
from app.services.memory.admin_projection import (
    collapse_current_versions,
    history_versions_for,
)
from app.services.memory.version_contracts import (
    CanonicalMemoryFact,
    MemoryVersionRecord,
)
from app.services.memory.write_gateway import MemoryWriteGateway


def _record(
    *,
    schema_key: str,
    memory_key: str,
    predicate: str,
    value: dict,
    valid_from: datetime | None = None,
    subject: str = "user",
    evidence_quote: str = "原始消息",
    is_tombstone: bool = False,
) -> MemoryVersionRecord:
    return MemoryVersionRecord(
        id=uuid4(),
        user_id=uuid4(),
        chain_id=uuid4(),
        schema_key=schema_key,
        memory_key=memory_key,
        version_no=1,
        operation="forget" if is_tombstone else "create",
        source_event_id=uuid4(),
        receipt_id=f"receipt-{memory_key}",
        canonical_hash="a" * 64,
        schema_version=1,
        subject=subject,
        predicate=predicate,
        value=value,
        evidence_quote=evidence_quote,
        is_tombstone=is_tombstone,
        valid_from=valid_from or datetime.now(timezone.utc),
    )


def test_legacy_preference_gets_user_facing_projection() -> None:
    fact = _to_admin_fact(
        _record(
            schema_key="general.preference",
            memory_key="entity:uuid:likes",
            predicate="likes",
            value={
                "domain": "general",
                "kind": "preference",
                "entity": "水",
                "polarity": "like",
                "summary": "我喜欢喝水",
            },
        )
    )

    assert fact.category_key == "preference"
    assert fact.category_label == "偏好"
    assert fact.presentation_key == "preference.entity"
    assert fact.display_value == "我喜欢喝水"
    assert fact.can_edit
    assert fact.can_forget


def test_reading_records_are_grouped_and_read_only() -> None:
    fact = _to_admin_fact(
        _record(
            schema_key="reading.state",
            memory_key="entity:uuid:reading_status",
            predicate="reading_status",
            subject="book",
            value={
                "domain": "reading",
                "kind": "state",
                "book_title": "失控",
                "reading_status": "want_to_read",
            },
        )
    )

    assert fact.category_key == "reading"
    assert fact.category_label == "阅读记忆"
    assert fact.display_value == "《失控》 · 想读"
    assert not fact.can_edit
    assert not fact.can_forget
    assert not fact.show_evidence


def test_general_skill_fact_uses_structured_summary_not_full_evidence() -> None:
    fact = _to_admin_fact(
        _record(
            schema_key="general.fact",
            memory_key="general.fact:state",
            predicate="state",
            value={
                "domain": "general",
                "kind": "fact",
                "state": "skill",
                "value": "Python",
                "summary": "我会 Python",
            },
        )
    )

    assert fact.category_key == "background"
    assert fact.category_label == "能力与背景"
    assert fact.display_value == "技能：Python"


def test_compatibility_name_heads_collapse_to_latest_value() -> None:
    now = datetime.now(timezone.utc)
    old = _record(
        schema_key="personal.fact",
        memory_key="personal.fact:name",
        predicate="name",
        value={"name": "冰露", "predicate": "name"},
        valid_from=now - timedelta(days=1),
    )
    corrected = _record(
        schema_key="personal.correction",
        memory_key="personal.correction:name",
        predicate="name",
        value={"name": "可乐", "predicate": "name"},
        valid_from=now,
    )

    collapsed = collapse_current_versions([old, corrected])

    assert [record.value["name"] for record in collapsed] == ["可乐"]
    assert _to_admin_fact(collapsed[0]).display_value == "当前名字：可乐"


def test_entity_name_heads_collapse_to_latest_without_losing_history() -> None:
    now = datetime.now(timezone.utc)
    old = _record(
        schema_key="general.fact",
        memory_key="general.fact:entity_name",
        predicate="entity_name",
        value={"entity": "三花猫咪", "name": "小花"},
        valid_from=now - timedelta(days=1),
    )
    renamed = _record(
        schema_key="general.correction",
        memory_key="entity:cat:entity_name",
        predicate="entity_name",
        value={"entity": "三花猫咪", "name": "咪咪"},
        valid_from=now,
    )

    collapsed = collapse_current_versions([old, renamed])
    history = history_versions_for(
        [renamed, old],
        memory_key=renamed.memory_key,
    )

    assert [record.value["name"] for record in collapsed] == ["咪咪"]
    assert [record.value["name"] for record in history] == ["小花", "咪咪"]
    assert _to_admin_fact(collapsed[0]).display_value == "三花猫咪叫咪咪"


def test_structured_possession_drops_stale_incidental_name_from_summary() -> None:
    possession = _record(
        schema_key="personal.state",
        memory_key="entity:cat:has",
        predicate="has",
        value={
            "entity": "三花猫咪",
            "summary": "我有一只三花猫咪，名字叫小花",
            "domain": "personal",
            "kind": "state",
            "predicate": "has",
        },
    )

    projected = collapse_current_versions([possession])[0]
    fact = _to_admin_fact(projected)

    assert projected.schema_key == "possession.entity"
    assert projected.predicate == "possession.entity"
    assert "summary" not in projected.value
    assert fact.category_key == "possession"
    assert fact.display_value == "拥有：三花猫咪"


def test_compatibility_name_history_includes_both_storage_chains() -> None:
    now = datetime.now(timezone.utc)
    old = _record(
        schema_key="personal.fact",
        memory_key="personal.fact:name",
        predicate="name",
        value={"name": "冰露", "predicate": "name"},
        valid_from=now - timedelta(days=1),
    )
    corrected = _record(
        schema_key="personal.correction",
        memory_key="personal.correction:name",
        predicate="name",
        value={"name": "可乐", "predicate": "name"},
        valid_from=now,
    )

    history = history_versions_for(
        [corrected, old],
        memory_key="personal.correction:name",
    )

    assert [record.value["name"] for record in history] == ["冰露", "可乐"]
    assert history[0].valid_to == corrected.valid_from
    assert history[1].valid_to is None


def test_edited_compatibility_name_still_collapses_as_one_semantic_fact() -> None:
    now = datetime.now(timezone.utc)
    old = _record(
        schema_key="personal.fact",
        memory_key="personal.fact:name",
        predicate="name",
        value={"name": "冰露"},
        valid_from=now - timedelta(days=1),
    )
    edited = _record(
        schema_key="identity.self_reported_name",
        memory_key="personal.correction:name",
        predicate="identity.self_reported_name",
        value={"name": "可乐"},
        valid_from=now,
        subject="self",
    )

    collapsed = collapse_current_versions([old, edited])
    history = history_versions_for(
        [edited, old],
        memory_key="personal.correction:name",
    )

    assert [record.value["name"] for record in collapsed] == ["可乐"]
    assert [record.value["name"] for record in history] == ["冰露", "可乐"]


def test_admin_generated_evidence_is_hidden_and_tombstone_is_readable() -> None:
    fact = _to_admin_fact(
        _record(
            schema_key="preference.entity",
            memory_key="preference.entity:water",
            predicate="prefers",
            value={"entity": "水", "polarity": "like"},
            evidence_quote="用户在记忆中心更新了这条事实。",
        )
    )
    tombstone = _to_admin_fact(
        _record(
            schema_key="preference.entity",
            memory_key="preference.entity:water",
            predicate="prefers",
            value={},
            evidence_quote="用户在记忆中心遗忘了这条事实。",
            is_tombstone=True,
        )
    )

    assert not fact.show_evidence
    assert tombstone.display_value == "遗忘此条记忆"
    assert not tombstone.can_edit
    assert not tombstone.can_forget


def test_unknown_current_fact_can_be_forgotten_without_being_editable() -> None:
    fact = _to_admin_fact(
        _record(
            schema_key="legacy.unknown",
            memory_key="legacy.unknown:one",
            predicate="unknown",
            value={"summary": "旧系统保存的事实"},
        )
    )

    assert fact.display_value == "旧系统保存的事实"
    assert not fact.can_edit
    assert fact.can_forget


def test_admin_correction_preserves_the_selected_memory_key() -> None:
    fact = CanonicalMemoryFact(
        schema_key="preference.entity",
        memory_key="preference.entity:new-key",
        schema_version=1,
        subject="self",
        predicate="preference.entity",
        value={"entity": "茶", "polarity": "like"},
        evidence_quote="用户在记忆中心更新了这条事实。",
        canonical_hash="a" * 64,
    )
    receipt = Mock()
    receipt.model_dump.return_value = {"mutations": []}

    with patch(
        "app.services.memory.write_gateway.MemoryVersionStore"
    ) as store_type:
        store_type.return_value.commit = AsyncMock(return_value=receipt)
        result = asyncio.run(
            MemoryWriteGateway(Mock()).commit_admin_correction(
                fact,
                target_memory_key="general.preference:likes",
                user_id=uuid4(),
                thread_id=None,
                source_event_id=uuid4(),
                receipt_id="admin-exact-target",
            )
        )

    command = store_type.return_value.commit.await_args.args[0]
    assert command.facts[0].memory_key == "general.preference:likes"
    assert result["status"] == "committed"
