"""Unified memory classification: domain (领域) + kind (事实类型).

Boundary (frozen 2026-08-14):
- The MODEL understands natural language and PROPOSES domain + kind for new
  user messages (semantic interpreter output). This module never re-guesses
  semantics from keywords/subject for new writes.
- This module is responsible for: enum validation, legacy-field compatibility
  (deriving (domain, kind) from old type/subject/state_key during migration),
  and a deterministic FALLBACK only when the model did not provide one.
- `general` is a legitimate domain and is allowed to exist; it is NOT a reason
  to clarify.
"""

from __future__ import annotations

from typing import Any

MEMORY_DOMAINS = frozenset(
    {"reading", "personal", "possession", "relationship", "plan", "general"}
)
MEMORY_KINDS = frozenset(
    {"preference", "state", "feedback", "fact", "agreement", "correction"}
)

_READING_SUBJECTS = frozenset(
    {
        "book",
        "author",
        "tag",
        "theme",
        "style",
        "genre",
        "mood",
        "pacing",
        "content",
    }
)
ENTITY_TYPE_TO_DOMAIN = {
    "book": "reading",
    "person": "relationship",
    "pet": "relationship",
    "object": "possession",
    "account": "possession",
    "place": "personal",
    "project": "plan",
    "other": "general",
}


def derive_domain_from_entity_type(entity_type: Any) -> str:
    """Domain derived from the model-proposed entity type (single source)."""
    return ENTITY_TYPE_TO_DOMAIN.get(_token(entity_type), "general")
_STATE_DOMAIN_BY_SUBJECT = {
    "book": "reading",
    "author": "reading",
    "tag": "reading",
    "theme": "reading",
    "style": "reading",
    "genre": "reading",
    "mood": "reading",
    "pacing": "reading",
    "content": "reading",
    "pet": "possession",
    "object": "possession",
    "person": "relationship",
}


def _token(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def derive_domain_kind(
    memory_type: Any,
    subject: Any,
    state_key: Any = "",
) -> tuple[str, str]:
    """Derive (domain, kind) from legacy memory fields (single source of truth)."""
    t = _token(memory_type)
    s = _token(subject)
    key = _token(state_key)

    if key.startswith("identity."):
        return "personal", "fact"
    if key.startswith("possession."):
        return "possession", "fact"
    if key.startswith("relationship."):
        return "relationship", "fact"
    if key.startswith("instruction."):
        return "personal", "agreement"
    if key.startswith("plan."):
        return "plan", "fact"
    if key.startswith("feedback."):
        return ("reading", "feedback") if s in _READING_SUBJECTS else ("personal", "feedback")
    if key.startswith("preference."):
        return (
            ("reading", "preference")
            if s in _READING_SUBJECTS
            else ("personal", "preference")
        )
    if key.startswith("temporary.") or key.startswith("state."):
        return _STATE_DOMAIN_BY_SUBJECT.get(s, "personal"), "state"

    if t == "reading_state":
        return "reading", "state"
    if t == "feedback":
        return ("reading", "feedback") if s in _READING_SUBJECTS else ("personal", "feedback")
    if t == "preference":
        return (
            ("reading", "preference")
            if s in _READING_SUBJECTS
            else ("personal", "preference")
        )
    if t == "entity":
        return ENTITY_TYPE_TO_DOMAIN.get(s, "general"), "fact"
    if t == "instruction":
        return "personal", "agreement"
    if t == "state":
        return _STATE_DOMAIN_BY_SUBJECT.get(s, "personal"), "state"
    if t == "correction":
        return "general", "correction"
    return "general", "fact"


__all__ = [
    "MEMORY_DOMAINS",
    "MEMORY_KINDS",
    "derive_domain_kind",
]