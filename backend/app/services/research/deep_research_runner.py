from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.chat import UserInput
from app.services.agent_core.contracts import PublishedAnswer
from app.services.external_search.contracts import SearchHit, SearchRequest, SearchResult
from app.services.research.contracts import ResearchEvidence
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


DEEP_RESEARCH_RECEIPT_VERSION = "deep-research-receipt-v1"

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
    created_at: str = ""

    def to_published_answer(self) -> PublishedAnswer:
        """Publish the complete report as the chat message body.

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

        model_name = resolve_model_name(requested_model) or ""
    receipt = await run_deep_research(
        user_id=user_input.user_id,
        thread_id=user_input.thread_id,
        objective=user_input.content,
        model_name=model_name,
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
) -> ResearchSearchTask | None:
    """Pop the next reviewer subquestion, falling back to the planner.

    Reviewer gaps become executable next-round searches; deterministic
    planning stays the fallback when the queue is empty or exhausted.
    """

    from app.services.research.search_policy import (
        build_research_search_request,
    )

    while pending_subquestions:
        candidate = pending_subquestions.pop(0)
        normalized = _normalize_query(candidate)
        if not normalized or normalized in used_queries:
            continue
        base = build_research_search_request(objective)
        return ResearchSearchTask(
            round_index=round_index,
            objective=objective,
            purpose="reviewer_gap",
            query=normalized[:300],
            should_search=True,
            target_gaps=[],
            exhausted_queries=sorted(used_queries)[-8:],
            max_results=budget.max_results_per_round,
            detail=base.detail,
            time_range=base.time_range,
            include_domains=base.include_domains,
            exclude_domains=base.exclude_domains,
            include_url_prefixes=base.include_url_prefixes,
            language=base.language,
            category=base.category,
            metadata={"source": "reviewer_subquestion"},
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


def _normalize_query(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


async def run_deep_research(
    *,
    user_id: UUID,
    thread_id: UUID | None,
    objective: str,
    model_name: str = "",
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

    budget = ResearchLoopBudget(
        max_search_rounds=2,
        max_results_per_round=5,
        max_records_per_round=3,
        min_independent_sources=1,
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
        metadata={"capture_source": "deep_research_toggle"},
    )
    run_id = started.run.id
    if run_id is None:
        raise RuntimeError("deep research run was not created")

    previous_assessment: ResearchGapAssessment | None = None
    rounds: list[ResearchRoundSources] = []
    pending_subquestions: list[str] = []
    used_queries: set[str] = set()
    last_review = None
    try:
        for round_index in range(1, budget.max_search_rounds + 1):
            task = _plan_next_task(
                objective=objective,
                round_index=round_index,
                budget=budget,
                previous_assessment=previous_assessment,
                pending_subquestions=pending_subquestions,
                used_queries=used_queries,
            )
            if task is None or not task.should_search:
                break
            used_queries.add(_normalize_query(task.query))
            round_sources = await _search_round(
                orchestrator=orchestrator,
                user_id=user_id,
                run_id=run_id,
                task=task,
                get_search_gateway=get_search_gateway,
                check_content_garbage=check_content_garbage,
            )
            rounds.append(round_sources)
            assessment = evaluate_research_gaps(
                round_index=round_index,
                rounds=rounds,
                budget=budget,
            )
            previous_assessment = assessment
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
                metadata={
                    "research_loop": assessment.model_dump(mode="json"),
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
            review = await review_research_state(
                current_state,
                round_index=round_index,
                budget=budget,
                model_id=model_name,
            )
            pending_subquestions = _enqueue_subquestions(
                pending_subquestions,
                review.next_subquestions,
                used_queries=used_queries,
            )
            await orchestrator.update_research_state(
                user_id=user_id,
                run_id=run_id,
                metadata={
                    "research_review": review.model_dump(mode="json"),
                    "research_pending_subquestions": list(
                        pending_subquestions
                    ),
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
            if review.verdict in {"sufficient", "budget_exhausted"}:
                break

        report = await build_research_report(
            user_id=user_id,
            run_id=run_id,
            limit_steps=100,
            limit_evidence=100,
        )
        sources = [
            item for item in report.sources if str(item.source_url or "").strip()
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
            write_research_report,
        )

        written = await write_research_report(
            report,
            model_id=model_name,
            review=(
                last_review.model_dump(mode="json")
                if last_review is not None
                else None
            ),
        )
        content = str(written.report_markdown or "").strip()
        status = "completed" if evidence_count else "failed"
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
                "report_status": report.report_status,
                "report_writer_status": written.status,
                "report_writer_error": written.error[:300],
                "report_writer_failure_cause": written.metadata.get(
                    "failure_cause", ""
                ),
                "report_writer_attempts": written.metadata.get(
                    "attempts", []
                )[-8:],
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
            created_at=(
                finished.run.created_at.isoformat()
                if finished.run.created_at is not None
                else ""
            ),
        )
    except Exception as exc:
        try:
            await orchestrator.finish_research(
                user_id=user_id,
                run_id=run_id,
                conclusion=FAILED,
                status="failed",
                metadata={"error": str(exc)[:500]},
            )
        except Exception:
            pass
        raise


async def _search_round(
    *,
    orchestrator,
    user_id: UUID,
    run_id: UUID,
    task: ResearchSearchTask,
    get_search_gateway,
    check_content_garbage,
) -> ResearchRoundSources:
    started_at = time.perf_counter()
    search_request = SearchRequest(
        query=task.query,
        max_results=task.max_results,
        detail=task.detail,
        time_range=task.time_range,
        include_domains=task.include_domains,
        exclude_domains=task.exclude_domains,
        include_url_prefixes=task.include_url_prefixes,
        language=task.language,
        zone=("cn" if task.language.lower().startswith("zh") else None),
        category=task.category,
    )
    result = await get_search_gateway().search(search_request)
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
    documents, garbage_reasons = _documents_from_hits(
        result,
        check_content_garbage=check_content_garbage,
        limit=task.max_results,
    )
    extraction = extract_research_source_records(
        query=task.query,
        subquestion=task.objective,
        documents=documents,
        provider_source=result.provider or "web_search",
        max_records_per_document=1,
        metadata={
            "research_round": task.round_index,
            "target_gaps": task.target_gaps,
        },
    )
    rejection_codes = list(
        dict.fromkeys(
            reason
            for document in extraction.rejected_documents
            for reason in document.reason_codes
        )
    )
    return ResearchRoundSources(
        round_index=task.round_index,
        executed=True,
        task=task,
        provider=result.provider or "web_search",
        provider_status=result.outcome,
        error=result.error or "",
        source_records=extraction.source_records,
        source_count=extraction.document_count,
        publishable_source_count=extraction.extracted_count,
        rejection_reason_codes=rejection_codes,
        metadata={
            "extraction_status": extraction.status,
            "garbage_dropped_count": len(garbage_reasons),
            "garbage_reasons": garbage_reasons,
            "provider_attempts": result.attempts,
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


__all__ = [
    "DeepResearchReceipt",
    "run_deep_research",
    "run_deep_research_turn",
]
