from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.research.contracts import normalize_text
from app.services.research.loop.admission_projection import evidence_was_admitted


RESEARCH_EVOLUTION_CONTRACT_VERSION = "research-evolution-v1"
MAX_CONFIRMED_FACTS = 20
MAX_OPEN_QUESTIONS = 10
MAX_INFO_GAPS = 10
MAX_EXHAUSTED_QUERIES = 8
MAX_CONFLICTS = 5


class ConfirmedResearchFact(BaseModel):
    """One source-backed finding admitted into the evolving report."""

    content: str = Field(min_length=1, max_length=320)
    source: str = Field(default="", max_length=2_000)
    confidence: Literal["high", "medium", "low"] = "medium"
    step: int = Field(default=0, ge=0)


class EvolvingResearchReport(BaseModel):
    """Bounded research workspace projected from trusted execution receipts.

    This is a lossy, system-rebuilt projection used by the Controller to keep
    research focus. Facts are leads, not conclusions; single-source or
    low-confidence facts remain unverified.
    """

    result_mode: str = "evolving_research_report"
    contract_version: str = RESEARCH_EVOLUTION_CONTRACT_VERSION
    objective: str = ""
    status: str = "running"
    step_count: int = Field(default=0, ge=0)
    confirmed_facts: list[ConfirmedResearchFact] = Field(
        default_factory=list,
        max_length=MAX_CONFIRMED_FACTS,
    )
    open_questions: list[str] = Field(
        default_factory=list,
        max_length=MAX_OPEN_QUESTIONS,
    )
    information_gaps: list[str] = Field(
        default_factory=list,
        max_length=MAX_INFO_GAPS,
    )
    current_focus: str = Field(default="", max_length=1_000)
    exhausted_queries: list[str] = Field(
        default_factory=list,
        max_length=MAX_EXHAUSTED_QUERIES,
    )
    conflicts: list[str] = Field(
        default_factory=list,
        max_length=MAX_CONFLICTS,
    )
    trust_note: str = (
        "Facts are leads rebuilt from trusted receipts, not verified "
        "conclusions. Single-source or low-confidence facts remain "
        "unverified until independently corroborated."
    )


def project_evolving_report(receipts: list[Any]) -> EvolvingResearchReport | None:
    """Project one bounded evolving report from accumulated ActionReceipts.

    Pure projection: reads receipt outputs only, performs no state writes and
    no external calls. Returns None when no research activity is visible.
    """

    normalized = [receipt for receipt in receipts or [] if _is_receipt(receipt)]
    if not normalized:
        return None
    objective = _objective(normalized)
    if not objective:
        return None

    facts = _admitted_facts(normalized)
    assessment = _latest_gap_assessment(normalized)
    report_output = _latest_report_output(normalized)
    conflicts = _conflicts(report_output)
    gaps = _field_list(assessment, "gaps")
    gap_descriptions = _field_list(assessment, "gap_descriptions")
    exhausted = _field_list(assessment, "exhausted_queries") or (
        _exhausted_from_receipts(normalized)
    )
    next_actions = _field_list(assessment, "next_actions")
    step_count = _executed_search_rounds(normalized)

    status = _status(
        assessment=assessment,
        report_status=(
            str(report_output.get("report_status") or "")
            if report_output
            else ""
        ),
        fact_count=len(facts),
    )
    focus = "; ".join(
        _clean(item) for item in next_actions if _clean(item)
    )
    if not focus and gap_descriptions:
        focus = "; ".join(gap_descriptions[:3])
    return EvolvingResearchReport(
        objective=objective,
        status=status,
        step_count=step_count,
        confirmed_facts=facts[:MAX_CONFIRMED_FACTS],
        open_questions=gap_descriptions[:MAX_OPEN_QUESTIONS],
        information_gaps=gaps[:MAX_INFO_GAPS],
        current_focus=focus[:1_000],
        exhausted_queries=exhausted[:MAX_EXHAUSTED_QUERIES],
        conflicts=conflicts[:MAX_CONFLICTS],
    )


def _is_receipt(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("operation") and "output" in value)
    return bool(getattr(value, "operation", None) and hasattr(value, "output"))


def _receipt_output(receipt: Any) -> dict[str, Any]:
    output = receipt.get("output") if isinstance(receipt, dict) else getattr(
        receipt, "output", None
    )
    return output if isinstance(output, dict) else {}


def _receipt_business_input(receipt: Any) -> dict[str, Any]:
    value = (
        receipt.get("business_input")
        if isinstance(receipt, dict)
        else getattr(receipt, "business_input", None)
    )
    return value if isinstance(value, dict) else {}


def _receipt_operation(receipt: Any) -> str:
    return str(
        receipt.get("operation")
        if isinstance(receipt, dict)
        else getattr(receipt, "operation", "") or ""
    )


def _clean(value: Any) -> str:
    return " ".join(normalize_text(value).split())


def _objective(receipts: list[Any]) -> str:
    for receipt in reversed(receipts):
        if _receipt_operation(receipt) != "start_research":
            continue
        output = _receipt_output(receipt)
        run = output.get("run")
        if isinstance(run, dict) and _clean(run.get("objective")):
            return _clean(run["objective"])
        state = output.get("state")
        if isinstance(state, dict) and _clean(state.get("objective")):
            return _clean(state["objective"])
        business = _receipt_business_input(receipt)
        if _clean(business.get("objective")):
            return _clean(business["objective"])
    for receipt in reversed(receipts):
        business = _receipt_business_input(receipt)
        if _receipt_operation(receipt) in {
            "acquire_research_sources",
            "collect_research_sources",
        } and _clean(business.get("objective")):
            return _clean(business["objective"])
    return ""


def _admitted_facts(receipts: list[Any]) -> list[ConfirmedResearchFact]:
    facts: list[ConfirmedResearchFact] = []
    seen: set[str] = set()
    for receipt in receipts:
        if _receipt_operation(receipt) != "add_evidence":
            continue
        output = _receipt_output(receipt)
        results = output.get("results")
        candidates = results if isinstance(results, list) else [output]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if not evidence_was_admitted(candidate):
                continue
            for item in _admission_evidence_items(candidate):
                claim = _clean(item.get("claim"))
                if not claim or claim in seen:
                    continue
                seen.add(claim)
                facts.append(
                    ConfirmedResearchFact(
                        content=claim[:320],
                        source=_clean(item.get("source_url"))
                        or _clean(item.get("source_title")),
                        confidence=_confidence(item.get("quality")),
                        step=_step_from_evidence_item(item),
                    )
                )
    return facts


def _admission_evidence_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    steps = result.get("steps")
    if isinstance(steps, list):
        for step in reversed(steps):
            if not isinstance(step, dict):
                continue
            if step.get("step_type") != "add_evidence":
                continue
            step_output = step.get("output")
            if not isinstance(step_output, dict):
                continue
            admission = step_output.get("evidence_admission")
            if isinstance(admission, dict):
                evidence = admission.get("evidence")
                if isinstance(evidence, dict):
                    return [evidence]
    evidence = result.get("evidence")
    if isinstance(evidence, list):
        return [item for item in evidence if isinstance(item, dict)]
    return []


def _step_from_evidence_item(item: dict[str, Any]) -> int:
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        source_record = metadata.get("source_record")
        if isinstance(source_record, dict):
            research_round = source_record.get("research_round")
            if isinstance(research_round, int):
                return max(0, research_round)
        research_round = metadata.get("research_round")
        if isinstance(research_round, int):
            return max(0, research_round)
    return 0


def _confidence(value: Any) -> Literal["high", "medium", "low"]:
    token = str(value or "").strip().lower()
    if token == "high":
        return "high"
    if token == "medium":
        return "medium"
    return "low"


def _latest_gap_assessment(receipts: list[Any]) -> dict[str, Any] | None:
    for receipt in reversed(receipts):
        if _receipt_operation(receipt) != "evaluate_research_gaps":
            continue
        output = _receipt_output(receipt)
        if output.get("gaps") is not None or output.get("result_mode"):
            return output
    return None


def _field_list(
    payload: dict[str, Any] | None,
    field: str,
) -> list[str]:
    if payload is None:
        return []
    value = payload.get(field)
    if not isinstance(value, list):
        return []
    return [_clean(item) for item in value if _clean(item)]


def _latest_report_output(receipts: list[Any]) -> dict[str, Any] | None:
    for receipt in reversed(receipts):
        if _receipt_operation(receipt) != "build_research_report":
            continue
        output = _receipt_output(receipt)
        if output:
            return output
    return None


def _conflicts(report_output: dict[str, Any] | None) -> list[str]:
    if report_output is None:
        return []
    conflicts = report_output.get("conflicts")
    if not isinstance(conflicts, list):
        return []
    return [_clean(item) for item in conflicts if _clean(item)]


def _exhausted_from_receipts(receipts: list[Any]) -> list[str]:
    exhausted: list[str] = []
    for receipt in receipts:
        if _receipt_operation(receipt) != "acquire_research_sources":
            continue
        output = _receipt_output(receipt)
        task = output.get("task")
        if isinstance(task, dict) and _clean(task.get("query")):
            exhausted.append(_clean(task["query"]))
    return list(dict.fromkeys(exhausted))


def _executed_search_rounds(receipts: list[Any]) -> int:
    rounds: set[int] = set()
    for receipt in receipts:
        if _receipt_operation(receipt) != "acquire_research_sources":
            continue
        output = _receipt_output(receipt)
        if output.get("executed") is False:
            continue
        try:
            rounds.add(int(output.get("round_index") or 0))
        except (TypeError, ValueError):
            continue
    return len(rounds)


def _status(
    *,
    assessment: dict[str, Any] | None,
    report_status: str,
    fact_count: int,
) -> str:
    if report_status in {"verified", "uncertain_final", "partial_with_gaps"}:
        return report_status
    if report_status:
        return report_status
    if assessment is None:
        return "running"
    if assessment.get("satisfied") is True:
        return "satisfied"
    stop_reason = str(assessment.get("stop_reason") or "")
    if stop_reason in {"search_budget_exhausted", "previous_round_satisfied"}:
        return "search_budget_exhausted"
    if fact_count == 0:
        return "no_publishable_evidence"
    return "running"


__all__ = [
    "ConfirmedResearchFact",
    "EvolvingResearchReport",
    "project_evolving_report",
]
