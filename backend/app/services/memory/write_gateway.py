"""MemoryWriteGateway — the ONLY path into durable memory.

Semantic decisions happen BEFORE this gateway:
  TurnFactCompiler (understand) -> EntityResolver / ClarificationGate (bind/confirm)
  -> this gateway routes by fact type -> ReadingService or VersionStore.

VersionStore keeps version-chain + data integrity only; it never interprets
semantics. No path (Chat, admin, correction, ReadingService-derived memory) may
write memory_events directly.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.memory.classification import derive_domain_kind
from app.services.memory.entity_resolver import EntityResolver
from app.services.memory.version_contracts import (
    CanonicalMemoryFact,
    MemoryVersionCommitCommand,
)
from app.services.memory.version_store import MemoryVersionStore


class MemoryWriteGateway:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def commit_compiled(
        self,
        *,
        user_id: UUID,
        thread_id: UUID | None,
        compiled: Any,
        source_event_id: UUID,
        receipt_id: str,
        source_kind: str = "user_message",
    ) -> dict[str, Any]:
        """Execute every independent fact from one semantic batch exactly once."""
        executable = [fact for fact in compiled.facts if not fact.needs_clarification]
        ambiguous = [fact for fact in compiled.facts if fact.needs_clarification]
        reading = [fact for fact in executable if fact.domain == "reading"]
        others = [fact for fact in executable if fact.domain != "reading"]
        mutations: list[dict[str, Any]] = []
        shelf_entries: list[dict[str, Any]] = []
        questions: list[str] = []

        if reading:
            reading_outcome = await self._commit_reading(
                reading,
                user_id=user_id,
                thread_id=thread_id,
                raw_text=compiled.raw_text,
                source_event_id=source_event_id,
                receipt_id=receipt_id,
                source_kind=source_kind,
            )
            mutations.extend(reading_outcome.get("mutations", []))
            shelf_entries.extend(reading_outcome.get("shelf_entries", []))
            questions.extend(reading_outcome.get("clarification_questions", []))

        if others:
            memory_outcome = await self.commit_compiled_facts(
                others,
                user_id=user_id,
                thread_id=thread_id,
                source_event_id=source_event_id,
                receipt_id=receipt_id + "-memory",
                source_kind=source_kind,
                raw_text=compiled.raw_text,
            )
            mutations.extend(memory_outcome.get("mutations", []))

        questions.extend(
            fact.clarification_question or "请明确你指的是哪个对象或状态。"
            for fact in ambiguous
        )
        if not executable and questions:
            return {
                "status": "clarification_required",
                "clarification_question": questions[0],
                "clarification_questions": questions,
            }
        if not executable:
            return {"status": "noop", "reason": "no_facts"}
        return {
            "status": "partial" if questions else "committed",
            "mutations": mutations,
            "shelf_entries": shelf_entries,
            "clarification_question": questions[0] if questions else "",
            "clarification_questions": questions,
        }



    async def commit_compiled_facts(
        self,
        facts: list[Any],
        *,
        user_id: UUID,
        thread_id: UUID | None,
        source_event_id: UUID,
        receipt_id: str,
        source_kind: str = "user_message",
        raw_text: str,
    ) -> dict[str, Any]:
        canonical: list[CanonicalMemoryFact] = []
        resolver = EntityResolver(self.session)
        for fact in facts:
            entity = str(fact.entity or "").strip()
            entity_type = str(fact.entity_type or "").strip()
            domain = fact.domain if fact.domain in _DOMAINS else "general"
            kind = fact.kind if fact.kind in _KINDS else "fact"
            entity_id: UUID | None = None
            if entity and entity_type:
                resolved = await resolver.resolve(
                    user_id=user_id, entity_type=entity_type, name=entity
                )
                if resolved.status == "ambiguous":
                    return {
                        "status": "clarification_required",
                        "clarification_question": resolved.question,
                    }
                entity_id = resolved.entity_id
            predicate = _predicate(fact)
            value = {
                **dict(fact.attributes or {}),
                "domain": domain,
                "kind": kind,
                "predicate": predicate,
                "summary": fact.summary or entity or raw_text,
            }
            if entity:
                value["entity"] = entity
            if entity_type:
                value["entity_type"] = entity_type
            if entity_id is not None:
                value["entity_id"] = str(entity_id)
            memory_key = (
                f"entity:{entity_id}:{predicate}"
                if entity_id is not None
                else f"{domain}.{kind}:{predicate}"
            )
            canonical.append(
                CanonicalMemoryFact(
                    schema_key=f"{domain}.{kind}",
                    memory_key=memory_key,
                    schema_version=1,
                    subject=entity_type or "user",
                    predicate=predicate,
                    value=value,
                    qualifiers={},
                    evidence_quote=raw_text,
                    canonical_hash=_hash(value),
                )
            )
        receipt = await MemoryVersionStore(self.session).commit(
            MemoryVersionCommitCommand(
                facts=canonical,
                source_event_id=source_event_id,
                source_kind=source_kind,
                receipt_id=receipt_id,
            ),
            user_id=user_id,
            thread_id=thread_id,
        )
        receipt_payload = receipt.model_dump(mode="json")
        return {
            "status": "committed",
            "mutations": receipt_payload.get("mutations", []),
            "receipt": receipt_payload,
        }

    async def record_reading_memory(
        self,
        *,
        user_id: UUID,
        thread_id: UUID | None,
        book_title: str,
        reading_status: str | None,
        evaluation: str | None,
        entity_id: UUID,
        source_event_id: UUID,
        receipt_id: str,
        evidence: str,
    ) -> dict[str, Any]:
        """Project a committed Reading action through the same Memory gateway."""
        facts: list[CanonicalMemoryFact] = []
        if reading_status:
            value = {
                "book_title": book_title,
                "reading_status": reading_status,
                "domain": "reading",
                "kind": "state",
                "predicate": "reading_status",
                "entity_id": str(entity_id),
            }
            facts.append(
                CanonicalMemoryFact(
                    schema_key="reading.state",
                    memory_key=f"entity:{entity_id}:reading_status",
                    schema_version=1,
                    subject="book",
                    predicate="reading_status",
                    value=value,
                    qualifiers={},
                    evidence_quote=evidence,
                    canonical_hash=_hash(value),
                )
            )
        if evaluation:
            value = {
                "book_title": book_title,
                "evaluation": evaluation,
                "domain": "reading",
                "kind": "feedback",
                "predicate": "evaluation",
                "entity_id": str(entity_id),
            }
            facts.append(
                CanonicalMemoryFact(
                    schema_key="reading.feedback",
                    memory_key=f"entity:{entity_id}:evaluation",
                    schema_version=1,
                    subject="book",
                    predicate="evaluation",
                    value=value,
                    qualifiers={},
                    evidence_quote=evidence,
                    canonical_hash=_hash(value),
                )
            )
        if not facts:
            return {"status": "noop", "mutations": []}
        receipt = await MemoryVersionStore(self.session).commit(
            MemoryVersionCommitCommand(
                facts=facts,
                source_event_id=source_event_id,
                source_kind="reading_event",
                receipt_id=receipt_id,
            ),
            user_id=user_id,
            thread_id=thread_id,
        )
        receipt_payload = receipt.model_dump(mode="json")
        return {
            "status": "committed",
            "mutations": receipt_payload.get("mutations", []),
            "receipt": receipt_payload,
        }

    async def commit_facts(
        self,
        facts: list[CanonicalMemoryFact],
        *,
        user_id: UUID,
        thread_id: UUID | None,
        source_event_id: UUID,
        receipt_id: str,
        evidence_quote: str = "",
    ) -> dict[str, Any]:
        """Admin / correction adapter: canonical facts are enriched (domain/kind/
        entity binding) and committed ONLY through VersionStore."""
        enriched: list[CanonicalMemoryFact] = []
        resolver = EntityResolver(self.session)
        for fact in facts:
            domain = str(fact.value.get("domain") or "") or derive_domain_kind("", fact.subject, fact.schema_key)[0]
            kind = str(fact.value.get("kind") or "") or derive_domain_kind("", fact.subject, fact.schema_key)[1]
            value = dict(fact.value)
            entity = str(value.get("entity") or "").strip()
            entity_type = str(value.get("entity_type") or "").strip()
            entity_id: UUID | None = None
            if entity and entity_type:
                resolved = await resolver.resolve(user_id=user_id, entity_type=entity_type, name=entity)
                if resolved.status == "ambiguous":
                    return {"status": "clarification_required", "clarification_question": resolved.question}
                entity_id = resolved.entity_id
                value["entity_id"] = str(entity_id)
            value["domain"] = domain
            value["kind"] = kind
            predicate = str(fact.predicate or "") or "fact"
            memory_key = (
                f"entity:{entity_id}:{predicate}"
                if entity_id is not None
                else f"{domain}.{kind}:{predicate}"
            )
            enriched.append(
                CanonicalMemoryFact(
                    schema_key=fact.schema_key,
                    memory_key=memory_key,
                    schema_version=fact.schema_version,
                    subject=fact.subject,
                    predicate=predicate,
                    value=value,
                    qualifiers=fact.qualifiers,
                    evidence_quote=evidence_quote or fact.evidence_quote,
                    canonical_hash=_hash(value),
                )
            )
        receipt = await MemoryVersionStore(self.session).commit(
            MemoryVersionCommitCommand(
                facts=enriched,
                source_event_id=source_event_id,
                receipt_id=receipt_id,
            ),
            user_id=user_id,
            thread_id=thread_id,
        )
        receipt_payload = receipt.model_dump(mode="json")
        return {
            "status": "committed",
            "mutations": receipt_payload.get("mutations", []),
            "receipt": receipt_payload,
        }

    async def forget(
        self,
        *,
        user_id: UUID,
        thread_id: UUID | None,
        memory_keys: list[str],
        source_event_id: UUID,
        receipt_id: str,
        evidence_quote: str,
    ) -> dict[str, Any]:
        from app.services.memory.version_contracts import MemoryVersionForgetCommand

        receipt = await MemoryVersionStore(self.session).forget(
            MemoryVersionForgetCommand(
                memory_keys=memory_keys,
                source_event_id=source_event_id,
                receipt_id=receipt_id,
                evidence_quote=evidence_quote,
            ),
            user_id=user_id,
            thread_id=thread_id,
        )
        return {"status": "forgotten", "receipt": receipt.model_dump(mode="json")}

    async def _commit_reading(
        self,
        facts: list[Any],
        *,
        user_id: UUID,
        thread_id: UUID | None,
        raw_text: str,
        source_event_id: UUID,
        receipt_id: str,
        source_kind: str,
    ) -> dict[str, Any]:
        from app.services.books.reading_service import ReadingService

        resolver = EntityResolver(self.session)
        svc = ReadingService(self.session)
        resolved_items: list[tuple[Any, Any, str | None, str | None]] = []
        questions: list[str] = []
        for fact in facts:
            if not str(fact.entity or "").strip():
                questions.append("请明确要更新的是哪一本书。")
                continue
            resolved = await resolver.resolve(
                user_id=user_id, entity_type="book", name=fact.entity
            )
            if resolved.status == "ambiguous":
                questions.append(resolved.question)
                continue
            attributes = dict(fact.attributes or {})
            reading_status = _normalize_reading_status(attributes.get("reading_status"))
            evaluation = _normalize_evaluation(attributes.get("evaluation"))
            if reading_status is None and evaluation is None:
                questions.append(f"请明确《{fact.entity}》的阅读状态或评价。")
                continue
            resolved_items.append((fact, resolved, reading_status, evaluation))

        shelf_entries: list[dict[str, Any]] = []
        mutations: list[dict[str, Any]] = []
        for index, (fact, resolved, reading_status, evaluation) in enumerate(resolved_items):
            kwargs: dict[str, Any] = {
                "user_id": user_id,
                "title": fact.entity,
                "reading_status": reading_status,
                "source": source_kind[:32],
                "thread_id": thread_id,
                "request_id": receipt_id,
            }
            if evaluation is not None:
                kwargs["evaluation"] = evaluation
            result = await svc.upsert(**kwargs)
            shelf_entries.append(result.shelf.model_dump(mode="json"))
            event_id = next(
                (
                    getattr(event, "id", None)
                    for event in reversed(result.events)
                    if getattr(event, "id", None) is not None
                ),
                source_event_id,
            )
            mem_outcome = await self.record_reading_memory(
                user_id=user_id,
                book_title=result.shelf.title,
                reading_status=reading_status,
                evaluation=evaluation,
                entity_id=resolved.entity_id,
                source_event_id=event_id,
                thread_id=thread_id,
                receipt_id=f"{receipt_id}-reading-{index}"[:128],
                evidence=fact.source_excerpt or raw_text,
            )
            mutations.extend(mem_outcome.get("mutations", []))

        return {
            "status": "partial" if questions and resolved_items else (
                "clarification_required" if questions else "committed"
            ),
            "mutations": mutations,
            "shelf_entries": shelf_entries,
            "clarification_question": questions[0] if questions else "",
            "clarification_questions": questions,
        }


_DOMAINS = frozenset({"reading", "personal", "possession", "relationship", "plan", "general"})
_KINDS = frozenset({"preference", "state", "feedback", "fact", "agreement", "correction"})


def _predicate(fact: Any) -> str:
    proposed = str(getattr(fact, "predicate", "") or "").strip()
    if proposed:
        return proposed[:120]
    attributes = fact.attributes or {}
    for key in ("name", "color", "age", "relation", "platform", "species"):
        if key in attributes:
            return str(key)[:120]
    keys = [str(k) for k in attributes.keys()]
    return (keys[0] if keys else "fact")[:120]


def _hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = ["MemoryWriteGateway"]


_READING_STATUSES = frozenset(
    {"want_to_read", "reading", "read", "dropped"}
)


def _normalize_reading_status(value):
    raw = str(value or "").strip().lower()
    return raw if raw in _READING_STATUSES else None


_EVALUATIONS = frozenset(
    {"liked", "neutral", "disliked", "not_interested"}
)


def _normalize_evaluation(value):
    raw = str(value or "").strip().lower()
    return raw if raw in _EVALUATIONS else None
