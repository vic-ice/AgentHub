from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.chat import UserInput
from app.services.agent_core.contracts import PublishedAnswer
from app.services.external_search.contracts import (
    SearchAttempt,
    SearchHit,
    SearchRequest,
    SearchResult,
)
from app.services.research.contracts import ResearchEvidence
from app.services.research.candidate_quality import (
    catalog_book_title,
    candidate_topic_supported,
    normalize_candidate_title,
    objective_topic_anchors,
)
from app.services.research.evidence_quality import (
    assess_evidence_candidate,
    classify_source,
    source_quality,
)
from app.services.research.loop import (
    ResearchGapAssessment,
    ResearchLoopBudget,
    ResearchRoundSources,
    ResearchSearchTask,
    evaluate_research_gaps,
    plan_research_search_task,
)
from app.services.research.source_extraction import (
    extract_research_source_records,
)
from app.services.research.source_visit import fetch_research_source_document
from app.services.execution_progress import (
    current_execution_progress,
    report_completed_step,
)
from app.services.user_answer_contracts import build_user_answer_brief
from app.services.evidence_strategy import EvidenceStrategy


DEEP_RESEARCH_RECEIPT_VERSION = "deep-research-receipt-v1"
DEEP_RESEARCH_TOTAL_BUDGET_SECONDS = 360.0
DEEP_RESEARCH_PUBLICATION_RESERVE_SECONDS = 110.0

_REVIEW_META_QUERY_RE = re.compile(
    r"(?:已核验|尚未核验|数量不足|证据不足|资料不足|继续验证|继续检索|"
    r"需要补充|仍需补充|关键缺口|同类图书)"
)
_NON_ENTITY_SEARCH_TITLE_RE = re.compile(
    r"(?:推荐|书单|盘点|必读|清单|指南|教程|课程|论文|资源|资料|"
    r"学习路径|文末赠书|如何|什么|哪些|\.\.\.|…|"
    r"best\s+\d+|top\s+\d+|\d+\s*(?:本|books?)|"
    r"books?\s+(?:for|to|about))",
    re.IGNORECASE,
)
_SEARCH_SITE_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—|_]\s*)?(?:wikipedia|维基百科|百度百科|知乎|"
    r"豆瓣读书|goodreads|amazon(?:\.\w+)?|github)\s*$",
    re.IGNORECASE,
)

FAILED = ("\u672a\u80fd\u5b8c\u6210\u6df1\u5ea6\u7814\u7a76\uff0c"
          "\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")


class DeepResearchReceipt(BaseModel):
    """Compact result of one app-owned multi-round research run."""

    result_mode: str = "deep_research_receipt"
    contract_version: str = DEEP_RESEARCH_RECEIPT_VERSION
    run_id: UUID
    objective: str
    status: str = "completed"
    content: str = ""
    conclusion: str = ""
    evidence_count: int = 0
    source_count: int = 0
    quality_status: str = "insufficient"
    created_at: str = ""

    def to_published_answer(self) -> PublishedAnswer:
        """Publish the complete user-facing answer as the chat message body.

        The full report is stored and rendered in the conversation; the
        Request Builder later swaps it for a compact pointer so the model
        never carries the whole report.
        """

        conclusion = " ".join(str(self.conclusion or "").split())
        return PublishedAnswer(
            status="completed" if self.status == "completed" else "failed",
            content=self.content or self.conclusion or self.objective,
            receipt_backed=True,
            receipt_refs=[f"research-run:{self.run_id}"],
            publication_mode="deep_research",
            custom_data={
                "research_run_id": str(self.run_id),
                "research_objective": self.objective,
                "research_status": self.status,
                "research_conclusion": conclusion[:300],
                "research_evidence_count": self.evidence_count,
                "research_quality_status": self.quality_status,
                **(
                    {"failure_code": "research_evidence_insufficient"}
                    if self.status != "completed"
                    else {}
                ),
            },
        )


async def run_deep_research_turn(
    user_input: UserInput,
) -> PublishedAnswer:
    """Run the Deep Research runtime and return a compact chat receipt."""

    requested_model = user_input.model_uuid or user_input.model_name
    model_name = ""
    if requested_model:
        from app.infra.llm import resolve_model_name
        from app.infra.llm.resolver import refresh_model_cache_if_missing

        model_name = resolve_model_name(requested_model) or ""
        await refresh_model_cache_if_missing(model_name)
    receipt = await run_deep_research(
        user_id=user_input.user_id,
        thread_id=user_input.thread_id,
        objective=user_input.content,
        model_name=model_name,
        thinking_mode=bool(user_input.thinking_mode),
    )
    return receipt.to_published_answer()


def _plan_next_task(
    *,
    objective: str,
    round_index: int,
    budget,
    previous_assessment: ResearchGapAssessment | None,
    pending_subquestions: list[str],
    used_queries: set[str],
    candidate_hints: dict[str, str] | None = None,
    catalog_queries: list[str] | None = None,
    prefer_catalog_batch: bool = False,
) -> ResearchSearchTask | None:
    """Pop the next reviewer subquestion, falling back to the planner.

    Reviewer gaps become executable next-round searches; deterministic
    planning stays the fallback when the queue is empty or exhausted.
    """

    from app.services.research.search_policy import (
        build_research_search_request,
    )

    base = build_research_search_request(objective)
    known_hints = candidate_hints or {}
    trusted_catalog_queries = list(
        dict.fromkeys(
            " ".join(str(item or "").split()).strip()[:80]
            for item in catalog_queries or []
            if " ".join(str(item or "").split()).strip()
        )
    )[:6]
    if (
        prefer_catalog_batch
        and "book_recommendation" in base.requirements
        and (known_hints or trusted_catalog_queries)
    ):
        # Verify a wider pool than the publication minimum. Catalog misses and
        # edition ambiguity are normal, so a four-title input cannot reliably
        # produce four verified books.
        candidate_titles = list(known_hints)[:8]
        quoted = " ".join(f"《{title}》" for title in candidate_titles)
        query = " ".join(
            part
            for part in (
                quoted,
                " ".join(trusted_catalog_queries),
                "作者 出版社 内容简介 豆瓣",
            )
            if part
        )[:300]
        return ResearchSearchTask(
            round_index=round_index,
            objective=objective,
            purpose="catalog_candidate_verification",
            query=query,
            should_search=True,
            target_gaps=["insufficient_recommendation_candidates"],
            exhausted_queries=sorted(used_queries)[-8:],
            max_results=budget.max_results_per_round,
            detail="standard",
            include_domains=["book.douban.com"],
            exclude_domains=base.exclude_domains,
            include_url_prefixes=["https://book.douban.com/subject/"],
            language="zh",
            category=base.category,
            metadata={
                "source": "structured_catalog_candidate_batch",
                "candidate_catalog_batch": True,
                "candidate_titles": candidate_titles,
                "candidate_search_hints": {
                    title: str(known_hints.get(title) or title)
                    for title in candidate_titles
                },
                "catalog_discovery_queries": trusted_catalog_queries,
            },
        )

    while pending_subquestions:
        candidate = pending_subquestions.pop(0)
        normalized = _normalize_query(candidate)
        if not normalized or normalized in used_queries:
            continue
        discovered_titles = re.findall(r"《([^》]{1,100})》", candidate)
        candidate_titles = [
            title for title in discovered_titles if title in known_hints
        ]
        candidate_verification = bool(
            candidate_titles and "book_recommendation" in base.requirements
        )
        executable_query = _compile_reviewer_query(
            objective=objective,
            candidate=candidate,
            base_query=base.query,
            round_index=round_index,
            is_book_recommendation="book_recommendation" in base.requirements,
        )
        return ResearchSearchTask(
            round_index=round_index,
            objective=objective,
            purpose="reviewer_gap",
            query=(
                "site:book.douban.com/subject/ " + candidate
                if candidate_verification
                else executable_query
            )[:300],
            should_search=True,
            target_gaps=[],
            exhausted_queries=sorted(used_queries)[-8:],
            max_results=budget.max_results_per_round,
            detail=base.detail,
            time_range=base.time_range,
            include_domains=(
                ["book.douban.com"]
                if candidate_verification
                else base.include_domains
            ),
            exclude_domains=base.exclude_domains,
            include_url_prefixes=(
                ["https://book.douban.com/subject/"]
                if candidate_verification
                else base.include_url_prefixes
            ),
            language=base.language,
            category=base.category,
            metadata={
                "source": (
                    "objective_plan_candidate_verification"
                    if candidate_verification
                    else "reviewer_subquestion"
                ),
                "candidate_titles": candidate_titles,
                "candidate_search_hints": {
                    title: str((candidate_hints or {}).get(title) or title)
                    for title in candidate_titles
                },
            },
        )
    return plan_research_search_task(
        objective=objective,
        round_index=round_index,
        budget=budget,
        previous_assessment=previous_assessment,
    )


def _enqueue_subquestions(
    pending: list[str],
    next_subquestions: list[str] | None,
    *,
    used_queries: set[str],
) -> list[str]:
    """Append bounded, de-duplicated reviewer subquestions to the queue."""

    result = list(pending)
    seen = {_normalize_query(item) for item in result}
    for item in next_subquestions or []:
        normalized = _normalize_query(item)
        if (
            not normalized
            or normalized in seen
            or normalized in used_queries
        ):
            continue
        seen.add(normalized)
        result.append(normalized[:300])
        if len(result) >= 6:
            break
    return result


def _catalog_companion_tasks(
    *,
    objective: str,
    round_index: int,
    budget,
    evidence_strategy: EvidenceStrategy,
    candidate_titles: list[str],
    used_queries: set[str],
) -> list[ResearchSearchTask]:
    """Run evidence questions beside entity verification, not after it.

    Catalog identity and decision evidence are independent branches. Spending
    separate logical rounds on them makes a fixed safety budget behave like a
    fixed research recipe and leaves no room for reviewer-directed follow-up.
    """

    from app.services.research.search_policy import build_research_search_request

    base = build_research_search_request(objective)
    tasks: list[ResearchSearchTask] = []
    facets = evidence_strategy.active_facets(limit=3)
    planned: list[tuple[Any, list[str]]] = []
    for facet_index, facet in enumerate(facets):
        if facet.candidate_specific and candidate_titles:
            # Compile a rich evidence strategy into a short retrieval query.
            # Search engines handle one or two entities far more reliably than
            # four titles plus a prose purpose and every possible source role.
            start = (
                (max(1, round_index) - 1) * 2 + facet_index * 2
            ) % len(candidate_titles)
            title_batch = [
                candidate_titles[(start + offset) % len(candidate_titles)]
                for offset in range(min(2, len(candidate_titles)))
            ]
        else:
            title_batch = []
        planned.append((facet, title_batch))
    # Use the optional fourth branch for the highest-priority facet's next
    # candidate window instead of starving later facets behind facet one.
    if facets and candidate_titles and facets[0].candidate_specific:
        start = (max(1, round_index) * 2) % len(candidate_titles)
        extra_batch = [
            candidate_titles[(start + offset) % len(candidate_titles)]
            for offset in range(min(2, len(candidate_titles)))
        ]
        if extra_batch != planned[0][1]:
            planned.append((facets[0], extra_batch))

    for facet, title_batch in planned[:4]:
            search_terms = [
                *facet.query_terms[:2],
                *facet.preferred_source_types[:1],
            ]
            subject_anchor = (
                " ".join(f"《{title}》" for title in title_batch)
                if title_batch
                else " ".join(objective.split())[:80]
            )
            clean_query = " ".join(
                item
                for item in [
                    subject_anchor,
                    " ".join(search_terms) or facet.name,
                ]
                if item
            )[:180]
            normalized = _normalize_query(clean_query)
            if not normalized or normalized in used_queries:
                continue
            tasks.append(
                ResearchSearchTask(
                    round_index=round_index,
                    objective=objective,
                    purpose="evidence_strategy_facet",
                    query=clean_query,
                    should_search=True,
                    target_gaps=[facet.name],
                    exhausted_queries=sorted(used_queries)[-8:],
                    max_results=budget.max_results_per_round,
                    detail=base.detail,
                    time_range=base.time_range,
                    include_domains=base.include_domains,
                    exclude_domains=base.exclude_domains,
                    include_url_prefixes=base.include_url_prefixes,
                    language=base.language,
                    category=base.category,
                    metadata={
                        "source": "objective_plan_evidence_strategy",
                        "evidence_facet": facet.name,
                        "evidence_purpose": facet.purpose,
                        "source_roles": list(facet.preferred_source_types),
                        "evidence_candidate_titles": title_batch,
                        "facet_required": facet.required_for_recommendation,
                    },
                )
            )
            if len(tasks) >= 4:
                return tasks
    return tasks


def _merge_parallel_round_sources(
    branches: list[ResearchRoundSources],
) -> ResearchRoundSources:
    """Merge bounded parallel search branches into one logical round."""

    if not branches:
        raise ValueError("parallel research round produced no branch result")
    primary = branches[0]
    records = []
    seen_records: set[tuple[str, str]] = set()
    for branch in branches:
        for record in branch.source_records:
            key = (
                str(record.source_url or "").strip().casefold(),
                " ".join(str(record.claim or "").split()).casefold(),
            )
            if key in seen_records:
                continue
            seen_records.add(key)
            records.append(record)
    providers = list(
        dict.fromkeys(
            str(branch.provider or "").strip()
            for branch in branches
            if str(branch.provider or "").strip()
        )
    )
    errors = list(
        dict.fromkeys(
            str(branch.error or "").strip()
            for branch in branches
            if str(branch.error or "").strip()
        )
    )
    rejection_codes = list(
        dict.fromkeys(
            code
            for branch in branches
            for code in branch.rejection_reason_codes
        )
    )
    return primary.model_copy(
        update={
            "status": (
                "completed"
                if records
                else "empty_result"
                if all(branch.status == "empty_result" for branch in branches)
                else "failed"
            ),
            "provider": ",".join(providers),
            "provider_status": (
                "found" if records else primary.provider_status
            ),
            "error": "; ".join(errors)[:500],
            "source_records": records,
            "rejected_documents": [
                item
                for branch in branches
                for item in branch.rejected_documents
            ],
            "source_count": sum(branch.source_count for branch in branches),
            "publishable_source_count": len(records),
            "rejection_reason_codes": rejection_codes,
            "metadata": {
                **primary.metadata,
                "parallel_branch_count": len(branches),
                "parallel_branch_purposes": [
                    branch.task.purpose for branch in branches
                ],
            },
        }
    )


def _unverified_candidate_hints(
    candidate_hints: dict[str, str],
    records: list[Any],
) -> dict[str, str]:
    """Return candidate hypotheses that still lack their own catalog entity."""

    verified_keys = {
        normalize_candidate_title(
            catalog_book_title(
                getattr(record, "source_title", ""),
                source_url=getattr(record, "source_url", ""),
            )
        )
        for record in records
    }
    verified_keys.discard("")
    return {
        title: hint
        for title, hint in candidate_hints.items()
        if normalize_candidate_title(title) not in verified_keys
    }


def _normalize_query(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _compile_reviewer_query(
    *,
    objective: str,
    candidate: str,
    base_query: str,
    round_index: int,
    is_book_recommendation: bool,
) -> str:
    """Turn reviewer prose into an executable, diversified search query."""

    normalized = " ".join(str(candidate or "").split()).strip()
    if not is_book_recommendation or not _REVIEW_META_QUERY_RE.search(normalized):
        return normalized.casefold()
    focus = (
        "经典教材 专业书单 入门 进阶 作者 出版社"
        if round_index <= 2
        else "理论 实战 适合人群 学习路线 对比 课程参考书"
    )
    if "深度学习" in objective:
        focus += " 神经网络 PyTorch"
    # The base query already carries the immutable subject. Drop the internal
    # review sentence entirely: it describes system state, not user intent.
    return " ".join(dict.fromkeys(f"{base_query} {focus}".split()))[:300]


def _review_leads(rounds: list[ResearchRoundSources]) -> list[dict[str, Any]]:
    """Expose bounded discovery leads to Reviewer without promoting them to facts."""

    leads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for round_sources in rounds:
        for source in round_sources.source_records:
            title = " ".join(str(source.source_title or "").split()).strip()
            claim = " ".join(str(source.claim or source.excerpt or "").split()).strip()
            url = str(source.source_url or "").strip()
            identity = (url or f"{title}|{claim}").casefold()
            if not identity or identity in seen or not (title or claim):
                continue
            seen.add(identity)
            leads.append(
                {
                    "source_title": title[:240],
                    "claim": claim[:600],
                    "source_url": url[:1000],
                    "quality": str(source.quality or "unknown"),
                    "status": "discovery_lead",
                }
            )
    return leads[-12:]


def _discovered_candidate_titles(
    round_sources: ResearchRoundSources,
    *,
    excluded_titles: list[str],
    objective: str,
    themes: list[str] | None = None,
) -> list[str]:
    """Extract retrieval-backed candidates with source-owned topic support."""

    return list(
        _discovered_candidate_hints(
            round_sources,
            excluded_titles=excluded_titles,
            objective=objective,
            themes=themes,
        )
    )


def _discovered_candidate_hints(
    round_sources: ResearchRoundSources,
    *,
    excluded_titles: list[str],
    objective: str,
    themes: list[str] | None = None,
) -> dict[str, str]:
    """Return exact candidates with source-owned author hints when present."""

    excluded = {_normalize_query(title) for title in excluded_titles}
    hints: dict[str, str] = {}
    for source in round_sources.source_records:
        values = re.findall(
            r"《([^》]{1,100})》",
            " ".join([str(source.claim or ""), str(source.excerpt or "")]),
        )
        # Search engines often truncate the result title after an opening book
        # bracket. Recover that embedded entity instead of treating the whole
        # editorial headline as a book title.
        values.extend(
            re.findall(
                r"《([^》|\r\n]{2,100})(?:》|$)",
                str(source.source_title or "").strip(),
            )
        )
        if "book.douban.com/subject/" in str(source.source_url or ""):
            source_title = re.sub(
                r"\s*(?:[（(]\s*豆瓣\s*[）)]|-\s*豆瓣读书)\s*$",
                "",
                str(source.source_title or "").strip(),
                flags=re.IGNORECASE,
            )
            if source_title:
                values.append(source_title)
        else:
            source_title = _search_result_title_candidate(source.source_title)
            if source_title:
                values.append(source_title)
        for value in values:
            title = " ".join(str(value or "").split()).strip("《》 ")
            key = _normalize_query(title)
            if not title or key in excluded or key in {
                _normalize_query(item) for item in hints
            }:
                continue
            if not candidate_topic_supported(
                objective=objective,
                themes=themes or [],
                candidate_title=title,
                evidence_texts=[
                    str(source.source_title or ""),
                    str(source.claim or ""),
                    str(source.excerpt or ""),
                ],
            ):
                continue
            clean_title = title[:100]
            hints[clean_title] = _candidate_source_hint(
                title=clean_title,
                source_text=" ".join(
                    [str(source.claim or ""), str(source.excerpt or "")]
                ),
            )
            if len(hints) >= 8:
                return hints
    return hints


def _search_result_title_candidate(value: object) -> str:
    """Keep title-shaped discovery leads; final catalog lookup owns identity."""

    title = " ".join(str(value or "").split()).strip(" 《》")
    title = _SEARCH_SITE_SUFFIX_RE.sub("", title).strip(" -–—|_")
    if (
        not title
        or len(title) > 100
        or len(title) < 2
        or "《" in title
        or "》" in title
        or _NON_ENTITY_SEARCH_TITLE_RE.search(title)
    ):
        return ""
    return title


def _candidate_source_hint(*, title: str, source_text: str) -> str:
    text = " ".join(str(source_text or "").split())
    author = ""
    for pattern in (
        r"(?:作者|著者)\s*(?:为|是|[:：])\s*([^，,。；;\n]{2,80})",
        r"\bby\s+([A-Z][A-Za-z .'-]{2,80})",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            author = " ".join(match.group(1).split()).strip()
            break
    return f"{title} {author}".strip()[:220]


def _candidate_discovery_enabled(task: ResearchSearchTask) -> bool:
    """Verification results may confirm candidates, never create new ones."""

    return bool(task.metadata.get("catalog_discovery_queries")) or not bool(
        task.metadata.get("candidate_titles")
    )


def _filter_recommendation_candidate_records(
    round_sources: ResearchRoundSources,
    *,
    objective: str,
    themes: list[str] | None = None,
) -> ResearchRoundSources:
    """Drop catalog/book identities that have no source-owned topic evidence."""

    # A catalog identity row usually contains only title/author/year, while the
    # synopsis extracted from the same subject page is stored as a separate
    # record.  Evaluate those records as one source: otherwise a relevant book
    # loses its identity row merely because the title itself lacks a topic word.
    topic_supported_urls = {
        str(source.source_url or "").strip().casefold()
        for source in round_sources.source_records
        if str(source.source_url or "").strip()
        and candidate_topic_supported(
            objective=objective,
            themes=themes or [],
            evidence_texts=[
                str(source.source_title or ""),
                str(source.claim or ""),
                str(source.excerpt or ""),
            ],
        )
    }
    kept = []
    dropped = 0
    for source in round_sources.source_records:
        candidate_titles = re.findall(
            r"《([^》]{1,100})》",
            " ".join([str(source.claim or ""), str(source.excerpt or "")]),
        )
        if "book.douban.com/subject/" in str(source.source_url or ""):
            source_title = re.sub(
                r"\s*(?:[（(]\s*豆瓣\s*[）)]|-\s*豆瓣读书)\s*$",
                "",
                str(source.source_title or "").strip(),
                flags=re.IGNORECASE,
            )
            if source_title:
                candidate_titles.append(source_title)
        source_url_key = str(source.source_url or "").strip().casefold()
        locally_supported = any(
            candidate_topic_supported(
                objective=objective,
                themes=themes or [],
                candidate_title=title,
                evidence_texts=[
                    str(source.source_title or ""),
                    str(source.claim or ""),
                    str(source.excerpt or ""),
                ],
            )
            for title in candidate_titles
        )
        if (
            candidate_titles
            and not locally_supported
            and source_url_key not in topic_supported_urls
        ):
            dropped += 1
            continue
        kept.append(source)
    if not dropped:
        return round_sources
    return round_sources.model_copy(
        update={
            "source_records": kept,
            "publishable_source_count": len(kept),
            "rejection_reason_codes": list(
                dict.fromkeys(
                    [
                        *round_sources.rejection_reason_codes,
                        "candidate_topic_mismatch",
                    ]
                )
            ),
            "metadata": {
                **round_sources.metadata,
                "candidate_topic_mismatch_count": dropped,
            },
        }
    )


async def run_deep_research(
    *,
    user_id: UUID,
    thread_id: UUID | None,
    objective: str,
    model_name: str = "",
    thinking_mode: bool = False,
) -> DeepResearchReceipt:
    """Execute bounded multi-round research to completion.

    The runner owns only the multi-round loop. Searching, quality gating,
    claim extraction, evidence admission and report building all reuse the
    shared research primitives.
    """

    from app.services.external_search import get_search_gateway
    from app.services.research.content_quality_gate import (
        check_content_garbage,
    )
    from app.services.research.orchestrator import (
        get_research_orchestrator,
    )
    from app.services.research.report import build_research_report
    from app.services.research.objective_planner import (
        candidate_search_hints,
        candidate_verification_queries,
        fallback_research_objective_plan,
        plan_research_objective,
    )
    from app.services.research.search_policy import (
        build_research_search_request,
        requested_book_count,
    )

    event_loop = asyncio.get_running_loop()
    run_started_at = event_loop.time()
    hard_deadline = run_started_at + DEEP_RESEARCH_TOTAL_BUDGET_SECONDS
    acquisition_deadline = (
        hard_deadline - DEEP_RESEARCH_PUBLICATION_RESERVE_SECONDS
    )
    objective_search_request = build_research_search_request(objective)
    book_recommendation_required = bool(
        "book_recommendation" in objective_search_request.requirements
    )
    requested_candidate_count = requested_book_count(objective)
    minimum_candidate_count = (
        max(4, min(requested_candidate_count, 10))
        if book_recommendation_required
        else 0
    )
    objective_plan = fallback_research_objective_plan(objective)
    budget = ResearchLoopBudget(
        max_search_rounds=4,
        max_results_per_round=max(8, minimum_candidate_count),
        max_records_per_round=(
            12 if book_recommendation_required else 8
        ),
        min_independent_sources=2,
        required_evidence_quality="medium",
        min_recommendation_candidates=(
            minimum_candidate_count
        ),
        recommendation_candidate_titles=[],
    )
    orchestrator = get_research_orchestrator()
    started = await orchestrator.start_research(
        user_id=user_id,
        objective=objective,
        thread_id=thread_id,
        mode="deep_research",
        subquestions=[objective],
        next_actions=[
            "plan_search",
            "acquire_sources",
            "evaluate_gaps",
            "build_report",
        ],
        budget=budget.model_dump(mode="json"),
        stop_criteria=["evidence_satisfied", "search_budget_exhausted"],
        metadata={
            "capture_source": "deep_research_toggle",
            "objective_plan": objective_plan.model_dump(mode="json"),
                "semantic_plan_pending": True,
        },
    )
    run_id = started.run.id
    if run_id is None:
        raise RuntimeError("deep research run was not created")
    await report_completed_step(
        kind="research",
        status="completed",
        title="\u7814\u7a76\u4efb\u52a1\u5df2\u521b\u5efa",
        detail="\u5df2\u5efa\u7acb\u7814\u7a76\u7a7a\u95f4\u5e76\u786e\u8ba4\u68c0\u7d22\u9884\u7b97",
        step_id=f"research:{run_id}:created",
    )
    # The semantic planner enriches later frontier slots.  It starts only after
    # the durable run exists, so startup failures cannot leak an orphan task.
    planner_task = asyncio.create_task(
        plan_research_objective(objective, model_id=model_name)
    )


    previous_assessment: ResearchGapAssessment | None = None
    rounds: list[ResearchRoundSources] = []
    # The immutable goal seeds a broad first query.  The semantic plan contributes
    # only a bounded initial frontier; evidence and Reviewer gaps may grow it.
    broad_discovery_query = objective_search_request.query
    pending_subquestions: list[str] = [broad_discovery_query]
    used_queries: set[str] = set()
    candidate_hints: dict[str, str] = {}
    queued_candidate_keys: set[str] = set()
    catalog_candidate_round_completed = False
    catalog_retry_pending = False
    last_review = None
    previous_accepted_record_count = 0
    stagnant_batches = 0
    deadline_exhausted = False
    deadline_stop_reason = ""
    try:
        for round_index in range(1, budget.max_search_rounds + 1):
            remaining_acquisition = acquisition_deadline - event_loop.time()
            if remaining_acquisition <= 0:
                deadline_exhausted = True
                deadline_stop_reason = "wall_clock_budget_exhausted"
                break
            catalog_verification_ready = bool(
                book_recommendation_required
                and (
                    not catalog_candidate_round_completed
                    or catalog_retry_pending
                )
                and candidate_hints
                and (
                    len(candidate_hints) >= budget.min_recommendation_candidates
                    or round_index >= 3
                    or not pending_subquestions
                )
            )
            task = _plan_next_task(
                objective=objective,
                round_index=round_index,
                budget=budget,
                previous_assessment=previous_assessment,
                pending_subquestions=pending_subquestions,
                used_queries=used_queries,
                candidate_hints=candidate_hints,
                catalog_queries=objective_topic_anchors(
                    objective,
                    themes=objective_plan.themes,
                ),
                # Verify exact entities as soon as discovery has a useful
                # frontier. The round limit is only a safety bound.
                prefer_catalog_batch=catalog_verification_ready,
            )
            if task is None or not task.should_search:
                break
            if task.metadata.get("candidate_catalog_batch"):
                catalog_candidate_round_completed = True
                catalog_retry_pending = False
            round_tasks = [task]
            if task.metadata.get("candidate_catalog_batch"):
                round_tasks.extend(
                    _catalog_companion_tasks(
                        objective=objective,
                        round_index=round_index,
                        budget=budget,
                        evidence_strategy=objective_plan.evidence_strategy,
                        candidate_titles=list(candidate_hints),
                        used_queries=used_queries,
                    )
                )
            round_query_keys = {
                _normalize_query(item.query) for item in round_tasks
            }
            used_queries.update(round_query_keys)
            pending_subquestions = [
                item
                for item in pending_subquestions
                if _normalize_query(item) not in round_query_keys
            ]
            try:
                async with asyncio.timeout(remaining_acquisition):
                    branch_results = await asyncio.gather(
                        *(
                            _search_round(
                                orchestrator=orchestrator,
                                user_id=user_id,
                                run_id=run_id,
                                task=round_task,
                                get_search_gateway=get_search_gateway,
                                check_content_garbage=check_content_garbage,
                                max_records_per_round=budget.max_records_per_round,
                            )
                            for round_task in round_tasks
                        )
                    )
                    round_sources = _merge_parallel_round_sources(
                        list(branch_results)
                    )
            except TimeoutError:
                deadline_exhausted = True
                deadline_stop_reason = "wall_clock_budget_exhausted"
                break
            if round_index == 1:
                remaining_acquisition = acquisition_deadline - event_loop.time()
                if planner_task.done():
                    objective_plan = await planner_task
                elif remaining_acquisition > 0:
                    try:
                        async with asyncio.timeout(remaining_acquisition):
                            objective_plan = await planner_task
                    except TimeoutError:
                        deadline_exhausted = True
                        deadline_stop_reason = "wall_clock_budget_exhausted"
                else:
                    deadline_exhausted = True
                    deadline_stop_reason = "wall_clock_budget_exhausted"
                    planner_task.cancel()
                budget = budget.model_copy(
                    update={
                        "min_recommendation_candidates": (
                            minimum_candidate_count
                            if book_recommendation_required
                            else 0
                        )
                    }
                )
                if (
                    book_recommendation_required
                    and objective_plan.task_type != "book_recommendation"
                ):
                    objective_plan = objective_plan.model_copy(
                        update={
                            "task_type": "book_recommendation",
                            "subject_type": "books",
                        }
                    )
                if book_recommendation_required:
                    planned_hints = candidate_search_hints(objective_plan)
                    for title, hint in planned_hints.items():
                        key = _normalize_query(title)
                        if not key or key in queued_candidate_keys:
                            continue
                        candidate_hints[title] = hint
                        queued_candidate_keys.add(key)
                    budget = budget.model_copy(
                        update={
                            "recommendation_candidate_titles": list(
                                candidate_hints
                            )[:10]
                        }
                    )
                pending_subquestions = _enqueue_subquestions(
                    pending_subquestions,
                    [
                        *objective_plan.search_queries,
                        *candidate_verification_queries(objective_plan),
                    ],
                    used_queries=used_queries,
                )
                await orchestrator.update_research_state(
                    user_id=user_id,
                    run_id=run_id,
                    budget=budget.model_dump(mode="json"),
                    metadata={
                        "objective_plan": objective_plan.model_dump(mode="json"),
                        "semantic_plan_pending": False,
                    },
                )
            if book_recommendation_required:
                round_sources = _filter_recommendation_candidate_records(
                    round_sources,
                    objective=objective,
                    themes=objective_plan.themes,
                )
            rounds.append(round_sources)
            newly_discovered_hints = (
                _discovered_candidate_hints(
                    round_sources,
                    excluded_titles=objective_plan.seed_entities,
                    objective=objective,
                    themes=objective_plan.themes,
                )
                if _candidate_discovery_enabled(task)
                else {}
            )
            unqueued = [
                title
                for title in newly_discovered_hints
                if _normalize_query(title) not in queued_candidate_keys
            ][:8]
            if unqueued and book_recommendation_required:
                for title in unqueued:
                    candidate_hints[title] = newly_discovered_hints[title]
                    queued_candidate_keys.add(_normalize_query(title))
                budget = budget.model_copy(
                    update={
                        "recommendation_candidate_titles": list(
                            candidate_hints
                        )[:10]
                    }
                )
            assessment = evaluate_research_gaps(
                round_index=round_index,
                rounds=rounds,
                budget=budget,
            )
            previous_assessment = assessment
            if (
                task.metadata.get("candidate_catalog_batch")
                and "insufficient_recommendation_candidates" in assessment.gaps
                and round_index < budget.max_search_rounds
            ):
                unresolved_hints = _unverified_candidate_hints(
                    candidate_hints,
                    round_sources.source_records,
                )
                if unresolved_hints:
                    candidate_hints = unresolved_hints
                    catalog_retry_pending = True
            accepted_record_count = int(assessment.accepted_record_count or 0)
            information_gain = max(
                0,
                accepted_record_count - previous_accepted_record_count,
            )
            stagnant_batches = (
                stagnant_batches + 1 if information_gain == 0 else 0
            )
            previous_accepted_record_count = accepted_record_count
            await _admit_round_evidence(
                orchestrator=orchestrator,
                user_id=user_id,
                run_id=run_id,
                objective=objective,
                round_index=round_index,
                round_sources=round_sources,
            )
            await orchestrator.update_research_state(
                user_id=user_id,
                run_id=run_id,
                gaps=assessment.gap_descriptions,
                exhausted_queries=assessment.exhausted_queries,
                next_actions=assessment.next_actions,
                budget=budget.model_dump(mode="json"),
                metadata={
                    "research_loop": assessment.model_dump(mode="json"),
                    "objective_plan": objective_plan.model_dump(mode="json"),
                    "semantic_plan_pending": False,
                },
                replace=True,
            )
            from app.services.research.reviewer import review_research_state

            current_state = await orchestrator.inspect_research_state(
                user_id=user_id,
                run_id=run_id,
                limit_steps=100,
                limit_evidence=100,
            )
            remaining_acquisition = acquisition_deadline - event_loop.time()
            if remaining_acquisition <= 0:
                deadline_exhausted = True
                deadline_stop_reason = "wall_clock_budget_exhausted"
                break
            try:
                async with asyncio.timeout(remaining_acquisition):
                    review = await review_research_state(
                        current_state,
                        round_index=round_index,
                        budget=budget,
                        model_id=model_name,
                        leads=_review_leads(rounds),
                        research_brief=objective_plan.model_dump(mode="json"),
                    )
            except TimeoutError:
                deadline_exhausted = True
                deadline_stop_reason = "wall_clock_budget_exhausted"
                break
            catalog_round_still_required = bool(
                book_recommendation_required
                and not catalog_candidate_round_completed
            )
            blocking_assessment_gaps = [
                gap
                for gap in assessment.gaps
                if gap != "insufficient_recommendation_candidates"
            ]
            if review.verdict == "sufficient" and (
                blocking_assessment_gaps or catalog_round_still_required
            ):
                review = review.model_copy(
                    update={
                        "verdict": "insufficient",
                        "missing_questions": list(
                            dict.fromkeys(
                                [
                                    *review.missing_questions,
                                    *[
                                        description
                                        for gap, description in zip(
                                            assessment.gaps,
                                            assessment.gap_descriptions,
                                            strict=False,
                                        )
                                        if gap != "insufficient_recommendation_candidates"
                                    ],
                                    *(
                                        ["需要完成结构化书籍目录核验。"]
                                        if catalog_round_still_required
                                        else []
                                    ),
                                ]
                            )
                        )[:5],
                        "stop_reason": "deterministic_quality_gaps_remain",
                    }
                )
            if not review.next_subquestions and assessment.should_continue:
                repair_task = plan_research_search_task(
                    objective=objective,
                    round_index=min(
                        round_index + 1,
                        budget.max_search_rounds,
                    ),
                    budget=budget,
                    previous_assessment=assessment,
                )
                if repair_task.should_search:
                    review.next_subquestions = [repair_task.query]
            if (
                stagnant_batches >= 2
                and review.verdict == "insufficient"
                and not catalog_round_still_required
            ):
                review = review.model_copy(
                    update={
                        "verdict": "budget_exhausted",
                        "stop_reason": "marginal_information_gain_exhausted",
                        "reasons": list(
                            dict.fromkeys(
                                [
                                    *review.reasons,
                                    "two_search_batches_added_no_publishable_evidence",
                                ]
                            )
                        )[:8],
                    }
                )
            discovered_frontier = _enqueue_subquestions(
                [],
                review.next_subquestions,
                used_queries=used_queries,
            )
            pending_subquestions = _enqueue_subquestions(
                discovered_frontier,
                pending_subquestions,
                used_queries=used_queries,
            )
            await orchestrator.update_research_state(
                user_id=user_id,
                run_id=run_id,
                budget=budget.model_dump(mode="json"),
                metadata={
                    "research_review": review.model_dump(mode="json"),
                    "research_pending_subquestions": list(
                        pending_subquestions
                    ),
                    "research_information_gain": {
                        "accepted_record_delta": information_gain,
                        "stagnant_batches": stagnant_batches,
                    },
                },
            )
            if (
                review.verdict == "insufficient"
                and round_index >= budget.max_search_rounds
            ):
                review = review.model_copy(
                    update={
                        "verdict": "budget_exhausted",
                        "stop_reason": "budget_exhausted_after_insufficient_review",
                    }
                )
                await orchestrator.update_research_state(
                    user_id=user_id,
                    run_id=run_id,
                    metadata={
                        "research_review": review.model_dump(mode="json"),
                    },
                )
            last_review = review
            await report_completed_step(
                kind="research",
                status=("completed" if round_sources.status == "completed" else "failed"),
                title=(
                    "关键书目核验完成"
                    if task.metadata.get("candidate_catalog_batch")
                    else "研究缺口补充完成"
                ),
                detail=(
                    f"围绕“{task.purpose[:40]}”获得 {round_sources.source_count} 个来源；"
                    f"{_review_verdict_label(review.verdict)}"
                ),
                step_id=f"research:{run_id}:round:{round_index}",
                error=round_sources.error or None,
            )
            if review.verdict in {"sufficient", "budget_exhausted"}:
                break

        if not planner_task.done():
            planner_task.cancel()
        await asyncio.gather(planner_task, return_exceptions=True)
        if deadline_exhausted:
            if last_review is not None and last_review.verdict == "insufficient":
                last_review = last_review.model_copy(
                    update={
                        "verdict": "budget_exhausted",
                        "stop_reason": deadline_stop_reason,
                    }
                )
            await report_completed_step(
                kind="research",
                status="completed",
                title="检索时间预算已用完",
                detail="已停止新增检索，将基于已核验资料整理部分结论",
                step_id=f"research:{run_id}:deadline",
            )

        report = await build_research_report(
            user_id=user_id,
            run_id=run_id,
            limit_steps=100,
            limit_evidence=100,
        )
        publishable_evidence_ids = {
            evidence_id
            for claim in [*report.verified_claims, *report.uncertain_claims]
            if claim.publishable
            for evidence_id in claim.evidence_ids
        }
        sources = [
            item
            for item in report.sources
            if item.evidence_id in publishable_evidence_ids
            and str(item.source_url or "").strip()
        ]
        evidence_count = len(sources)
        source_count = len(
            {str(item.source_url or "").strip().lower() for item in sources}
        )
        conclusion = str(report.final_answer or "").strip()
        if not conclusion:
            conclusion = (
                f"\u5df2\u6536\u5f55 {evidence_count} "
                "\u6761\u53ef\u6838\u9a8c\u8bc1\u636e\u3002"
            )
        from app.services.research.publication.report_writer import (
            verified_book_candidate_titles,
            write_research_report,
        )

        required_candidate_count = (
            budget.min_recommendation_candidates
            if book_recommendation_required
            else 0
        )
        available_candidate_count = len(
            verified_book_candidate_titles(report)
        )
        candidate_research_requirement_met = bool(
            required_candidate_count <= 0
            or available_candidate_count >= required_candidate_count
        )
        semantic_review_succeeded = bool(
            last_review is not None
            and last_review.verdict == "sufficient"
            and last_review.provider == "runtime_llm"
            and not last_review.error
        )
        reviewer_satisfied = bool(
            last_review is not None
            and (
                last_review.verdict == "sufficient"
                or (
                    previous_assessment is not None
                    and not [
                        gap
                        for gap in previous_assessment.gaps
                        if gap != "insufficient_recommendation_candidates"
                    ]
                    and catalog_candidate_round_completed
                )
            )
        )
        material_assessment_satisfied = bool(
            previous_assessment is not None
            and not [
                gap
                for gap in previous_assessment.gaps
                if gap != "insufficient_recommendation_candidates"
            ]
        )
        research_sufficient = bool(
            evidence_count
            and not deadline_exhausted
            and material_assessment_satisfied
            and reviewer_satisfied
        )
        answer_brief = build_user_answer_brief(
            objective,
            task_type=objective_plan.task_type,
            decision_dimensions=list(objective_plan.decision_dimensions),
            critical_questions=list(objective_plan.critical_unknowns),
            answer_depth=objective_plan.answer_depth,
            response_style=objective_plan.response_style,
        )
        written = await write_research_report(
            report,
            model_id=model_name,
            review={
                "objective_plan": objective_plan.model_dump(mode="json")
            },
            answer_brief=answer_brief,
            research_sufficient=research_sufficient,
            thinking_mode=thinking_mode,
        )
        content = str(written.report_markdown or "").strip()
        published_candidate_count = max(
            0,
            int(
                written.metadata.get(
                    "published_verified_candidate_count",
                    0,
                )
                or 0
            ),
        )
        candidate_requirement_met = bool(
            required_candidate_count <= 0
            or published_candidate_count >= required_candidate_count
        )
        publication_safe = bool(written.metadata.get("publication_safe"))
        answer_quality_pass = bool(
            written.metadata.get("answer_quality_pass")
        )
        status = _research_delivery_status(
            content=content,
            evidence_count=evidence_count,
        )
        delivery_succeeded = status == "completed"
        # Compatibility alias: objective satisfaction now describes research
        # sufficiency only. Publication and delivery are reported separately.
        objective_satisfied = research_sufficient
        quality_status = (
            "verified"
            if (
                research_sufficient
                and publication_safe
                and answer_quality_pass
                and delivery_succeeded
                and report.report_status == "verified"
            )
            else "partial"
            if delivery_succeeded and publication_safe and not research_sufficient
            else "degraded"
            if delivery_succeeded
            else "insufficient"
        )
        publishable_answer = delivery_succeeded and publication_safe
        await report_completed_step(
            kind="research",
            status=status,
            title=(
                "最终报告已生成"
                if answer_quality_pass
                else "已基于现有资料生成回答"
            ),
            detail=(
                f"整理了 {evidence_count} 条通过主题与来源核验的资料，来自 {source_count} 个来源"
                if quality_status == "verified"
                else f"依据 {source_count} 个来源回答了当前问题；部分结论仍需保留条件"
            ),
            step_id=f"research:{run_id}:report",
            error=written.error or None,
        )
        finished = await orchestrator.finish_research(
            user_id=user_id,
            run_id=run_id,
            conclusion=conclusion,
            status=status,
            gaps=report.gaps,
            conflicts=report.conflicts,
            metadata={
                "evidence_count": evidence_count,
                "source_count": source_count,
                "published_verified_candidate_count": published_candidate_count,
                "available_verified_candidate_count": available_candidate_count,
                "required_recommendation_candidate_count": required_candidate_count,
                "external_candidate_coverage_met": candidate_research_requirement_met,
                "candidate_coverage_policy": (
                    "advisory_external_coverage_not_publication_allowlist"
                ),
                "explicit_requested_candidate_count": requested_candidate_count,
                "book_recommendation_required": book_recommendation_required,
                "catalog_candidate_round_completed": (
                    catalog_candidate_round_completed
                ),
                "objective_task_type": objective_plan.task_type,
                "objective_plan": objective_plan.model_dump(mode="json"),
                "report_status": report.report_status,
                "report_writer_status": written.status,
                "report_delivery_status": (
                    "succeeded"
                    if delivery_succeeded
                    else "degraded"
                ),
                "report_writer_error": written.error[:300],
                "report_writer_failure_cause": written.metadata.get(
                    "failure_cause", ""
                ),
                "report_writer_attempts": written.metadata.get(
                    "attempts", []
                )[-8:],
                "objective_satisfied": objective_satisfied,
                "research_sufficient": research_sufficient,
                "publication_safe": publication_safe,
                "answer_quality_pass": answer_quality_pass,
                "delivery_succeeded": delivery_succeeded,
                "candidate_publication_requirement_met": (
                    candidate_requirement_met
                ),
                "reviewer_verdict": (
                    last_review.verdict
                    if last_review is not None
                    else "budget_exhausted"
                    if deadline_exhausted
                    else ""
                ),
                "reviewer_stop_reason": (
                    last_review.stop_reason
                    if last_review is not None
                    else deadline_stop_reason
                ),
                "semantic_review_succeeded": semantic_review_succeeded,
                "deadline_exhausted": deadline_exhausted,
                "deadline_stop_reason": deadline_stop_reason,
                "elapsed_ms": int((event_loop.time() - run_started_at) * 1000),
                "completed_rounds": len(rounds),
                "research_quality_status": quality_status,
                "quality_decision_version": "semantic-evidence-v3",
                "semantic_plan_pending": False,
                "publishable_answer": publishable_answer,
                "token_usage": _current_research_usage(),
                "deep_research_contract": DEEP_RESEARCH_RECEIPT_VERSION,
            },
        )
        return DeepResearchReceipt(
            run_id=run_id,
            objective=objective,
            status=status,
            content=content,
            conclusion=conclusion,
            evidence_count=evidence_count,
            source_count=source_count,
            quality_status=quality_status,
            created_at=(
                finished.run.created_at.isoformat()
                if finished.run.created_at is not None
                else ""
            ),
        )
    except Exception as exc:
        if not planner_task.done():
            planner_task.cancel()
        await asyncio.gather(planner_task, return_exceptions=True)
        try:
            await orchestrator.finish_research(
                user_id=user_id,
                run_id=run_id,
                conclusion=FAILED,
                status="failed",
                metadata={
                    "error": str(exc)[:500],
                    "token_usage": _current_research_usage(),
                },
            )
        except Exception:
            pass
        raise
    except asyncio.CancelledError:
        if not planner_task.done():
            planner_task.cancel()
        await asyncio.gather(planner_task, return_exceptions=True)
        try:
            await asyncio.shield(
                orchestrator.finish_research(
                    user_id=user_id,
                    run_id=run_id,
                    conclusion=FAILED,
                    status="failed",
                    metadata={
                        "error": "deep_research_cancelled",
                        "token_usage": _current_research_usage(),
                    },
                )
            )
        except Exception:
            pass
        raise


def _research_delivery_status(*, content: str, evidence_count: int) -> str:
    """Separate answer delivery success from research coverage quality."""

    return "completed" if str(content or "").strip() and evidence_count > 0 else "failed"


async def _search_round(
    *,
    orchestrator,
    user_id: UUID,
    run_id: UUID,
    task: ResearchSearchTask,
    get_search_gateway,
    check_content_garbage,
    max_records_per_round: int = 8,
) -> ResearchRoundSources:
    started_at = time.perf_counter()
    search_request = SearchRequest(
        query=task.query,
        max_results=task.max_results,
        detail=task.detail,
        strategy="federated",
        provider_budget=3,
        time_range=task.time_range,
        include_domains=task.include_domains,
        exclude_domains=task.exclude_domains,
        include_url_prefixes=task.include_url_prefixes,
        language=task.language,
        zone=("cn" if task.language.lower().startswith("zh") else None),
        category=task.category,
    )
    result = await _execute_search_task(
        gateway=get_search_gateway(),
        request=search_request,
        task=task,
    )
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    log_status = {
        "found": "completed",
        "empty": "empty_result",
        "unavailable": "failed",
    }.get(result.outcome, "failed")
    await orchestrator.search_research(
        user_id=user_id,
        run_id=run_id,
        query=task.query,
        status=log_status,
        rationale=f"Deep research round {task.round_index}.",
        results=[hit.model_dump(mode="json") for hit in result.hits],
        duration_ms=duration_ms,
        error=result.error or None,
    )
    documents: list[dict[str, Any]] = []
    garbage_reasons: list[str] = []
    candidate_verification_mode = bool(
        task.metadata.get("candidate_titles")
        or task.metadata.get("catalog_discovery_queries")
    )
    book_subject_mode = any(
        "book.douban.com/subject/" in str(prefix or "")
        for prefix in task.include_url_prefixes
    )
    if book_subject_mode:
        eligible_hits = (
            list(result.hits)
            if candidate_verification_mode
            else _book_entity_hits(result.hits, objective=task.query)
        )
        visit_limit = (
            task.max_results
            if candidate_verification_mode
            else min(task.max_results, 5)
        )
        visits = await asyncio.gather(
            *(
                fetch_research_source_document(
                    url=str(hit.url or ""),
                    query=task.query,
                    subquestion=task.objective,
                    provider_source="deep_research_source_visit",
                    include_extraction=False,
                    timeout_seconds=12,
                    metadata={"research_round": task.round_index},
                )
                for hit in eligible_hits[:visit_limit]
                if str(hit.url or "").strip()
            )
        )
        for visit in visits:
            await orchestrator.visit_source(
                user_id=user_id,
                run_id=run_id,
                url=visit.url,
                title=(
                    visit.source_document.source_title
                    if visit.source_document is not None
                    else visit.url
                ),
                status=_research_visit_step_status(
                    visit.status,
                    has_document=visit.source_document is not None,
                ),
                summary=(
                    "Fetched page body for evidence extraction."
                    if visit.source_document is not None
                    else "Source body was unavailable."
                ),
                rationale="Verify book facts from the subject page, not the search snippet.",
                duration_ms=visit.duration_ms,
                error=visit.error,
            )
            if visit.source_document is not None:
                documents.append(
                    visit.source_document.model_dump(mode="json")
                )
        if candidate_verification_mode:
            snippet_documents, snippet_garbage = _documents_from_hits(
                result,
                check_content_garbage=check_content_garbage,
                limit=task.max_results,
            )
            # A successful HTTP visit can still be a login/chrome shell with
            # no book facts.  Keep the independently returned search snippet
            # even for the same URL; claim-level deduplication happens after
            # extraction and prevents duplicate evidence.
            documents.extend(snippet_documents)
            garbage_reasons.extend(snippet_garbage)
    if not book_subject_mode and result.hits:
        visits = await asyncio.gather(
            *(
                fetch_research_source_document(
                    url=str(hit.url or ""),
                    query=task.query,
                    subquestion=task.objective,
                    provider_source="deep_research_source_visit",
                    include_extraction=False,
                    timeout_seconds=12,
                    metadata={"research_round": task.round_index},
                )
                for hit in result.hits[: min(task.max_results, 5)]
                if str(hit.url or "").strip()
            ),
            return_exceptions=True,
        )
        for visit in visits:
            if isinstance(visit, Exception):
                continue
            await orchestrator.visit_source(
                user_id=user_id,
                run_id=run_id,
                url=visit.url,
                title=(
                    visit.source_document.source_title
                    if visit.source_document is not None
                    else visit.url
                ),
                status=_research_visit_step_status(
                    visit.status,
                    has_document=visit.source_document is not None,
                ),
                summary=(
                    "Fetched page body for evidence extraction."
                    if visit.source_document is not None
                    else "Source body was unavailable."
                ),
                rationale="Read the discovered source before extracting research evidence.",
                duration_ms=visit.duration_ms,
                error=visit.error,
            )
            if visit.source_document is not None:
                documents.append(visit.source_document.model_dump(mode="json"))

        snippet_documents, snippet_garbage = _documents_from_hits(
            result,
            check_content_garbage=check_content_garbage,
            limit=task.max_results,
        )
        # A fetched page can still be a dynamic shell or lose the exact
        # passage during extraction. Preserve the provider snippet as a
        # low-strength lead; claim-level deduplication runs after extraction.
        documents.extend(snippet_documents)
        documents = documents[: task.max_results]
        garbage_reasons.extend(snippet_garbage)
    extraction = extract_research_source_records(
        query=task.query,
        subquestion=task.objective,
        documents=documents,
        provider_source=result.provider or "web_search",
        max_records_per_document=5,
        metadata={
            "research_round": task.round_index,
            "target_gaps": task.target_gaps,
            **task.metadata,
        },
    )
    rejection_codes = list(
        dict.fromkeys(
            reason
            for document in extraction.rejected_documents
            for reason in document.reason_codes
        )
    )
    source_records = (
        _fair_candidate_source_records(
            extraction.source_records,
            limit=max_records_per_round,
        )
        if candidate_verification_mode
        else extraction.source_records[:max_records_per_round]
    )
    return ResearchRoundSources(
        status=(
            "completed"
            if source_records
            else "empty_result"
            if result.outcome == "empty"
            else "failed"
        ),
        round_index=task.round_index,
        executed=True,
        task=task,
        provider=result.provider or "web_search",
        provider_status=result.outcome,
        error=_public_research_search_error(result),
        source_records=source_records,
        source_count=extraction.document_count,
        publishable_source_count=len(source_records),
        rejection_reason_codes=rejection_codes,
        metadata={
            "extraction_status": extraction.status,
            "garbage_dropped_count": len(garbage_reasons),
            "garbage_reasons": garbage_reasons,
            "provider_attempts": result.attempts,
            "records_before_round_budget": len(extraction.source_records),
            "records_after_round_budget": len(source_records),
        },
    )


def _documents_from_hits(
    result: SearchResult,
    *,
    check_content_garbage,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    documents: list[dict[str, Any]] = []
    garbage_reasons: list[str] = []
    seen_urls: set[str] = set()
    for hit in result.hits:
        url = str(hit.url or "").strip()
        if not url or url.lower() in seen_urls:
            continue
        content = " ".join(
            str(hit.content or hit.snippet or "").split()
        )[:4_000]
        if not content:
            continue
        is_garbage, garbage_reason = check_content_garbage(
            content,
            min_chars=None,
        )
        if is_garbage:
            garbage_reasons.append(garbage_reason)
            continue
        seen_urls.add(url.lower())
        source_class = classify_source(url)
        score = hit.score
        relevance = 3
        if isinstance(score, (int, float)):
            relevance = max(1, min(5, round(float(score) * 5)))
        documents.append(
            {
                "source_type": "web",
                "source_title": str(hit.title or ""),
                "source_url": url,
                "content": content,
                "quality": source_quality(source_class),
                "relevance": relevance,
                "published_date": str(hit.published_date or ""),
                "metadata": {
                    "provider_source": str(hit.provider or ""),
                    "search_query": result.query,
                    "source_class": source_class,
                    "published_date": str(hit.published_date or ""),
                    "score": score,
                },
            }
        )
        if len(documents) >= limit:
            break
    return documents, garbage_reasons


def _fair_candidate_source_records(
    records: list[Any],
    *,
    limit: int,
) -> list[Any]:
    """Preserve one record per catalog entity before adding detail records.

    One fetched subject page can yield several facts. A flat slice therefore
    used to spend the whole round budget on the first few books and silently
    discard later catalog candidates. Round-robin selection keeps candidate
    recall stable while retaining extra synopsis records when budget remains.
    """

    bounded_limit = max(1, int(limit or 1))
    groups: dict[str, list[Any]] = {}
    for index, record in enumerate(records):
        url = str(getattr(record, "source_url", "") or "").strip().casefold()
        key = url or f"record:{index}"
        groups.setdefault(key, []).append(record)
    selected: list[Any] = []
    depth = 0
    while len(selected) < bounded_limit:
        added = False
        for group in groups.values():
            if depth >= len(group):
                continue
            selected.append(group[depth])
            added = True
            if len(selected) >= bounded_limit:
                break
        if not added:
            break
        depth += 1
    return selected


async def _execute_search_task(
    *,
    gateway,
    request: SearchRequest,
    task: ResearchSearchTask,
    catalog_provider: Any | None = None,
) -> SearchResult:
    """Resolve book identities from the catalog before web-search fallbacks."""

    candidate_titles = [
        str(item or "").strip()
        for item in task.metadata.get("candidate_titles", [])
        if str(item or "").strip()
    ]
    catalog_queries = list(
        dict.fromkeys(
            " ".join(str(item or "").split()).strip()
            for item in task.metadata.get("catalog_discovery_queries", [])
            if " ".join(str(item or "").split()).strip()
        )
    )[:6]
    if not candidate_titles and not catalog_queries:
        return await gateway.search(request)

    raw_hints = task.metadata.get("candidate_search_hints") or {}
    candidate_hints = {
        title: " ".join(str(raw_hints.get(title) or title).split()).strip()
        for title in candidate_titles
    }
    if catalog_provider is None:
        from app.services.books.douban_catalog import DoubanCatalogProvider

        catalog_provider = DoubanCatalogProvider()

    hits: list[SearchHit] = []
    candidate_pool_limit = 30
    attempts: list[SearchAttempt] = []
    providers: list[str] = []
    errors: list[str] = []
    matched_exact_titles: set[str] = set()
    unmatched_titles: list[str] = []
    lookup_terms: list[tuple[str, bool]] = [
        (title, True) for title in candidate_titles
    ]
    exact_keys = {_book_title_key(title) for title in candidate_titles}
    lookup_terms.extend(
        (query, False)
        for query in catalog_queries
        if _book_title_key(query) not in exact_keys
    )
    raw_catalog_results = await asyncio.gather(
        *(
            catalog_provider.search(
                term,
                limit=min(8, request.max_results),
            )
            for term, _ in lookup_terms
        ),
        return_exceptions=True,
    )
    for (term, exact_lookup), raw in zip(
        lookup_terms,
        raw_catalog_results,
        strict=True,
    ):
        if isinstance(raw, Exception):
            attempts.append(
                SearchAttempt(
                    provider="douban_catalog",
                    outcome="unavailable",
                    error_type=raw.__class__.__name__,
                    error=(str(raw) or raw.__class__.__name__)[:300],
                )
            )
            errors.append(str(raw) or raw.__class__.__name__)
            if exact_lookup:
                unmatched_titles.append(term)
            continue
        catalog_hits = _catalog_result_hits(raw)
        outcome = (
            "found"
            if catalog_hits
            else "empty"
            if str(raw.status) in {"ok", "empty_result"}
            else "unavailable"
        )
        attempts.append(
            SearchAttempt(
                provider=str(raw.source or "douban_catalog"),
                outcome=outcome,
                duration_ms=max(0, int(raw.duration_ms or 0)),
                error_type=(
                    str(raw.status)
                    if outcome == "unavailable"
                    else ""
                ),
                error=str(raw.error or "")[:300],
            )
        )
        if raw.source and raw.source not in providers:
            providers.append(raw.source)
        if raw.error and outcome == "unavailable":
            errors.append(str(raw.error))
        if exact_lookup:
            selected = _best_candidate_subject_hit(
                catalog_hits,
                title=term,
                search_hint=candidate_hints[term],
            )
            if selected is not None and _candidate_hint_is_supported(
                selected,
                title=term,
                search_hint=candidate_hints[term],
            ):
                _append_unique_hit(hits, selected, limit=candidate_pool_limit)
                matched_exact_titles.add(term)
            else:
                unmatched_titles.append(term)
            continue
        for hit in catalog_hits:
            candidate_title = re.sub(
                r"\s*[（(]豆瓣[）)]\s*$",
                "",
                str(hit.title or ""),
            ).strip()
            if not candidate_topic_supported(
                objective=task.objective,
                candidate_title=candidate_title,
                evidence_texts=[hit.snippet, hit.content],
            ):
                continue
            _append_unique_hit(hits, hit, limit=candidate_pool_limit)

    web_titles = list(dict.fromkeys(unmatched_titles))
    requests = [
        request.model_copy(
            update={
                "query": (
                    f"{title} site:book.douban.com/subject"
                )[:300],
                "max_results": min(8, request.max_results),
                "detail": "standard",
                "language": "zh",
                "zone": "cn",
            }
        )
        for title in web_titles
    ]
    raw_results = await asyncio.gather(
        *(gateway.search(item) for item in requests),
        return_exceptions=True,
    )
    unmatched_titles = []
    subject_retry_requests: list[SearchRequest] = []
    for title, raw in zip(web_titles, raw_results, strict=True):
        if isinstance(raw, Exception):
            errors.append(str(raw) or raw.__class__.__name__)
            unmatched_titles.append(title)
            continue
        attempts.extend(raw.attempts)
        if raw.provider and raw.provider not in providers:
            providers.append(raw.provider)
        selected = _best_candidate_subject_hit(
            raw.hits,
            title=title,
            search_hint=candidate_hints[title],
        )
        if selected is not None and _candidate_hint_is_supported(
            selected,
            title=title,
            search_hint=candidate_hints[title],
        ):
            _append_unique_hit(hits, selected, limit=candidate_pool_limit)
            matched_exact_titles.add(title)
        else:
            unmatched_titles.append(title)
            if raw.error:
                errors.append(raw.error)
    if unmatched_titles:
        subject_retry_requests = [
            request.model_copy(
                update={
                    "query": (
                        f"《{title}》 {candidate_hints[title]} 豆瓣读书"
                    )[:300],
                    "max_results": min(8, request.max_results),
                    "detail": "standard",
                    "language": "zh",
                    "zone": "cn",
                }
            )
            for title in unmatched_titles
        ]
        subject_retry_results = await asyncio.gather(
            *(gateway.search(item) for item in subject_retry_requests),
            return_exceptions=True,
        )
        still_unmatched: list[str] = []
        for title, raw in zip(
            unmatched_titles,
            subject_retry_results,
            strict=True,
        ):
            if isinstance(raw, Exception):
                errors.append(str(raw) or raw.__class__.__name__)
                still_unmatched.append(title)
                continue
            attempts.extend(raw.attempts)
            if raw.provider and raw.provider not in providers:
                providers.append(raw.provider)
            selected = _best_candidate_subject_hit(
                raw.hits,
                title=title,
                search_hint=candidate_hints[title],
            )
            if selected is not None and _candidate_hint_is_supported(
                selected,
                title=title,
                search_hint=candidate_hints[title],
            ):
                _append_unique_hit(hits, selected, limit=candidate_pool_limit)
                matched_exact_titles.add(title)
            else:
                still_unmatched.append(title)
                if raw.error:
                    errors.append(raw.error)
        unmatched_titles = still_unmatched

    if unmatched_titles:
        fallback_requests = [
            request.model_copy(
                update={
                    "query": (
                        f"{title} 作者 出版社 内容简介"
                    )[:300],
                    "max_results": min(8, request.max_results),
                    "include_domains": [],
                    "include_url_prefixes": [],
                }
            )
            for title in unmatched_titles
        ]
        fallback_results = await asyncio.gather(
            *(gateway.search(item) for item in fallback_requests),
            return_exceptions=True,
        )
        for title, raw in zip(unmatched_titles, fallback_results, strict=True):
            if isinstance(raw, Exception):
                errors.append(str(raw) or raw.__class__.__name__)
                continue
            attempts.extend(raw.attempts)
            if raw.provider and raw.provider not in providers:
                providers.append(raw.provider)
            selected = _best_candidate_evidence_hit(
                raw.hits,
                title=title,
                search_hint=candidate_hints[title],
            )
            if selected is not None and _candidate_hint_is_supported(
                selected,
                title=title,
                search_hint=candidate_hints[title],
            ):
                _append_unique_hit(hits, selected, limit=candidate_pool_limit)
                matched_exact_titles.add(title)
            elif raw.error:
                errors.append(raw.error)
    effective_queries = [
        f"douban_catalog:{term}" for term, _ in lookup_terms
    ]
    effective_queries.extend(item.query for item in requests)
    if subject_retry_requests:
        effective_queries.extend(item.query for item in subject_retry_requests)
    if unmatched_titles:
        effective_queries.extend(item.query for item in fallback_requests)
    selected_hits = _rank_candidate_hits(
        hits,
        objective=task.objective,
        limit=request.max_results,
    )
    return SearchResult(
        outcome="found" if selected_hits else ("unavailable" if errors else "empty"),
        provider=",".join(providers),
        query=request.query,
        effective_query=" | ".join(effective_queries),
        hits=selected_hits,
        attempts=attempts,
        error="; ".join(dict.fromkeys(errors))[:500],
        metadata={
            "candidate_batch": True,
            "candidate_count": len(candidate_titles),
            "catalog_query_count": len(catalog_queries),
            "matched_candidate_count": len(matched_exact_titles),
            "catalog_discovered_count": max(
                0,
                len(selected_hits) - len(matched_exact_titles),
            ),
        },
    )


def _catalog_result_hits(result: Any) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for candidate in list(getattr(result, "candidates", []) or []):
        if not isinstance(candidate, dict):
            continue
        title = " ".join(str(candidate.get("title") or "").split()).strip()
        url = _canonical_douban_subject_url(candidate.get("url"))
        if not title or not url:
            continue
        author = " ".join(
            str(candidate.get("author_name") or "").split()
        ).strip()
        book_year = " ".join(
            str(candidate.get("book_published_date") or "").split()
        ).strip()
        facts = [f"《{title}》"]
        if author:
            facts.append(f"作者：{author}")
        if book_year:
            facts.append(f"出版年：{book_year}")
        content = "；".join(facts) + "。"
        hits.append(
            SearchHit(
                title=f"{title} (豆瓣)",
                url=url,
                snippet=content,
                content=content,
                provider=str(getattr(result, "source", "") or "douban_catalog"),
                score=1.0,
                metadata={
                    "catalog_identity": True,
                    "author_name": author,
                    "book_published_date": book_year,
                },
            )
        )
    return hits


def _append_unique_hit(
    hits: list[SearchHit],
    hit: SearchHit,
    *,
    limit: int,
) -> None:
    if len(hits) >= max(1, min(int(limit), 30)):
        return
    url = str(hit.url or "").strip().casefold()
    title_key = _book_title_key(hit.title)
    if any(
        (url and str(existing.url or "").strip().casefold() == url)
        or (title_key and _book_title_key(existing.title) == title_key)
        for existing in hits
    ):
        return
    hits.append(hit)


def _rank_candidate_hits(
    hits: list[SearchHit],
    *,
    objective: str,
    limit: int,
) -> list[SearchHit]:
    """Prefer catalog candidates that visibly satisfy audience constraints."""

    beginner_focus = bool(
        re.search(
            r"(?:零基础|初学者|新手|入门|beginner|from\s+scratch)",
            str(objective or ""),
            flags=re.IGNORECASE,
        )
    )

    def score(index_and_hit: tuple[int, SearchHit]) -> tuple[int, int]:
        index, hit = index_and_hit
        title = re.sub(
            r"\s*[（(]豆瓣[）)]\s*$",
            "",
            str(hit.title or ""),
        )
        text = " ".join(
            (title, str(hit.snippet or ""), str(hit.content or ""))
        ).casefold()
        value = 0
        if "深度学习" in str(objective or ""):
            if "深度学习" in text or "deep learning" in text:
                value += 120
            if "pytorch" in text or "tensorflow" in text:
                value += 70
            if "神经网络" in text or "neural network" in text:
                value += 50
            if (
                ("机器学习" in text or "machine learning" in text)
                and not re.search(
                    r"(?:深度学习|deep\s+learning|pytorch|tensorflow|神经网络|neural\s+network)",
                    text,
                    flags=re.IGNORECASE,
                )
            ):
                value -= 35
        for marker, weight in (
            ("零基础", 60),
            ("初学者", 55),
            ("新手", 50),
            ("入门", 40),
            ("漫画", 25),
            ("极简", 20),
            ("通俗", 15),
            ("实战", 8),
        ):
            if marker in text:
                value += weight
        if beginner_focus and not re.search(
            r"(?:零基础|初学者|新手|入门|漫画|极简|通俗)",
            text,
        ):
            value -= 30
        return value, -index

    ranked = [
        hit
        for _, hit in sorted(
            enumerate(hits),
            key=score,
            reverse=True,
        )
    ]
    return ranked[: max(1, min(int(limit or 1), 10))]


def _best_candidate_subject_hit(
    hits: list[SearchHit],
    *,
    title: str,
    search_hint: str = "",
) -> SearchHit | None:
    candidate_key = _book_title_key(title)
    ranked: list[tuple[int, int, float, SearchHit]] = []
    for hit in hits:
        canonical_url = _canonical_douban_subject_url(str(hit.url or ""))
        hit_key = _book_title_key(
            re.sub(r"\s*[（(]豆瓣[）)]\s*$", "", str(hit.title or ""))
        )
        if (
            not canonical_url
            or not candidate_key
            or not hit_key.startswith(candidate_key)
        ):
            continue
        extra_chars = max(0, len(hit_key) - len(candidate_key))
        hint_score = _candidate_hint_score(hit, title=title, search_hint=search_hint)
        ranked.append(
            (
                -hint_score,
                extra_chars,
                -(float(hit.score) if isinstance(hit.score, (int, float)) else 0.0),
                hit.model_copy(update={"url": canonical_url}),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return ranked[0][3] if ranked else None


def _best_candidate_evidence_hit(
    hits: list[SearchHit],
    *,
    title: str,
    search_hint: str = "",
) -> SearchHit | None:
    subject = _best_candidate_subject_hit(
        hits,
        title=title,
        search_hint=search_hint,
    )
    if subject is not None:
        return subject
    candidate_key = _book_title_key(title)
    ranked: list[tuple[int, int, float, SearchHit]] = []
    for hit in hits:
        source_class = classify_source(str(hit.url or ""))
        if source_class in {
            "marketplace",
            "low_trust_text",
            "social",
            "unknown",
        }:
            continue
        hit_key = _book_title_key(str(hit.title or ""))
        if not candidate_key or not hit_key.startswith(candidate_key):
            continue
        extra_chars = max(0, len(hit_key) - len(candidate_key))
        hint_score = _candidate_hint_score(hit, title=title, search_hint=search_hint)
        ranked.append(
            (
                -hint_score,
                extra_chars,
                -(float(hit.score) if isinstance(hit.score, (int, float)) else 0.0),
                hit,
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return ranked[0][3] if ranked else None


def _candidate_hint_score(
    hit: SearchHit,
    *,
    title: str,
    search_hint: str,
) -> int:
    """Rank same-title results using author/edition hints from the one plan."""

    haystack = _book_title_key(
        " ".join((str(hit.title or ""), str(hit.snippet or ""), str(hit.content or "")))
    )
    tokens = _candidate_hint_keys(title=title, search_hint=search_hint)
    score = 0
    for key in tokens:
        if key in haystack:
            score += min(len(key), 12)
    return score


def _candidate_hint_is_supported(
    hit: SearchHit,
    *,
    title: str,
    search_hint: str,
) -> bool:
    keys = _candidate_hint_keys(title=title, search_hint=search_hint)
    return not keys or _candidate_hint_score(
        hit,
        title=title,
        search_hint=search_hint,
    ) > 0


def _candidate_hint_keys(*, title: str, search_hint: str) -> list[str]:
    title_key = _book_title_key(title)
    generic = {
        "作者", "出版社", "内容简介", "豆瓣", "书籍", "经典", "推荐",
        "中文版", "原版书名", "isbn", "site", "book", "douban", "subject",
    }
    keys: list[str] = []
    for token in re.findall(
        r"[A-Za-z0-9\u4e00-\u9fff]{2,}",
        str(search_hint or ""),
    ):
        key = _book_title_key(token)
        if not key or key == title_key or key in generic or key in keys:
            continue
        keys.append(key)
    return keys


def _book_title_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())


def _research_visit_step_status(status: str, *, has_document: bool) -> str:
    """Map adapter detail statuses onto the persisted ResearchStep contract."""

    if has_document:
        return "completed"
    if status == "timeout":
        return "timeout"
    if status in {"empty_result", "no_extractable_claims"}:
        return "empty_result"
    return "failed"


def _public_research_search_error(result: SearchResult) -> str:
    if result.outcome in {"found", "empty"}:
        return ""
    return "本轮外部检索暂时不可用，系统会在剩余预算内继续尝试其他方向。"


def _review_verdict_label(verdict: str) -> str:
    return {
        "sufficient": "证据已满足回答要求",
        "insufficient": "证据仍需补充，继续下一轮",
        "budget_exhausted": "已达到检索预算，将按现有证据部分回答",
    }.get(str(verdict or ""), "本轮审查已完成")


def _book_entity_hits(
    hits: list[SearchHit],
    *,
    objective: str,
) -> list[SearchHit]:
    """Keep exact-title subject hits for a quoted-work research objective."""

    titles = [
        " ".join(match.split()).strip().casefold()
        for match in re.findall(r"《([^》]{1,100})》", str(objective or ""))
        if " ".join(match.split()).strip()
    ]
    if not titles:
        return list(hits)
    selected: list[SearchHit] = []
    for hit in hits:
        if not any(title in str(hit.title or "").casefold() for title in titles):
            continue
        canonical_url = _canonical_douban_subject_url(str(hit.url or ""))
        if not canonical_url:
            continue
        selected.append(hit.model_copy(update={"url": canonical_url}))
    return selected


def _canonical_douban_subject_url(value: str) -> str:
    match = re.fullmatch(
        r"https?://book\.douban\.com/subject/+(\d+)/?(?:\?[^#]*)?",
        str(value or "").strip(),
        flags=re.IGNORECASE,
    )
    if match is None:
        return ""
    return f"https://book.douban.com/subject/{match.group(1)}/"


async def _admit_round_evidence(
    *,
    orchestrator,
    user_id: UUID,
    run_id: UUID,
    objective: str,
    round_index: int,
    round_sources: ResearchRoundSources,
) -> None:
    for record in round_sources.source_records:
        assessment = assess_evidence_candidate(
            claim=record.claim,
            query=objective,
            source_title=record.source_title,
            source_url=record.source_url,
            published_date=str(
                (record.metadata or {}).get("published_date") or ""
            ),
        )
        if not assessment.publishable:
            continue
        await orchestrator.add_evidence(
            user_id=user_id,
            run_id=run_id,
            evidence=ResearchEvidence(
                run_id=run_id,
                source_type="web",
                source_title=record.source_title,
                source_url=record.source_url,
                claim=record.claim,
                excerpt=record.excerpt,
                quality=record.quality,
                relevance=record.relevance,
                metadata={
                    **record.metadata,
                    "capture_source": "deep_research_toggle",
                    "research_round": round_index,
                },
            ),
            known_facts=[record.claim],
            next_actions=["evaluate_research_gaps"],
        )


def _current_research_usage() -> dict[str, int]:
    collector = current_execution_progress()
    if collector is None:
        return {}
    return collector.usage_summary().model_dump(mode="json")


__all__ = [
    "DeepResearchReceipt",
    "run_deep_research",
    "run_deep_research_turn",
]
