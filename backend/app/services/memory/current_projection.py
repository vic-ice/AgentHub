"""Non-destructive semantic projection for current memory heads.

Older releases could persist one naming fact under more than one storage key.
The version store intentionally preserves those chains.  Runtime consumers,
however, need one current truth per semantic naming slot.  This module owns
that read-side compatibility rule and never mutates stored history.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.services.memory.entity_resolver import canonical_entity_name
from app.services.memory.version_contracts import (
    CanonicalMemoryFact,
    MemoryVersionRecord,
)
from app.services.memory.versioned_schema_registry import (
    VersionedMemorySchema,
    VersionedMemorySchemaRegistry,
)


SemanticNameSlot = tuple[str, str]

_SELF_NAME_PREDICATES = frozenset(
    {"name", "self_reported_name", "identity.self_reported_name"}
)
_ENTITY_NAME_PREDICATES = frozenset(
    {"entity_name", "entity.name", "name_of", "object_name", "named_entity"}
)


def collapse_semantic_current(
    records: Iterable[MemoryVersionRecord],
) -> list[MemoryVersionRecord]:
    """Expose only the newest head for each compatibility naming slot."""

    values = list(records)
    newest_by_slot: dict[SemanticNameSlot, MemoryVersionRecord] = {}
    for record in values:
        slot = semantic_name_slot(record)
        if slot is None:
            continue
        current = newest_by_slot.get(slot)
        if current is None or record.valid_from > current.valid_from:
            newest_by_slot[slot] = record
    selected_ids = {record.id for record in newest_by_slot.values()}
    selected = [
        record
        for record in values
        if semantic_name_slot(record) is None or record.id in selected_ids
    ]
    return [_project_current_record(record) for record in selected]


def semantic_history_for_key(
    records: Iterable[MemoryVersionRecord],
    *,
    memory_key: str,
) -> list[MemoryVersionRecord]:
    """Select all compatibility chains belonging to one naming slot."""

    values = list(records)
    exact = [record for record in values if record.memory_key == memory_key]
    if not exact:
        return []
    slot = semantic_name_slot(max(exact, key=lambda item: item.valid_from))
    if slot is None:
        return exact
    return [record for record in values if semantic_name_slot(record) == slot]


def retarget_name_facts_to_current_chains(
    facts: Iterable[CanonicalMemoryFact],
    current: Iterable[MemoryVersionRecord],
) -> list[CanonicalMemoryFact]:
    """Continue the newest compatible chain without deleting older chains."""

    newest_by_slot: dict[SemanticNameSlot, MemoryVersionRecord] = {}
    for record in current:
        slot = semantic_name_slot(record)
        if slot is None:
            continue
        existing = newest_by_slot.get(slot)
        if existing is None or record.valid_from > existing.valid_from:
            newest_by_slot[slot] = record

    aligned: list[CanonicalMemoryFact] = []
    for fact in facts:
        slot = semantic_name_slot(fact)
        target = newest_by_slot.get(slot) if slot is not None else None
        aligned.append(
            fact.model_copy(update={"memory_key": target.memory_key})
            if target is not None and target.memory_key != fact.memory_key
            else fact
        )
    return aligned


def semantic_name_slot(
    item: MemoryVersionRecord | CanonicalMemoryFact,
) -> SemanticNameSlot | None:
    """Return the stable user-facing slot for self/entity naming facts."""

    value = item.value if isinstance(item.value, dict) else {}
    schema_key = str(item.schema_key or "").strip().casefold()
    predicate = str(
        value.get("predicate") or item.predicate or ""
    ).strip().casefold()
    subject = str(item.subject or "").strip().casefold()
    name = _text(value.get("name"))
    if (
        name
        and subject in {"self", "user"}
        and (
            schema_key == "identity.self_reported_name"
            or (
                schema_key != "entity.name"
                and predicate in _SELF_NAME_PREDICATES
            )
        )
    ):
        return ("self_name", "self")

    entity = canonical_entity_name(value.get("entity"))
    if name and entity and (
        schema_key == "entity.name" or predicate in _ENTITY_NAME_PREDICATES
    ):
        return ("entity_name", entity)
    return None


def _project_current_record(record: MemoryVersionRecord) -> MemoryVersionRecord:
    """Normalize a legacy current head from structured fields only.

    Free-form summaries are provenance-era convenience text.  They can embed
    a stale sibling fact (for example a pet's former name inside a possession
    summary), so a recognized structured fact must not feed that summary back
    to the model or current UI.
    """

    schema = _effective_schema(record)
    if schema is None:
        return record
    value = dict(record.value)
    if any(not _has_value(value.get(field)) for field in schema.required_value_fields):
        return record
    for key in ("summary", "domain", "kind", "predicate"):
        value.pop(key, None)
    return record.model_copy(
        update={
            "schema_key": schema.schema_key,
            "predicate": schema.schema_key,
            "value": value,
        }
    )


def _effective_schema(record: MemoryVersionRecord) -> VersionedMemorySchema | None:
    registry = VersionedMemorySchemaRegistry()
    try:
        return registry.require_schema(record.schema_key)
    except ValueError:
        predicate = str(
            record.value.get("predicate") or record.predicate or ""
        ).strip()
        return registry.resolve_predicate(predicate)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


__all__ = [
    "collapse_semantic_current",
    "retarget_name_facts_to_current_chains",
    "semantic_history_for_key",
    "semantic_name_slot",
]
