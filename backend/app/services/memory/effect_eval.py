from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field

from app.services.memory.canonicalizer import MemoryCanonicalizer
from app.services.memory.intent.router import MemoryIntentRouter
from app.services.memory.version_contracts import (
    ForgetMemoryTargetProposal,
    MemoryAssertionProposal,
    MemoryVersionRecord,
    SearchMemoryRequest,
)
from app.services.memory.version_search import VersionedMemorySearch


MemoryEffectDimension = Literal["route", "write", "forget", "query"]

ROUTE_STRATEGY = "rule_router"
WRITE_CURRENT_STRATEGY = "current_canonicalizer"
WRITE_AGGRESSIVE_STRATEGY = "aggressive_entity_normalizer"
FORGET_CURRENT_STRATEGY = "current_forget_resolver"
QUERY_LEXICAL_STRATEGY = "lexical_only"
QUERY_GUARDED_SEMANTIC_STRATEGY = "guarded_semantic"
QUERY_AGGRESSIVE_SEMANTIC_STRATEGY = "aggressive_semantic"
PRODUCTION_STRATEGIES = {
    ROUTE_STRATEGY,
    WRITE_CURRENT_STRATEGY,
    FORGET_CURRENT_STRATEGY,
    QUERY_GUARDED_SEMANTIC_STRATEGY,
}


class MemoryEffectExpected(BaseModel):
    status: str | None = None
    operation: str | None = None
    predicate: str | None = None
    scope: str | None = None
    schema_key: str | None = None
    value: dict[str, Any] | None = None
    qualifiers_include: dict[str, Any] = Field(default_factory=dict)
    qualifiers_exclude: list[str] = Field(default_factory=list)
    identity_group: str | None = None
    memories: list[str] = Field(default_factory=list)
    must_not_memories: list[str] = Field(default_factory=list)


class MemoryEffectCase(BaseModel):
    id: str
    dimension: MemoryEffectDimension
    category: str
    input: dict[str, Any] = Field(default_factory=dict)
    expected: MemoryEffectExpected
    tags: list[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, gt=0)


class MemoryEffectCaseResult(BaseModel):
    case_id: str
    dimension: MemoryEffectDimension
    category: str
    strategy: str
    passed: bool
    weight: float
    tags: list[str] = Field(default_factory=list)
    observed: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    failure_codes: list[str] = Field(default_factory=list)
    failure_family: str = ""


class MemoryFailureFamilySummary(BaseModel):
    strategy: str
    family: str
    total: int
    case_ids: list[str]


class MemoryEffectSummary(BaseModel):
    strategy: str
    dimension: MemoryEffectDimension
    total: int
    passed: int
    failed: int
    weighted_score: float
    weighted_total: float
    pass_rate: float


class MemoryQualityGateResult(BaseModel):
    gate: str
    passed: bool
    failures: list[str] = Field(default_factory=list)


class MemoryEffectReport(BaseModel):
    dataset_version: str
    case_count: int
    results: list[MemoryEffectCaseResult]
    summaries: list[MemoryEffectSummary]
    failure_families: list[MemoryFailureFamilySummary]
    quality_gates: list[MemoryQualityGateResult]
    production_passed: bool
    winners: dict[str, str]


def load_memory_effect_cases(path: Path) -> tuple[str, list[MemoryEffectCase]]:
    cases: list[MemoryEffectCase] = []
    dataset_version = ""
    with path.open("r", encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, 1):
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if line_number == 1 and "dataset_version" in payload:
                dataset_version = str(payload["dataset_version"])
                continue
            cases.append(MemoryEffectCase.model_validate(payload))
    if not dataset_version:
        dataset_version = "memory-effect-eval-v1"
    _assert_unique_case_ids(cases)
    return dataset_version, cases


class MemoryEffectEvaluator:
    """Run offline A/B-style memory quality checks over fixed cases."""

    def run(
        self,
        cases: list[MemoryEffectCase],
        *,
        dataset_version: str = "memory-effect-eval-v1",
    ) -> MemoryEffectReport:
        results: list[MemoryEffectCaseResult] = []
        identity_groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for case in cases:
            if case.dimension == "route":
                results.append(_evaluate_route(case))
            elif case.dimension == "write":
                for strategy in (
                    WRITE_CURRENT_STRATEGY,
                    WRITE_AGGRESSIVE_STRATEGY,
                ):
                    result = _evaluate_write(case, strategy=strategy)
                    results.append(result)
                    group = case.expected.identity_group
                    memory_key = result.observed.get("memory_key")
                    if group and isinstance(memory_key, str) and memory_key:
                        identity_groups[(strategy, group)].append(
                            (case.id, memory_key)
                        )
            elif case.dimension == "forget":
                results.append(_evaluate_forget(case))
            elif case.dimension == "query":
                for strategy in (
                    QUERY_LEXICAL_STRATEGY,
                    QUERY_GUARDED_SEMANTIC_STRATEGY,
                    QUERY_AGGRESSIVE_SEMANTIC_STRATEGY,
                ):
                    results.append(_evaluate_query(case, strategy=strategy))

        results.extend(
            _identity_group_results(
                cases=cases,
                identity_groups=identity_groups,
            )
        )
        summaries = _summarize(results)
        failure_families = _failure_families(results)
        quality_gates = _quality_gates(results, summaries)
        production_passed = all(
            result.passed
            for result in results
            if result.strategy in PRODUCTION_STRATEGIES
        ) and all(gate.passed for gate in quality_gates)
        return MemoryEffectReport(
            dataset_version=dataset_version,
            case_count=len(cases),
            results=results,
            summaries=summaries,
            failure_families=failure_families,
            quality_gates=quality_gates,
            production_passed=production_passed,
            winners=_winners(summaries),
        )


def _evaluate_route(case: MemoryEffectCase) -> MemoryEffectCaseResult:
    output = MemoryIntentRouter().decide(_request_from_case(case))
    observed: dict[str, Any]
    if output is None:
        observed = {"operation": "defer"}
    elif output.mode == "request_clarification":
        observed = {"operation": "clarify"}
    elif output.tool_calls:
        call = output.tool_calls[0]
        observed = {
            "operation": call.name,
            "predicate": _call_predicate(call.arguments),
            "scope": call.arguments.get("scope"),
        }
    else:
        observed = {"operation": output.mode}
    return _case_result(
        case,
        strategy=ROUTE_STRATEGY,
        observed=observed,
        checks=_compare_common(case.expected, observed),
    )


def _evaluate_write(
    case: MemoryEffectCase,
    *,
    strategy: str,
) -> MemoryEffectCaseResult:
    source_text = str(case.input.get("source_text") or "")
    assertions = [
        MemoryAssertionProposal.model_validate(item)
        for item in case.input.get("assertions", [])
    ]
    result = MemoryCanonicalizer().canonicalize(
        assertions,
        source_text=source_text,
    )
    observed: dict[str, Any] = {"status": result.status}
    if result.facts:
        fact = result.facts[0]
        value = dict(fact.value)
        qualifiers = dict(fact.qualifiers)
        if strategy == WRITE_AGGRESSIVE_STRATEGY:
            value, qualifiers = _aggressive_entity_projection(
                fact.schema_key,
                value,
                qualifiers,
            )
        observed.update(
            {
                "schema_key": fact.schema_key,
                "value": value,
                "qualifiers": qualifiers,
                "memory_key": _identity_fingerprint(
                    fact.schema_key,
                    value,
                    qualifiers,
                ),
            }
        )
    return _case_result(
        case,
        strategy=strategy,
        observed=observed,
        checks=_compare_common(case.expected, observed),
    )


def _evaluate_forget(case: MemoryEffectCase) -> MemoryEffectCaseResult:
    source_text = str(case.input.get("source_text") or "")
    targets = [
        ForgetMemoryTargetProposal.model_validate(item)
        for item in case.input.get("targets", [])
    ]
    result = MemoryCanonicalizer().resolve_targets(
        targets,
        source_text=source_text,
    )
    memories = [target.memory_key for target in result.targets]
    observed: dict[str, Any] = {
        "status": result.status,
        "memories": memories,
    }
    if result.targets:
        observed["schema_key"] = result.targets[0].schema_key
    return _case_result(
        case,
        strategy=FORGET_CURRENT_STRATEGY,
        observed=observed,
        checks=_compare_common(case.expected, observed),
    )


def _evaluate_query(
    case: MemoryEffectCase,
    *,
    strategy: str,
) -> MemoryEffectCaseResult:
    records = _records_from_case(case)
    request = SearchMemoryRequest.model_validate(case.input.get("request", {}))
    semantic_keys = tuple(case.input.get("semantic_memory_keys", []))
    if strategy == QUERY_LEXICAL_STRATEGY:
        receipt = VersionedMemorySearch().search(records, request)
        memories = [item.memory_key for item in receipt.memories]
    elif strategy == QUERY_GUARDED_SEMANTIC_STRATEGY:
        receipt = VersionedMemorySearch().search(
            records,
            request,
            semantic_memory_keys=semantic_keys,
        )
        memories = [item.memory_key for item in receipt.memories]
    else:
        memories = _aggressive_semantic_memories(
            records,
            request,
            semantic_keys,
        )
    observed = {
        "status": "completed" if memories else "empty",
        "memories": memories,
    }
    return _case_result(
        case,
        strategy=strategy,
        observed=observed,
        checks=_compare_common(case.expected, observed),
    )


def _request_from_case(case: MemoryEffectCase):
    previous = case.input.get("previous_user_turn")
    conversation = []
    if previous:
        conversation.append(type("Turn", (), {"role": "user", "content": previous})())
    working = None
    pending = case.input.get("pending_question")
    if pending:
        working = type("Working", (), {"pending_question": pending})()
    context = type(
        "Context",
        (),
        {"conversation": conversation, "working_state": working},
    )()
    return type(
        "Request",
        (),
        {
            "current_user_message": str(case.input.get("text") or ""),
            "context": context,
        },
    )()


def _call_predicate(arguments: dict[str, Any]) -> str | None:
    if "predicate" in arguments:
        return str(arguments.get("predicate") or "")
    assertions = arguments.get("assertions")
    if isinstance(assertions, list) and assertions:
        return str(assertions[0].get("predicate") or "")
    targets = arguments.get("targets")
    if isinstance(targets, list) and targets:
        return str(targets[0].get("predicate") or "")
    return None


def _records_from_case(case: MemoryEffectCase) -> list[MemoryVersionRecord]:
    return [
        _record_from_spec(case.id, index, spec)
        for index, spec in enumerate(case.input.get("records", []), 1)
    ]


def _record_from_spec(
    case_id: str,
    index: int,
    spec: dict[str, Any],
) -> MemoryVersionRecord:
    memory_key = str(spec["memory_key"])
    version_no = int(spec.get("version_no") or 1)
    current = bool(spec.get("current", True))
    record_id = _uuid(f"{case_id}:{index}:{memory_key}:{version_no}")
    return MemoryVersionRecord(
        id=record_id,
        user_id=_uuid(f"{case_id}:user"),
        thread_id=_uuid(f"{case_id}:thread"),
        chain_id=_uuid(f"{case_id}:chain:{memory_key}"),
        schema_key=str(spec["schema_key"]),
        memory_key=memory_key,
        version_no=version_no,
        operation=str(spec.get("operation") or "create"),
        previous_version_id=(
            _uuid(f"{case_id}:prev:{memory_key}:{version_no - 1}")
            if version_no > 1
            else None
        ),
        superseded_by=None if current else _uuid(f"{case_id}:next:{memory_key}"),
        source_event_id=_uuid(f"{case_id}:source:{index}"),
        receipt_id=f"receipt-{case_id}-{index}",
        canonical_hash="1" * 64,
        schema_version=1,
        subject=str(spec.get("subject") or "self"),
        predicate=str(spec.get("predicate") or spec["schema_key"]),
        value=dict(spec.get("value") or {}),
        qualifiers=dict(spec.get("qualifiers") or {}),
        evidence_quote=str(spec.get("evidence") or ""),
        is_tombstone=bool(spec.get("is_tombstone", False)),
        valid_from=_parse_datetime(spec.get("valid_from")),
        valid_to=(
            None
            if current
            else _parse_datetime(spec.get("valid_to") or "2026-02-01T00:00:00Z")
        ),
    )


def _aggressive_entity_projection(
    schema_key: str,
    value: dict[str, Any],
    qualifiers: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if schema_key not in {
        "possession.entity",
        "preference.entity",
        "relationship.entity",
    }:
        return value, qualifiers
    entity = str(value.get("entity") or "").strip()
    if not entity:
        return value, qualifiers
    species = ""
    suffix = ""
    for marker, mapped in (
        ("猫咪", "cat"),
        ("狗狗", "dog"),
        ("猫", "cat"),
        ("狗", "dog"),
    ):
        if marker in entity:
            species = mapped
            suffix = marker if entity.endswith(marker) else ""
            break
    if not species:
        return value, qualifiers
    next_value = dict(value)
    if suffix and len(entity) > len(suffix):
        next_value["entity"] = entity[: -len(suffix)].strip()
    next_qualifiers = dict(qualifiers)
    next_qualifiers.setdefault("entity_type", "pet")
    next_qualifiers["species"] = species
    return next_value, next_qualifiers


def _aggressive_semantic_memories(
    records: list[MemoryVersionRecord],
    request: SearchMemoryRequest,
    semantic_keys: tuple[str, ...],
) -> list[str]:
    receipt = VersionedMemorySearch().search(
        records,
        request,
        semantic_memory_keys=semantic_keys,
    )
    memories = [item.memory_key for item in receipt.memories]
    if memories or request.scope != "current" or request.predicate:
        return memories
    current_records = {
        item.memory_key: item
        for item in records
        if item.superseded_by is None and not item.is_tombstone
    }
    for key in semantic_keys:
        if key in current_records and key not in memories:
            memories.append(key)
    return memories


def _compare_common(
    expected: MemoryEffectExpected,
    observed: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    for field in ("status", "operation", "predicate", "scope", "schema_key"):
        expected_value = getattr(expected, field)
        if expected_value is not None and observed.get(field) != expected_value:
            failures.append(f"{field}_mismatch")
    if expected.value is not None and observed.get("value") != expected.value:
        failures.append("value_mismatch")
    qualifiers = observed.get("qualifiers") or {}
    for key, value in expected.qualifiers_include.items():
        if qualifiers.get(key) != value:
            failures.append(f"qualifier_missing:{key}")
    for key in expected.qualifiers_exclude:
        if key in qualifiers:
            failures.append(f"qualifier_forbidden:{key}")
    if expected.memories and observed.get("memories") != expected.memories:
        failures.append("memories_mismatch")
    if not expected.memories and "memories" in observed and observed["memories"]:
        failures.append("unexpected_memories")
    for memory_key in expected.must_not_memories:
        if memory_key in (observed.get("memories") or []):
            failures.append(f"forbidden_memory:{memory_key}")
    return failures


def _case_result(
    case: MemoryEffectCase,
    *,
    strategy: str,
    observed: dict[str, Any],
    checks: list[str],
) -> MemoryEffectCaseResult:
    return MemoryEffectCaseResult(
        case_id=case.id,
        dimension=case.dimension,
        category=case.category,
        strategy=strategy,
        passed=not checks,
        weight=case.weight,
        tags=list(case.tags),
        observed=observed,
        expected=case.expected.model_dump(exclude_none=True),
        failure_codes=checks,
        failure_family=_failure_family(
            dimension=case.dimension,
            category=case.category,
            tags=case.tags,
            observed=observed,
            expected=case.expected,
            failure_codes=checks,
        ),
    )


def _identity_group_results(
    *,
    cases: list[MemoryEffectCase],
    identity_groups: dict[tuple[str, str], list[tuple[str, str]]],
) -> list[MemoryEffectCaseResult]:
    by_id = {case.id: case for case in cases}
    results: list[MemoryEffectCaseResult] = []
    for (strategy, group), items in identity_groups.items():
        if len(items) < 2:
            continue
        unique_keys = sorted({memory_key for _case_id, memory_key in items})
        case_ids = [case_id for case_id, _memory_key in items]
        weight = sum(by_id[case_id].weight for case_id in case_ids) / len(case_ids)
        results.append(
            MemoryEffectCaseResult(
                case_id=f"identity_group:{group}",
                dimension="write",
                category="entity_identity",
                strategy=strategy,
                passed=len(unique_keys) == 1,
                weight=weight,
                tags=["identity_group"],
                observed={"case_ids": case_ids, "memory_keys": unique_keys},
                expected={"identity_group": group, "same_memory_key": True},
                failure_codes=[] if len(unique_keys) == 1 else ["identity_split"],
                failure_family=(
                    "" if len(unique_keys) == 1 else "entity_identity_split"
                ),
            )
        )
    return results


def _summarize(results: list[MemoryEffectCaseResult]) -> list[MemoryEffectSummary]:
    buckets: dict[tuple[str, str], list[MemoryEffectCaseResult]] = defaultdict(list)
    for result in results:
        buckets[(result.strategy, result.dimension)].append(result)
    summaries: list[MemoryEffectSummary] = []
    for (strategy, dimension), items in sorted(buckets.items()):
        weighted_total = sum(item.weight for item in items)
        weighted_score = sum(item.weight for item in items if item.passed)
        summaries.append(
            MemoryEffectSummary(
                strategy=strategy,
                dimension=dimension,  # type: ignore[arg-type]
                total=len(items),
                passed=sum(1 for item in items if item.passed),
                failed=sum(1 for item in items if not item.passed),
                weighted_score=round(weighted_score, 4),
                weighted_total=round(weighted_total, 4),
                pass_rate=round(
                    weighted_score / weighted_total if weighted_total else 0.0,
                    4,
                ),
            )
        )
    return summaries


def _winners(summaries: list[MemoryEffectSummary]) -> dict[str, str]:
    winners: dict[str, str] = {}
    by_dimension: dict[str, list[MemoryEffectSummary]] = defaultdict(list)
    for summary in summaries:
        by_dimension[summary.dimension].append(summary)
    for dimension, items in by_dimension.items():
        winner = max(
            items,
            key=lambda item: (item.pass_rate, item.weighted_score, -item.failed),
        )
        winners[dimension] = winner.strategy
    return winners


def _failure_families(
    results: list[MemoryEffectCaseResult],
) -> list[MemoryFailureFamilySummary]:
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for result in results:
        if result.passed or not result.failure_family:
            continue
        buckets[(result.strategy, result.failure_family)].append(result.case_id)
    return [
        MemoryFailureFamilySummary(
            strategy=strategy,
            family=family,
            total=len(case_ids),
            case_ids=sorted(case_ids),
        )
        for (strategy, family), case_ids in sorted(buckets.items())
    ]


def _quality_gates(
    results: list[MemoryEffectCaseResult],
    summaries: list[MemoryEffectSummary],
) -> list[MemoryQualityGateResult]:
    production = [
        result for result in results if result.strategy in PRODUCTION_STRATEGIES
    ]
    return [
        _gate(
            "route_safety_zero_false_memory_action",
            _failed_case_ids(
                production,
                strategy=ROUTE_STRATEGY,
                tags={"safety", "prohibition", "non_personal_entity"},
            ),
        ),
        _gate(
            "write_safety_zero_bad_admission",
            _failed_case_ids(
                production,
                strategy=WRITE_CURRENT_STRATEGY,
                tags={"secret", "evidence", "safety"},
            ),
        ),
        _gate(
            "entity_over_normalization_zero_tolerance",
            _failed_case_ids(
                production,
                strategy=WRITE_CURRENT_STRATEGY,
                tags={"over_normalization"},
            ),
        ),
        _gate(
            "forget_safety_zero_bad_deletion",
            _failed_case_ids(
                production,
                strategy=FORGET_CURRENT_STRATEGY,
                tags={"safety", "prohibition"},
            ),
        ),
        _gate(
            "query_false_recall_zero_tolerance",
            _failed_case_ids(
                production,
                strategy=QUERY_GUARDED_SEMANTIC_STRATEGY,
                tags={
                    "safety",
                    "semantic_guard",
                    "history_guard",
                    "non_personal_entity",
                    "prohibition",
                },
            ),
        ),
        _gate(
            "entity_identity_consistency",
            _failed_case_ids(
                production,
                strategy=WRITE_CURRENT_STRATEGY,
                tags={"identity_group"},
            ),
        ),
        _gate(
            "production_dimension_minimum_score",
            [
                f"{summary.dimension}:{summary.pass_rate}"
                for summary in summaries
                if summary.strategy in PRODUCTION_STRATEGIES
                and summary.pass_rate < 0.98
            ],
        ),
        _gate(
            "ablation_must_expose_tradeoffs",
            []
            if any(
                result.strategy not in PRODUCTION_STRATEGIES and not result.passed
                for result in results
            )
            else ["no_candidate_strategy_failed"],
        ),
    ]


def _gate(name: str, failures: list[str]) -> MemoryQualityGateResult:
    return MemoryQualityGateResult(
        gate=name,
        passed=not failures,
        failures=failures,
    )


def _failed_case_ids(
    results: list[MemoryEffectCaseResult],
    *,
    strategy: str,
    tags: set[str],
) -> list[str]:
    return sorted(
        result.case_id
        for result in results
        if result.strategy == strategy
        and tags.intersection(result.tags)
        and not result.passed
    )


def _failure_family(
    *,
    dimension: MemoryEffectDimension,
    category: str,
    tags: list[str],
    observed: dict[str, Any],
    expected: MemoryEffectExpected,
    failure_codes: list[str],
) -> str:
    if not failure_codes:
        return ""
    tag_set = set(tags)
    if any(code.startswith("forbidden_memory") for code in failure_codes):
        return "false_recall"
    if "unexpected_memories" in failure_codes:
        return "false_recall"
    if dimension == "forget":
        if expected.status in {"rejected", "clarification_required"}:
            return "unsafe_forget_admission"
        if "memories_mismatch" in failure_codes:
            return "forget_target_mismatch"
        return "forget_contract_mismatch"
    if "memories_mismatch" in failure_codes:
        observed_memories = observed.get("memories") or []
        return "missed_recall" if not observed_memories else "wrong_recall_order"
    if "identity_split" in failure_codes:
        return "entity_identity_split"
    if tag_set.intersection({"over_normalization", "media_title", "organization"}):
        if any(
            code.startswith("qualifier_forbidden") or code == "value_mismatch"
            for code in failure_codes
        ):
            return "over_normalization"
    if dimension == "route":
        return "route_misclassification"
    if dimension == "write":
        if expected.status in {"rejected", "clarification_required"}:
            return "unsafe_write_admission"
        if "value_mismatch" in failure_codes:
            return "wrong_canonical_fact"
        return "write_contract_mismatch"
    if category.endswith("guard") or tag_set.intersection({"semantic_guard", "history_guard"}):
        return "query_guardrail_failure"
    return "contract_mismatch"


def _identity_fingerprint(
    schema_key: str,
    value: dict[str, Any],
    qualifiers: dict[str, Any],
) -> str:
    entity = value.get("entity")
    if entity:
        identity = {
            "schema_key": schema_key,
            "entity": entity,
            "species": qualifiers.get("species"),
            "entity_role": qualifiers.get("entity_role"),
        }
    else:
        identity = {"schema_key": schema_key, "value": value}
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return schema_key + ":" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _parse_datetime(value: Any) -> datetime:
    if value:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _uuid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, value)


def _assert_unique_case_ids(cases: list[MemoryEffectCase]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for case in cases:
        if case.id in seen:
            duplicates.append(case.id)
        seen.add(case.id)
    if duplicates:
        raise ValueError("duplicate memory effect case ids: " + ", ".join(duplicates))


__all__ = [
    "FORGET_CURRENT_STRATEGY",
    "MemoryEffectEvaluator",
    "MemoryEffectReport",
    "PRODUCTION_STRATEGIES",
    "load_memory_effect_cases",
]
