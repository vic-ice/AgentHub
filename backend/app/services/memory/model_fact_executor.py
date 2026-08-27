"""Deterministically map model-understood assertions to version-store facts.

This component performs no language understanding and makes no business
inference.  The model supplies subject, predicate and values; the executor
only validates the contract, selects the registered storage schema and builds
stable identity/hash fields required by the version store.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.services.memory.entity_normalizer import normalize_entity_fact
from app.services.memory.guardrails import guard_memory_write_source
from app.services.memory.version_contracts import (
    CanonicalMemoryFact,
    MemoryAssertionProposal,
    MemoryCanonicalizationResult,
)
from app.services.memory.versioned_schema_registry import (
    VersionedMemorySchema,
    VersionedMemorySchemaRegistry,
)


_SELF_SUBJECTS = frozenset({"self", "user", "me", "myself", "我", "本人"})
_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "credential",
        "credentials",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "token",
    }
)
_CREDENTIAL_TEXT_RE = re.compile(
    r"(?:\bsk-[a-z0-9_-]{12,}\b|"
    r"\bbearer\s+[a-z0-9._~+/=-]{12,}|"
    r"\b(?:api[_ -]?key|password|secret|token)\s*[:=]\s*\S{6,})",
    re.IGNORECASE,
)


class ModelFactExecutor:
    """Execute structured model facts into the durable-memory contract."""

    def __init__(
        self,
        registry: VersionedMemorySchemaRegistry | None = None,
    ) -> None:
        self._registry = registry or VersionedMemorySchemaRegistry()

    def prepare(
        self,
        assertions: list[MemoryAssertionProposal],
        *,
        source_text: str,
    ) -> MemoryCanonicalizationResult:
        guard = guard_memory_write_source(source_text)
        if guard.blocked:
            return MemoryCanonicalizationResult(
                status=(
                    "clarification_required"
                    if guard.reason_code
                    in {
                        "memory_write_question_source",
                        "memory_write_uncertain_source",
                    }
                    else "rejected"
                ),
                clarification_question=(
                    "这句话像是在提问或表达不确定，请用确定陈述说明要记住的事实。"
                    if guard.reason_code
                    in {
                        "memory_write_question_source",
                        "memory_write_uncertain_source",
                    }
                    else ""
                ),
                reason_codes=[guard.reason_code],
            )

        facts: list[CanonicalMemoryFact] = []
        for assertion in assertions:
            prepared = self._prepare_one(assertion, source_text=source_text)
            if isinstance(prepared, MemoryCanonicalizationResult):
                return prepared
            facts.append(prepared)

        conflicts: dict[str, set[str]] = {}
        for fact in facts:
            conflicts.setdefault(fact.memory_key, set()).add(fact.canonical_hash)
        conflict_key = next(
            (key for key, hashes in conflicts.items() if len(hashes) > 1),
            "",
        )
        if conflict_key:
            return _result(
                "同一事实出现了多个不同值，请直接说明最终希望保留的值。",
                "batch_memory_key_conflict",
                conflict_key,
            )
        unique = {
            (fact.memory_key, fact.canonical_hash): fact for fact in facts
        }
        return MemoryCanonicalizationResult(
            status="ready",
            facts=list(unique.values()),
            reason_codes=["model_fact_execution_prepared"],
        )

    def _prepare_one(
        self,
        assertion: MemoryAssertionProposal,
        *,
        source_text: str,
    ) -> CanonicalMemoryFact | MemoryCanonicalizationResult:
        schema = self._registry.resolve_predicate(assertion.predicate)
        if schema is None:
            return _result(
                f"无法执行未注册的记忆操作“{assertion.predicate}”。",
                "unknown_memory_predicate",
            )
        subject = _text(assertion.subject).casefold()
        if schema.self_subject_only and subject not in _SELF_SUBJECTS:
            return _result(
                "请明确这是关于你本人的事实，还是关于其他人的信息。",
                "subject_scope_ambiguous",
            )
        canonical_subject = "self" if schema.self_subject_only else subject
        evidence = _text(assertion.evidence_quote)
        normalized_source = _text(source_text)
        if evidence.casefold() not in normalized_source.casefold():
            return MemoryCanonicalizationResult(
                status="rejected",
                reason_codes=["evidence_not_found_in_user_source"],
            )
        try:
            value = _json_object(assertion.value)
            qualifiers = _json_object(assertion.qualifiers)
        except ValueError:
            return MemoryCanonicalizationResult(
                status="rejected",
                reason_codes=["non_json_memory_value"],
            )
        if _contains_secret(value) or _contains_secret(qualifiers) or (
            _CREDENTIAL_TEXT_RE.search(evidence)
        ):
            return MemoryCanonicalizationResult(
                status="rejected",
                reason_codes=["secret_or_credential_forbidden"],
            )
        value, qualifiers = normalize_entity_fact(
            schema.schema_key,
            value,
            qualifiers,
        )
        missing = [
            *(_missing(value, schema.required_value_fields)),
            *(_missing(qualifiers, schema.required_qualifier_fields)),
        ]
        if missing:
            return _result(
                "这条事实还缺少必要信息：" + ", ".join(missing) + "。",
                "incomplete_canonical_fact",
            )
        if schema.schema_key == "preference.entity" and str(
            value.get("polarity") or ""
        ).casefold() not in {"like", "dislike", "avoid", "want", "neutral"}:
            return _result(
                "请明确这是喜欢、不喜欢、想要还是避免。",
                "invalid_preference_polarity",
            )

        identity = _identity(schema, value, qualifiers)
        if (
            not schema.self_subject_only
            and schema.schema_key != "entity.name"
            and canonical_subject not in _SELF_SUBJECTS
        ):
            identity = {"subject": canonical_subject, **identity}
        canonical_payload = {
            "schema_key": schema.schema_key,
            "schema_version": schema.version,
            "subject": canonical_subject,
            "value": value,
            "qualifiers": qualifiers,
        }
        return CanonicalMemoryFact(
            schema_key=schema.schema_key,
            memory_key=_memory_key(schema, identity),
            schema_version=schema.version,
            subject=canonical_subject,
            predicate=schema.schema_key,
            value=value,
            qualifiers=qualifiers,
            evidence_quote=evidence,
            canonical_hash=hashlib.sha256(
                _stable_json(canonical_payload).encode("utf-8")
            ).hexdigest(),
        )


def _identity(
    schema: VersionedMemorySchema,
    value: dict[str, Any],
    qualifiers: dict[str, Any],
) -> dict[str, Any]:
    return {
        "value": {key: value[key] for key in schema.identity_value_fields},
        "qualifiers": {
            key: qualifiers[key]
            for key in schema.identity_qualifier_fields
            if key in qualifiers and _has_value(qualifiers[key])
        },
    }


def _memory_key(schema: VersionedMemorySchema, identity: dict[str, Any]) -> str:
    if schema.cardinality == "single":
        return f"{schema.schema_key}:self"
    digest = hashlib.sha256(
        _stable_json(identity).encode("utf-8")
    ).hexdigest()[:32]
    return f"{schema.schema_key}:{digest}"


def _json_object(value: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key).strip().lower(): _json_value(child)
        for key, child in sorted(
            value.items(), key=lambda item: str(item[0]).casefold()
        )
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return _json_object(value)
    raise ValueError("memory values must be JSON-compatible")


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).strip().casefold() in _SECRET_KEYS
            or _contains_secret(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return isinstance(value, str) and bool(_CREDENTIAL_TEXT_RE.search(value))


def _missing(value: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    return [
        field for field in required
        if field not in value or not _has_value(value[field])
    ]


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


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _result(
    question: str,
    *reason_codes: str,
) -> MemoryCanonicalizationResult:
    return MemoryCanonicalizationResult(
        status="clarification_required",
        clarification_question=question,
        reason_codes=list(reason_codes),
    )


__all__ = ["ModelFactExecutor"]
