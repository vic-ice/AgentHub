from __future__ import annotations

from app.services.memory.guardrails import (
    guard_memory_forget_source,
    guard_memory_write_source,
)

_QUESTION_PARTICLES = "吗么呢吧是啥什么"
_NAMING_CUES = ("叫", "名字", "名称", "昵称", "英文名")


def validate_remember_write(
    source_text: str,
    assertions: list[dict],
) -> tuple[bool, str]:
    """Return (ok, reason_code) before any memory write commits.

    Deterministic guard for model-extracted assertions: question / uncertain /
    negation / metalinguistic sources never commit, and extracted slots must be
    structurally plausible (entity present, no question particles in entity,
    entity name relation never stores entity == name).
    """

    normalized = " ".join(str(source_text or "").split()).strip()
    if not normalized:
        return False, "missing_source"
    guard = guard_memory_write_source(normalized)
    if guard.blocked:
        return False, guard.reason_code
    for assertion in assertions:
        predicate = str(assertion.get("predicate") or "").strip()
        value = assertion.get("value") or {}
        if not isinstance(value, dict):
            return False, "invalid_value"
        entity = str(value.get("entity") or "").strip()
        name = str(value.get("name") or "").strip()
        if predicate in {"has", "possess", "entity_name"} and not entity:
            return False, "missing_entity"
        if entity and any(ch in entity for ch in _QUESTION_PARTICLES):
            return False, "entity_contains_question_particle"
        if predicate == "entity_name" and entity and name and entity == name:
            return False, "entity_equals_name"
    has_possess = any(
        str(item.get("predicate") or "") in {"has", "possess"}
        for item in assertions
    )
    has_naming = any(cue in normalized for cue in _NAMING_CUES)
    has_entity_name = any(
        str(item.get("predicate") or "") == "entity_name"
        for item in assertions
    )
    if has_possess and has_naming and not has_entity_name:
        return False, "naming_pattern_missing_entity_name"
    return True, ""


def validate_forget_write(source_text: str) -> tuple[bool, str]:
    """Return (ok, reason_code) before a forget tombstone commits."""

    normalized = " ".join(str(source_text or "").split()).strip()
    if not normalized:
        return False, "missing_source"
    guard = guard_memory_forget_source(normalized)
    if guard.blocked:
        return False, guard.reason_code
    return True, ""


__all__ = ["validate_forget_write", "validate_remember_write"]
