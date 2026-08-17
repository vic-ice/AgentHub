from __future__ import annotations

from typing import Literal
from uuid import UUID

from app.schemas.chat import UserInput
from app.services.conversation.contracts import ConversationWindow
from app.services.memory.committer import MemoryCommitter
from app.services.memory.fact_extractor import MemoryFactExtractor
from app.services.memory.persistence_policy import MemoryPersistencePolicy
from app.services.memory.reference_resolver import MemoryReferenceResolver
from app.services.memory.entity_resolver import EntityResolver
from app.services.memory.turn_compiler import TurnFactCompiler

from app.services.memory.write_contracts import (
    MemoryClarificationContext,
    MemoryCommitCommand,
    MemoryWriteOutcome,
    ResolvedMemoryFact,
    MemoryWriteRequest,
)


class MemoryWriteCoordinator:
    """DEPRECATED compatibility layer. Production chat uses version_runtime + MemoryWriteGateway; kept only as legacy routing adapter with NO independent write authority."""

    def __init__(
        self,
        *,
        reference_resolver: MemoryReferenceResolver | None = None,
        fact_extractor: MemoryFactExtractor | None = None,
        persistence_policy: MemoryPersistencePolicy | None = None,
        committer: MemoryCommitter | None = None,
    ) -> None:
        self._references = reference_resolver or MemoryReferenceResolver()
        self._facts = fact_extractor or MemoryFactExtractor()
        self._policy = persistence_policy or MemoryPersistencePolicy()
        self._committer = committer or MemoryCommitter()

    async def process(
        self,
        request: MemoryWriteRequest,
        *,
        user_input: UserInput,
        conversation: ConversationWindow,
        user_id: UUID,
        thread_id: UUID | None,
        model_id: str = "",
    ) -> MemoryWriteOutcome:
        resolution = self._references.resolve(request, conversation)
        if resolution.status == "clarification_required":
            return MemoryWriteOutcome(
                status="clarification_required",
                clarification_question=resolution.clarification_question,
                pending_clarification=_pending_clarification(
                    request,
                    question=resolution.clarification_question,
                    kind="reference",
                ),
                reason_codes=resolution.reason_codes,
            )

        # ── Turn semantic compile + entity resolution (entity-centric) ──
        compiled = await TurnFactCompiler().compile(
            request.utterance,
            conversation_turns=[
                str(turn.content or '') for turn in conversation.turns[-6:]
            ],
        )
        if compiled.facts:
            outcome = await self._commit_compiled_facts(
                compiled,
                user_id=user_id,
                thread_id=thread_id,
            )
            if outcome is not None:
                return outcome


        drafts = self._facts.deterministic(
            user_input=user_input,
            resolution=resolution,
        )
        if (
            not drafts
            and request.explicit
            and request.semantic_fallback_allowed
        ):
            try:
                drafts = [
                    await self._facts.semantic(
                        resolution=resolution,
                        model_id=model_id,
                    )
                ]
            except Exception as exc:
                return MemoryWriteOutcome(
                    status="clarification_required",
                    clarification_question=(
                        "我暂时无法可靠地解析这条信息，请直接用完整事实表述一次。"
                    ),
                    pending_clarification=_pending_clarification(
                        request,
                        question=(
                            "我暂时无法可靠地解析这条信息，"
                            "请直接用完整事实表述一次。"
                        ),
                        kind="completeness",
                    ),
                    reason_codes=[
                        "semantic_interpretation_unavailable",
                        type(exc).__name__,
                    ],
                )

        decision = self._policy.decide(request, drafts)
        if decision.status != "commit_ready":
            status = (
                "clarification_required"
                if decision.status == "clarification_required"
                else "rejected"
            )
            return MemoryWriteOutcome(
                status=status,
                clarification_question=decision.clarification_question,
                pending_clarification=(
                    _pending_clarification(
                        request,
                        question=decision.clarification_question,
                        kind="completeness",
                    )
                    if status == "clarification_required"
                    else None
                ),
                reason_codes=decision.reason_codes,
            )

        bound = await self._bind_entities(decision.facts, user_id=user_id)
        if isinstance(bound, MemoryWriteOutcome):
            return bound
        return await self._committer.commit(
            MemoryCommitCommand(facts=bound),
            user_id=user_id,
            thread_id=thread_id,
        )


    async def _commit_compiled_facts(
        self,
        compiled: Any,
        *,
        user_id: UUID,
        thread_id: UUID | None,
    ) -> MemoryWriteOutcome | None:
        """Route compiled facts: reading -> ReadingService; others -> committer."""
        if compiled.has_ambiguity:
            ambiguous = next(
                (fact for fact in compiled.facts if fact.needs_clarification),
                None,
            )
            return MemoryWriteOutcome(
                status="clarification_required",
                clarification_question=(
                    ambiguous.clarification_question
                    if ambiguous and ambiguous.clarification_question
                    else "请明确你指的是哪个对象或状态，我再记录。"
                ),
                reason_codes=["turn_compiler_ambiguous"],
            )

        reading = compiled.reading_facts
        if reading:
            return await self._commit_reading_facts(
                reading,
                user_id=user_id,
                thread_id=thread_id,
                raw_text=compiled.raw_text,
            )
        others = [fact for fact in compiled.facts if fact.domain != "reading"]
        if not others:
            return None
        bound = await self._bind_compiled_facts(others, user_id=user_id, raw_text=compiled.raw_text)
        if isinstance(bound, MemoryWriteOutcome):
            return bound
        return await self._committer.commit(
            MemoryCommitCommand(facts=bound),
            user_id=user_id,
            thread_id=thread_id,
        )

    async def _commit_reading_facts(
        self,
        facts: list[Any],
        *,
        user_id: UUID,
        thread_id: UUID | None,
        raw_text: str,
    ) -> MemoryWriteOutcome:
        from app.infra.database import get_database
        from app.services.books.reading_service import (
            ReadingService,
            write_reading_memory_best_effort,
        )

        db = get_database()
        memories: list[dict[str, Any]] = []
        memory_payloads: list[dict[str, Any]] = []
        async with db.session() as session:
            svc = ReadingService(session)
            resolver = EntityResolver(session)
            for fact in facts:
                resolved = await resolver.resolve(
                    user_id=user_id,
                    entity_type="book",
                    name=fact.entity,
                )
                if resolved.status == "ambiguous":
                    return MemoryWriteOutcome(
                        status="clarification_required",
                        clarification_question=resolved.question,
                        reason_codes=["entity_resolution_ambiguous"],
                    )
                attributes = dict(fact.attributes or {})
                kwargs: dict[str, Any] = {
                    "user_id": user_id,
                    "title": fact.entity,
                    "reading_status": attributes.get("reading_status") or "read",
                    "source": "book_feedback",
                }
                if "evaluation" in attributes:
                    kwargs["evaluation"] = attributes.get("evaluation")
                result = await svc.upsert(**kwargs)
                memories.append(result.shelf.model_dump(mode="json"))
                memory_payloads.append(
                    {
                        "book_title": result.shelf.title,
                        "reading_status": result.shelf.reading_status,
                        "evaluation": result.shelf.evaluation,
                        "entity_id": resolved.entity_id,
                    }
                )
        for payload in memory_payloads:
            await write_reading_memory_best_effort(
                user_id=user_id,
                book_title=payload["book_title"],
                reading_status=payload["reading_status"],
                evaluation=payload["evaluation"],
                thread_id=thread_id,
                source_kind="book_feedback",
                entity_id=payload["entity_id"],
            )
        return MemoryWriteOutcome(
            status="committed",
            memories=memories,
            reason_codes=["turn_compiler_reading_committed"],
        )


    async def _bind_entities(
        self,
        facts: list[ResolvedMemoryFact],
        *,
        user_id: UUID,
    ) -> list[ResolvedMemoryFact] | MemoryWriteOutcome:
        from app.infra.database import get_database

        db = get_database()
        bound: list[ResolvedMemoryFact] = []
        async with db.session() as session:
            resolver = EntityResolver(session)
            for fact in facts:
                sv = dict(fact.state_value or {})
                entity_name = str(sv.get("entity") or "").strip()
                entity_type = str(sv.get("entity_type") or "").strip()
                if entity_name and entity_type:
                    resolved = await resolver.resolve(
                        user_id=user_id,
                        entity_type=entity_type,
                        name=entity_name,
                    )
                    if resolved.status == "ambiguous":
                        return MemoryWriteOutcome(
                            status="clarification_required",
                            clarification_question=resolved.question,
                            reason_codes=["entity_resolution_ambiguous"],
                        )
                    sv["entity_id"] = str(resolved.entity_id)
                    bound.append(
                        fact.model_copy(
                            update={
                                "state_value": sv,
                                "entity_id": resolved.entity_id,
                            }
                        )
                    )
                else:
                    bound.append(fact)
        return bound

    async def _bind_compiled_facts(
        self,
        facts: list[Any],
        *,
        user_id: UUID,
        raw_text: str,
    ) -> list[ResolvedMemoryFact] | MemoryWriteOutcome:
        from app.infra.database import get_database
        from app.services.memory.write_contracts import MemorySourceEvidence

        db = get_database()
        bound: list[ResolvedMemoryFact] = []
        async with db.session() as session:
            resolver = EntityResolver(session)
            for fact in facts:
                sv = dict(fact.attributes or {})
                entity_name = str(fact.entity or "").strip()
                entity_type = str(fact.entity_type or "").strip()
                if entity_name and entity_type:
                    resolved = await resolver.resolve(
                        user_id=user_id,
                        entity_type=entity_type,
                        name=entity_name,
                    )
                    if resolved.status == "ambiguous":
                        return MemoryWriteOutcome(
                            status="clarification_required",
                            clarification_question=resolved.question,
                            reason_codes=["entity_resolution_ambiguous"],
                        )
                    sv["predicate"] = _compiled_predicate(fact)
                    sv["entity"] = entity_name
                    sv["entity_type"] = entity_type
                    sv["entity_id"] = str(resolved.entity_id)
                    entity_id = resolved.entity_id
                else:
                    entity_id = None
                bound.append(
                    _fact_from_compiled(
                        fact,
                        state_value=sv,
                        entity_id=entity_id,
                        raw_text=raw_text,
                    )
                )
        return bound


def _fact_from_compiled(
    fact: Any,
    *,
    state_value: dict[str, Any],
    entity_id: UUID | None,
    raw_text: str,
) -> ResolvedMemoryFact:
    from app.services.memory.write_contracts import MemorySourceEvidence

    domain = fact.domain if fact.domain in _VALID_DOMAINS else "general"
    kind = fact.kind if fact.kind in _VALID_KINDS else "fact"
    polarity = fact.polarity if fact.polarity in _VALID_POLARITIES else "neutral"
    legacy_type = {
        "preference": "preference",
        "state": "state",
        "feedback": "feedback",
        "fact": "entity",
        "agreement": "state",
        "correction": "correction",
    }.get(fact.kind or "", "state")
    legacy_subject = fact.entity_type or "user"
    return ResolvedMemoryFact(
        category=_category_from_domain_kind(fact.domain, fact.kind),
        state_key=f"{_safe_domain(fact.domain)}.{_safe_kind(fact.kind)}",
        summary=fact.summary or state_value.get("summary") or fact.entity or "",
        state_value=state_value,
        legacy_type=legacy_type,
        legacy_subject=legacy_subject,
        legacy_value=fact.summary or fact.entity or "",
        domain=domain,
        kind=kind,
        entity_id=entity_id,
        polarity=polarity,
        source=MemorySourceEvidence(
            source_kind="current_user_assertion",
            turn_offset=0,
            excerpt=raw_text,
        ),
        extraction_method="semantic",
        extraction_confidence=0.9,
    )


def _pending_clarification(
    request: MemoryWriteRequest,
    *,
    question: str,
    kind: Literal["reference", "completeness", "conflict"],
) -> MemoryClarificationContext:
    previous = request.clarification
    return MemoryClarificationContext(
        kind=kind,
        original_utterance=(
            previous.original_utterance
            if previous is not None
            else request.utterance
        ),
        target_expression=(
            request.target_expression
            or (
                previous.target_expression
                if previous is not None
                else ""
            )
        ),
        question=question,
    )

def _safe_domain(value: Any) -> str:
    return str(value or "").strip() or "general"


def _safe_kind(value: Any) -> str:
    return str(value or "").strip() or "fact"

def _category_from_domain_kind(domain: Any, kind: Any) -> str:
    """Map domain/kind onto the legacy user-state category enum."""
    k = str(kind or "").strip() or "fact"
    d = str(domain or "").strip() or "general"
    if k == "preference":
        return "preference"
    if k == "feedback":
        return "feedback"
    if k == "agreement" or k == "correction":
        return "profile"
    if d in {"relationship", "possession"}:
        return "relation"
    if d == "plan" or k == "state":
        return "short_term"
    return "short_term"

def _compiled_predicate(fact: Any) -> str:
    attributes = fact.attributes or {}
    for key in ("name", "predicate", "color", "age", "relation", "platform", "species"):
        if key in attributes:
            return str(key).strip()[:120]
    keys = [str(k) for k in attributes.keys()]
    return (keys[0] if keys else "fact")[:120]
_VALID_DOMAINS = frozenset({"reading", "personal", "possession", "relationship", "plan", "general"})
_VALID_KINDS = frozenset({"preference", "state", "feedback", "fact", "agreement", "correction"})
_VALID_POLARITIES = frozenset({"like", "dislike", "neutral", "want", "avoid", "read"})