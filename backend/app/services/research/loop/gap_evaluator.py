from __future__ import annotations

import re
from typing import Any

from app.services.research.evidence_quality import (
    assess_evidence_candidate,
    source_host,
)
from app.services.research.dedup import dedup_by_content
from app.services.research.candidate_quality import (
    catalog_book_title,
    candidate_topic_supported,
    is_book_catalog_url,
    normalize_candidate_title,
)
from app.services.research.loop.contracts import (
    ResearchGapAssessment,
    ResearchLoopBudget,
    ResearchRoundSources,
)
from app.services.research.source_acquisition import ResearchSourceRecord


_REQUIREMENT_GAPS = (
    "missing_recency_evidence",
    "missing_review_evidence",
    "missing_book_evidence",
)

_GAP_DESCRIPTIONS = {
    "missing_recency_evidence": "未找到带有明确发布日期、可用于判断“最新”的证据。",
    "missing_review_evidence": "未找到可核验评分或评论依据，无法判断“好评”。",
    "missing_book_evidence": "未找到可核验的具体图书信息。",
    "insufficient_independent_sources": "独立来源不足，尚不能完成交叉验证。",
    "no_publishable_evidence": "没有找到可进入研究报告的可核验证据。",
    "insufficient_evidence_quality": "现有候选只有低质量来源，需要机构、出版社或编辑来源补强。",
    "insufficient_recommendation_candidates": "已核验的推荐候选数量不足，需要继续验证同类图书。",
}

_QUALITY_RANK = {"unknown": 0, "low": 1, "medium": 2, "high": 3}


def evaluate_research_gaps(
    *,
    round_index: int,
    rounds: list[ResearchRoundSources | dict[str, Any]],
    budget: ResearchLoopBudget | dict[str, Any],
) -> ResearchGapAssessment:
    """Decide whether another search round is needed from visible evidence."""

    loop_budget = ResearchLoopBudget.model_validate(budget)
    sources = [
        ResearchRoundSources.model_validate(item)
        for item in rounds
    ]
    executed = [item for item in sources if item.executed]
    records = _unique_records(
        record
        for item in executed
        for record in item.source_records
    )
    reason_codes = list(
        dict.fromkeys(
            reason
            for item in executed
            for reason in item.rejection_reason_codes
        )
    )

    return evaluate_research_evidence_gaps(
        round_index=round_index,
        records=records,
        rejection_reason_codes=reason_codes,
        exhausted_queries=[
            item.task.query
            for item in executed
            if item.task.query
        ],
        budget=loop_budget,
        search_round_count=len(executed),
        objective=next(
            (
                item.task.objective
                for item in executed
                if item.task.objective
            ),
            "",
        ),
    )


def evaluate_research_evidence_gaps(
    *,
    round_index: int,
    records: list[ResearchSourceRecord | dict[str, Any]],
    rejection_reason_codes: list[str],
    exhausted_queries: list[str],
    budget: ResearchLoopBudget | dict[str, Any],
    search_round_count: int = 1,
    objective: str = "",
) -> ResearchGapAssessment:
    """Shared evidence-gap policy for canonical and compatibility runtimes."""

    loop_budget = ResearchLoopBudget.model_validate(budget)
    normalized_records = _unique_records(
        ResearchSourceRecord.model_validate(item)
        for item in records
    )
    assessments = [
        (
            record,
            assess_evidence_candidate(
                claim=record.claim,
                query=objective,
                source_title=record.source_title,
                source_url=record.source_url,
                published_date=_record_published_date(record),
            ),
        )
        for record in normalized_records
    ]
    structurally_publishable_records = [
        record
        for record, assessment in assessments
        if assessment.publishable
    ]
    required_quality = loop_budget.required_evidence_quality
    publishable_records = [
        record
        for record in structurally_publishable_records
        if _QUALITY_RANK.get(str(record.quality).lower(), 0)
        >= _QUALITY_RANK[required_quality]
    ]
    independent_records = dedup_by_content(
        publishable_records,
        text_of=_record_fingerprint_text,
    )
    independent_sources = {
        source_host(record.source_url)
        or record.source_title.strip().lower()
        for record in independent_records
        if source_host(record.source_url) or record.source_title.strip()
    }
    reason_codes = list(
        dict.fromkeys(
            [
                *rejection_reason_codes,
                *[
                    reason
                    for _record, assessment in assessments
                    for reason in assessment.reason_codes
                ],
            ]
        )
    )

    gaps: list[str] = []
    if not publishable_records:
        if structurally_publishable_records:
            gaps.append("insufficient_evidence_quality")
        else:
            gaps.extend(
                reason
                for reason in _REQUIREMENT_GAPS
                if reason in reason_codes
            )
        if not gaps:
            gaps.append("no_publishable_evidence")
    elif len(independent_sources) < loop_budget.min_independent_sources:
        gaps.append("insufficient_independent_sources")
    candidate_titles = _verified_recommendation_titles(
        publishable_records,
        expected=loop_budget.recommendation_candidate_titles,
        objective=objective,
    )
    if publishable_records and loop_budget.min_recommendation_candidates:
        if len(candidate_titles) < loop_budget.min_recommendation_candidates:
            gaps.append("insufficient_recommendation_candidates")

    satisfied = not gaps
    can_continue = bool(gaps) and round_index < loop_budget.max_search_rounds
    if satisfied:
        stop_reason = "evidence_satisfied"
    elif can_continue:
        stop_reason = "continue"
    else:
        stop_reason = "search_budget_exhausted"

    return ResearchGapAssessment(
        round_index=round_index,
        gaps=gaps,
        gap_descriptions=[
            _GAP_DESCRIPTIONS.get(gap, gap)
            for gap in gaps
        ],
        satisfied=satisfied,
        should_continue=can_continue,
        stop_reason=stop_reason,
        accepted_record_count=len(publishable_records),
        independent_source_count=len(independent_sources),
        exhausted_queries=list(dict.fromkeys(exhausted_queries)),
        next_actions=(
            ["plan_next_search"]
            if can_continue
            else ["build_research_report"]
        ),
        metadata={
            "search_round_count": search_round_count,
            "rejection_reason_codes": reason_codes,
            "candidate_record_count": len(normalized_records),
            "publishable_record_count": len(publishable_records),
            "structurally_publishable_record_count": len(
                structurally_publishable_records
            ),
            "required_evidence_quality": required_quality,
            "recommendation_candidate_count": len(candidate_titles),
            "max_search_rounds": loop_budget.max_search_rounds,
        },
    )


def _unique_records(records):
    unique = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (
            record.source_url.strip().lower(),
            record.claim.strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def _record_published_date(record: ResearchSourceRecord) -> str:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    return str(
        metadata.get("published_date")
        or metadata.get("published_at")
        or ""
    ).strip()


def _record_fingerprint_text(record: ResearchSourceRecord) -> str:
    return " ".join(
        part
        for part in (
            record.claim,
            record.excerpt,
        )
        if part
    )


def _verified_recommendation_titles(
    records: list[ResearchSourceRecord],
    *,
    expected: list[str],
    objective: str,
) -> set[str]:
    identities: dict[str, str] = {}
    for record in records:
        if not _is_book_entity_record(record):
            continue
        # Only the catalog page's own title is an entity identity. A synopsis
        # may mention comparison titles, sequels or references that do not own
        # this catalog URL and therefore must not inflate the stop count.
        source_title = catalog_book_title(
            record.source_title,
            source_url=record.source_url,
        )
        if key := normalize_candidate_title(source_title):
            identities[key] = source_title

    observed: set[str] = set()
    for key, title in identities.items():
        bound_texts: list[str] = []
        for record in records:
            quoted_keys = {
                normalize_candidate_title(value)
                for value in re.findall(
                    r"《([^》]{1,100})》",
                    " ".join([record.claim, record.excerpt]),
                )
            }
            source_key = normalize_candidate_title(
                catalog_book_title(
                    record.source_title,
                    source_url=record.source_url,
                )
            )
            if key not in quoted_keys and key != source_key:
                continue
            bound_texts.extend(
                [record.source_title, record.claim, record.excerpt]
            )
        if candidate_topic_supported(
            objective=objective,
            candidate_title=title,
            evidence_texts=bound_texts,
        ):
            observed.add(key)

    # `expected` is the discovery frontier, not an allow-list. Structured
    # catalog queries can legitimately discover additional exact entities in
    # the final round. Count every canonical, topic-supported catalog entity;
    # editorial pages and unrelated catalog records are already excluded above.
    return observed


def _book_title_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())


def _is_book_entity_record(record: ResearchSourceRecord) -> bool:
    return is_book_catalog_url(record.source_url)
