"""Semantic contracts for one user turn.

Production Chat semantics come from the Controller's single structured model
round. ``compiled_turn_from_assertions`` converts that already-understood 0..N
assertion batch into the internal contract without another LLM call.

``TurnFactCompiler`` remains an explicit standalone semantic entry for offline
evaluation and non-Controller adapters. It is model-only: rules may validate
its output but never manufacture business meaning from keywords or examples.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.services.memory.classification import MEMORY_DOMAINS, MEMORY_KINDS
from app.services.memory.version_contracts import MemoryAssertionProposal

logger = logging.getLogger(__name__)

_COMPILE_TIMEOUT_SECONDS = 60

class AtomicFact(BaseModel):
    """One atomic fact proposed by the compiler (never persisted as-is)."""

    entity: str = ""
    entity_type: str = ""
    domain: str = ""
    kind: str = ""
    predicate: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    polarity: str = "neutral"
    summary: str = ""
    needs_clarification: bool = False
    clarification_question: str = ""
    source_excerpt: str = ""

    @field_validator(
        "entity",
        "entity_type",
        "domain",
        "kind",
        "predicate",
        "summary",
        "clarification_question",
        "source_excerpt",
        mode="before",
    )
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return str(value or "").strip()


class CompiledTurn(BaseModel):
    """0..N model-understood atomic facts from one turn."""

    raw_text: str = ""
    facts: list[AtomicFact] = Field(default_factory=list)

    @property
    def reading_facts(self) -> list[AtomicFact]:
        return [fact for fact in self.facts if fact.domain == "reading"]

    @property
    def has_ambiguity(self) -> bool:
        return any(fact.needs_clarification for fact in self.facts)


class TurnFactCompiler:
    """Compile a user turn into atomic facts (model proposes, system executes)."""

    async def compile(
        self,
        text: str,
        *,
        conversation_turns: list[str] | None = None,
        model_id: str = "",
    ) -> CompiledTurn:
        raw = " ".join(str(text or "").split()).strip()
        facts: list[AtomicFact] = []

        # One standalone semantic model call. Failure is fail-closed: no rule
        # may manufacture a write from keywords in the source text.
        try:
            llm_facts = await self._llm_facts(raw, conversation_turns or [], model_id)
            for item in llm_facts:
                merged = self._merge_or_append(facts, item, raw)
                facts = merged
        except Exception:
            logger.exception("Turn compiler semantic pass failed closed")

        return CompiledTurn(raw_text=raw, facts=facts)

    def _merge_or_append(
        self,
        facts: list[AtomicFact],
        incoming: AtomicFact,
        source: str,
    ) -> list[AtomicFact]:
        """Merge same-entity reading facts (status + evaluation) instead of duplicating."""
        if not incoming.source_excerpt:
            incoming = incoming.model_copy(update={"source_excerpt": source})
        if incoming.domain == "reading" and incoming.entity_type == "book":
            for index, fact in enumerate(facts):
                if (
                    fact.domain == "reading"
                    and fact.entity_type == "book"
                    and fact.entity.strip().lower() == incoming.entity.strip().lower()
                ):
                    merged = facts[index].model_copy(
                        update={
                            "attributes": {
                                **facts[index].attributes,
                                **incoming.attributes,
                            },
                            "needs_clarification": (
                                facts[index].needs_clarification
                                or incoming.needs_clarification
                            ),
                            "clarification_question": (
                                incoming.clarification_question
                                or facts[index].clarification_question
                            ),
                        }
                    )
                    facts[index] = merged
                    return facts
        facts.append(incoming)
        return facts

    async def _llm_facts(self, text: str, turns: list[str], model_id: str = "") -> list[AtomicFact]:
        prompt = _build_compiler_prompt(text, turns)
        models: list[Any] = []
        try:
            if model_id:
                from app.infra.llm import get_llm

                models.append((model_id, get_llm(model_id)))
        except Exception as exc:
            logger.warning("Turn compiler requested model unavailable: %s", exc)
        if not models:
            try:
                from app.infra.llm import get_system_llm

                models.append(("system", get_system_llm()))
            except Exception as exc:
                logger.warning("Turn compiler system model unavailable: %s", exc)
                return []
        for model_name, model in models:
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(model.invoke, prompt),
                    timeout=_COMPILE_TIMEOUT_SECONDS,
                )
                payload = _extract_facts_json(_message_text(response))
                items = payload if isinstance(payload, list) else payload.get("facts", [])
                if isinstance(items, list):
                    return [
                        AtomicFact.model_validate(item)
                        for item in items
                        if isinstance(item, dict)
                    ]
            except Exception as exc:
                logger.warning(
                    "Turn compiler LLM call failed (model=%s): %s",
                    model_name,
                    str(exc)[:160],
                )
        return []


def compiled_turn_from_assertions(
    assertions: list[MemoryAssertionProposal],
    *,
    raw_text: str,
) -> CompiledTurn:
    """Adapt the Controller's first semantic result without reinterpreting it."""

    facts: list[AtomicFact] = []
    compiler = TurnFactCompiler()
    for assertion in assertions:
        value = dict(assertion.value or {})
        qualifiers = dict(assertion.qualifiers or {})
        predicate = assertion.predicate.strip()
        domain = assertion.domain
        kind = assertion.kind
        entity_type = assertion.entity_type or str(
            qualifiers.get("entity_type") or value.get("entity_type") or ""
        ).strip()
        entity = ""
        attributes: dict[str, Any] = dict(value)

        if predicate in {"reading_status", "evaluation"} or domain == "reading":
            domain = "reading"
            entity_type = "book"
            entity = assertion.subject.strip()
            if predicate == "reading_status":
                attributes = {
                    "reading_status": value.get(
                        "reading_status",
                        value.get("status"),
                    )
                }
                if value.get("evaluation") is not None:
                    attributes["evaluation"] = value.get("evaluation")
                kind = "state"
            elif predicate == "evaluation":
                attributes = {"evaluation": value.get("evaluation")}
                status = value.get(
                    "reading_status",
                    value.get("status"),
                )
                if status is not None:
                    attributes["reading_status"] = status
                kind = "feedback"
            else:
                attributes = {
                    key: value.get(key)
                    for key in ("reading_status", "evaluation")
                    if value.get(key) is not None
                }
        elif assertion.subject.casefold() not in {"self", "user", "用户", "我"}:
            entity = assertion.subject.strip()

        fact = AtomicFact(
            entity=entity,
            entity_type=entity_type,
            domain=domain,
            kind=kind,
            predicate=predicate,
            attributes=attributes,
            polarity=str(value.get("polarity") or "neutral"),
            summary=str(value.get("summary") or assertion.evidence_quote),
            needs_clarification=(
                assertion.needs_clarification
                or assertion.actor != "user"
                or assertion.modality != "asserted"
                or assertion.confidence < 0.7
            ),
            clarification_question=assertion.clarification_question,
            source_excerpt=assertion.evidence_quote,
        )
        facts = compiler._merge_or_append(facts, fact, raw_text)
    return CompiledTurn(raw_text=raw_text, facts=facts)


def _build_compiler_prompt(text: str, turns: list[str] | None = None) -> str:
    context_block = ""
    if turns:
        context_block = "\n".join(f"- {turn}" for turn in turns[-6:])
        context_block = "\nRecent conversation:\n" + context_block + "\n\n"

    return f"""Split the user statement into 0..N ATOMIC facts. Each fact is one
durable assertion the user is making about themselves or their objects.

Return ONLY a JSON array (or {{"facts": [...]}}):
[
  {{
    "entity": "the named object, or empty when the fact is not about an object",
    "entity_type": "book|person|pet|object|place|account|project|other",
    "domain": "reading|personal|possession|relationship|plan|general",
    "kind": "preference|state|feedback|fact|agreement|correction",
    "predicate": "stable semantic predicate",
    "attributes": {{"predicate": "value"}},
    "polarity": "like|dislike|neutral|want|avoid",
    "summary": "concise fact in the user's language",
    "needs_clarification": false,
    "clarification_question": ""
  }}
]

Rules:
- Split compound statements into multiple facts; never drop facts.
- Decide entity_type from WHAT THE OBJECT IS, not the verb.
  "我的猫叫咪咪" -> entity_type=pet, domain=relationship, kind=fact.
  "我在读《三体》" -> entity_type=book, domain=reading, kind=state.
  "我买了台电脑" -> entity_type=object, domain=possession, kind=fact.
- Referents (它/这本/那只/这个人) must resolve to ONE concrete entity; if you
- When a referent (它/这匹/我的马) points to an entity from recent conversation,
  output that entity CANONICAL name (e.g. 黑马), NOT a new name.
- A rename/name fact always targets the EXISTING entity: entity="黑马",
  attributes={{"name": "李白"}}. NEVER create a new entity for a name/rename.
- A white horse first mentioned as "白色的" keeps entity="白色的" (pet);
  later clarifications reuse that exact name.

  cannot, set needs_clarification=true and give a question.
- Abstract preferences/states without an object -> entity empty,
  entity_type "other" or "", domain=general/personal, correct kind.
- The FIRST attribute key is the predicate used for updates:
  naming -> {{"name": "..."}}; color -> {{"color": "..."}}; age -> {{"age": ...}}.
  Same entity + same predicate = one version chain (rename supersedes).
- Reading statements use entity_type=book and domain=reading. Use predicate
  reading_status with canonical values want_to_read|reading|read|dropped, and
  predicate evaluation with liked|neutral|disliked|not_interested. One turn may
  produce several facts for several books. Do not infer asserted user state
  from negated, hypothetical, uncertain, quoted, or third-party statements.

User statement (resolve referents like 它/这本/那只 using the recent conversation):
{context_block}{text}
"""


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, dict)
            else str(item or "")
            for item in content
        )
    return str(content or "")


def _extract_facts_json(text: str) -> Any:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start < 0 or end <= start:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("turn compiler did not return JSON facts")
        return json.loads(cleaned[start : end + 1])


__all__ = [
    "AtomicFact",
    "CompiledTurn",
    "TurnFactCompiler",
    "compiled_turn_from_assertions",
]
