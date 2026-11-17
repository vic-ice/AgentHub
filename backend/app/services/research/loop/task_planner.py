from __future__ import annotations

from typing import Any

from app.services.research.loop.contracts import (
    ResearchGapAssessment,
    ResearchLoopBudget,
    ResearchSearchTask,
)
from app.services.research.search_policy import build_research_search_request
from app.services.research.loop.loop_detector import QueryLoopDetector


_GAP_QUERY_TERMS = {
    "missing_recency_evidence": "出版日期 新书 发布 今年",
    "missing_review_evidence": "评分 书评 读者评论 高分榜单",
    "missing_book_evidence": "图书 作者 出版社 书目",
    "insufficient_independent_sources": "独立书评 出版社 编辑推荐",
    "no_publishable_evidence": "权威来源 详细介绍",
}


def plan_research_search_task(
    *,
    objective: str,
    round_index: int,
    budget: ResearchLoopBudget | dict[str, Any],
    constraints: list[dict[str, Any]] | None = None,
    previous_assessment: ResearchGapAssessment | dict[str, Any] | None = None,
) -> ResearchSearchTask:
    """Create one provider-neutral search task from the current evidence gap."""

    loop_budget = ResearchLoopBudget.model_validate(budget)
    base = build_research_search_request(
        objective,
        constraints=constraints or [],
    )
    previous = (
        ResearchGapAssessment.model_validate(previous_assessment)
        if previous_assessment is not None
        else None
    )

    if round_index > loop_budget.max_search_rounds:
        raise ValueError("research round exceeds the configured search budget")

    if previous is not None and not previous.should_continue:
        return ResearchSearchTask(
            round_index=round_index,
            objective=base.objective,
            purpose="no_op",
            query=base.query,
            should_search=False,
            target_gaps=list(previous.gaps),
            exhausted_queries=list(previous.exhausted_queries),
            max_results=loop_budget.max_results_per_round,
            detail=base.detail,
            time_range=base.time_range,
            include_domains=base.include_domains,
            exclude_domains=base.exclude_domains,
            include_url_prefixes=base.include_url_prefixes,
            language=base.language,
            category=base.category,
            metadata={"stop_reason": previous.stop_reason},
        )

    target_gaps = list(previous.gaps) if previous is not None else []
    candidate_query = _unique_query(
        f"{base.query} {_gap_suffix(target_gaps)}"
    )
    exhausted = (
        list(previous.exhausted_queries)
        if previous is not None
        else []
    )
    detector = QueryLoopDetector(
        history=exhausted,
        exclude_prefix=base.query,
    )
    loop_check = detector.check(candidate_query)
    if loop_check["is_loop"] or candidate_query in exhausted:
        unused_gaps = [
            gap
            for gap in target_gaps
            if _gap_term_used(gap, exhausted) is False
        ]
        if unused_gaps:
            query = _unique_query(
                f"{base.query} {_gap_suffix(unused_gaps)}"
            )
            should_search = True
            purpose = "gap_repair"
            loop_resolution = "unused_gap_suffix"
            hint = loop_check.get("hint") or ""
        else:
            return ResearchSearchTask(
                round_index=round_index,
                objective=base.objective,
                purpose="loop_detected",
                query=candidate_query,
                should_search=False,
                target_gaps=target_gaps,
                exhausted_queries=exhausted,
                max_results=loop_budget.max_results_per_round,
                detail=base.detail,
                time_range=base.time_range,
                include_domains=base.include_domains,
                exclude_domains=base.exclude_domains,
                include_url_prefixes=base.include_url_prefixes,
                language=base.language,
                category=base.category,
                metadata={
                    "requirements": base.requirements,
                    "loop_detected": True,
                    "loop_hint": loop_check.get("hint") or "",
                    "matched_query": loop_check.get("matched_query") or "",
                    "stop_reason": "query_loop_detected",
                },
            )
    else:
        query = candidate_query
        should_search = True
        purpose = "initial_evidence" if round_index == 1 else "gap_repair"
        loop_resolution = ""
        hint = ""
    return ResearchSearchTask(
        round_index=round_index,
        objective=base.objective,
        purpose=purpose,
        query=query,
        should_search=should_search,
        target_gaps=target_gaps,
        exhausted_queries=exhausted,
        max_results=loop_budget.max_results_per_round,
        detail=base.detail,
        time_range=base.time_range,
        include_domains=base.include_domains,
        exclude_domains=base.exclude_domains,
        include_url_prefixes=base.include_url_prefixes,
        language=base.language,
        category=base.category,
        metadata={
            "requirements": base.requirements,
            "query_changed": bool(
                previous is not None and query != base.query
            ),
            "loop_detected": bool(loop_check.get("is_loop")),
            "loop_resolution": loop_resolution,
            "loop_hint": hint,
        },
    )


def _unique_query(value: str) -> str:
    return " ".join(dict.fromkeys(str(value or "").split()))[:300].strip()


def _gap_suffix(gaps: list[str]) -> str:
    return " ".join(
        _GAP_QUERY_TERMS[gap]
        for gap in gaps
        if gap in _GAP_QUERY_TERMS
    )


def _gap_term_used(gap: str, exhausted_queries: list[str]) -> bool:
    term = _GAP_QUERY_TERMS.get(gap)
    if not term:
        return False
    return any(term in query for query in exhausted_queries)
