from __future__ import annotations

import asyncio
import re
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
from app.services.research.source_visit import fetch_research_source_document
from app.services.execution_progress import (
    current_execution_progress,
    report_completed_step,
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
    candidate_hints: dict[str, str] | None = None,
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
        discovered_titles = re.findall(r"《([^》]{1,100})》", candidate)
        known_hints = candidate_hints or {}
        candidate_titles = [
            title for title in discovered_titles if title in known_hints
        ]
        candidate_verification = bool(
            candidate_titles and "book_recommendation" in base.requirements
        )
        return ResearchSearchTask(
            round_index=round_index,
            objective=objective,
            purpose="reviewer_gap",
            query=(
                "site:book.douban.com/subject/ " + candidate
                if candidate_verification
                else normalized
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


def _normalize_query(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


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
    from app.services.research.objective_planner import plan_research_objective

    objective_plan = await plan_research_objective(
        objective,
        model_id=model_name,
    )

    budget = ResearchLoopBudget(
        max_search_rounds=3,
        max_results_per_round=8,
        max_records_per_round=8,
        min_independent_sources=1,
        required_evidence_quality="medium",
        min_recommendation_candidates=(
            4 if objective_plan.task_type == "book_recommendation" else 0
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


    previous_assessment: ResearchGapAssessment | None = None
    rounds: list[ResearchRoundSources] = []
    from app.services.research.search_policy import build_research_search_request

    # The immutable goal seeds a broad first query.  The semantic plan contributes
    # only a bounded initial frontier; evidence and Reviewer gaps may grow it.
    broad_discovery_query = build_research_search_request(objective).query
    pending_subquestions: list[str] = list(
        dict.fromkeys(
            [broad_discovery_query, *objective_plan.search_queries]
        )
    )
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
                candidate_hints={},
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
                leads=_review_leads(rounds),
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
            await report_completed_step(
                kind="research",
                status=("completed" if round_sources.status == "completed" else "failed"),
                title=f"\u7b2c {round_index} \u8f6e\u7814\u7a76\u5b8c\u6210",
                detail=(
                    f"\u83b7\u5f97 {round_sources.source_count} \u4e2a\u6765\u6e90\uff1b"
                    f"{_review_verdict_label(review.verdict)}"
                ),
                step_id=f"research:{run_id}:round:{round_index}",
                error=round_sources.error or None,
            )
            if review.verdict in {"sufficient", "budget_exhausted"}:
                break

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
            write_research_report,
        )

        review_context = (
            last_review.model_dump(mode="json")
            if last_review is not None
            else {}
        )
        review_context["objective_plan"] = objective_plan.model_dump(mode="json")
        written = await write_research_report(
            report,
            model_id=model_name,
            review=review_context,
        )
        content = str(written.report_markdown or "").strip()
        objective_satisfied = bool(
            evidence_count
            and (
                objective_plan.task_type != "book_recommendation"
                or (
                    previous_assessment is not None
                    and previous_assessment.satisfied
                )
            )
        )
        # Coverage quality and execution success are different axes. A
        # source-backed partial answer with an honest limitation is a valid
        # completed delivery; only the absence of publishable content is a
        # failed turn. Keep objective_satisfied in metadata for Trace/ResearchRun
        # quality analysis instead of surfacing a technical failure to Chat.
        status = _research_delivery_status(
            content=content,
            evidence_count=evidence_count,
        )
        publishable_answer = status == "completed"
        await report_completed_step(
            kind="research",
            status=status,
            title="最终回答已整理",
            detail=f"整理了 {evidence_count} 条可靠资料，来自 {source_count} 个来源",
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
                "report_status": report.report_status,
                "report_writer_status": written.status,
                "report_writer_error": written.error[:300],
                "report_writer_failure_cause": written.metadata.get(
                    "failure_cause", ""
                ),
                "report_writer_attempts": written.metadata.get(
                    "attempts", []
                )[-8:],
                "objective_satisfied": objective_satisfied,
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
                metadata={
                    "error": str(exc)[:500],
                    "token_usage": _current_research_usage(),
                },
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
    candidate_verification_mode = bool(task.metadata.get("candidate_titles"))
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
            if task.metadata.get("candidate_titles")
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
        visited_urls: set[str] = set()
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
                visited_urls.add(str(visit.url or "").strip().casefold())

        snippet_documents, snippet_garbage = _documents_from_hits(
            result,
            check_content_garbage=check_content_garbage,
            limit=task.max_results,
        )
        documents.extend(
            document
            for document in snippet_documents
            if str(document.get("source_url") or "").strip().casefold()
            not in visited_urls
        )
        documents = documents[: task.max_results]
        garbage_reasons.extend(snippet_garbage)
    extraction = extract_research_source_records(
        query=task.query,
        subquestion=task.objective,
        documents=documents,
        provider_source=result.provider or "web_search",
        max_records_per_document=12,
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
        error=_public_research_search_error(result),
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


async def _execute_search_task(
    *,
    gateway,
    request: SearchRequest,
    task: ResearchSearchTask,
) -> SearchResult:
    """Search recommendation candidates independently, then merge one hit each."""

    candidate_titles = [
        str(item or "").strip()
        for item in task.metadata.get("candidate_titles", [])
        if str(item or "").strip()
    ]
    if not candidate_titles:
        return await gateway.search(request)

    raw_hints = task.metadata.get("candidate_search_hints") or {}
    candidate_hints = {
        title: " ".join(str(raw_hints.get(title) or title).split()).strip()
        for title in candidate_titles
    }

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
        for title in candidate_titles
    ]
    raw_results = await asyncio.gather(
        *(gateway.search(item) for item in requests),
        return_exceptions=True,
    )
    hits: list[SearchHit] = []
    attempts = []
    providers: list[str] = []
    errors: list[str] = []
    unmatched_titles: list[str] = []
    subject_retry_requests: list[SearchRequest] = []
    for title, raw in zip(candidate_titles, raw_results, strict=True):
        if isinstance(raw, Exception):
            errors.append(str(raw) or raw.__class__.__name__)
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
            hits.append(selected)
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
                hits.append(selected)
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
                hits.append(selected)
            elif raw.error:
                errors.append(raw.error)
    effective_queries = [item.query for item in requests]
    if subject_retry_requests:
        effective_queries.extend(item.query for item in subject_retry_requests)
    if unmatched_titles:
        effective_queries.extend(item.query for item in fallback_requests)
    return SearchResult(
        outcome="found" if hits else ("unavailable" if errors else "empty"),
        provider=",".join(providers),
        query=request.query,
        effective_query=" | ".join(effective_queries),
        hits=hits,
        attempts=attempts,
        error="; ".join(dict.fromkeys(errors))[:500],
        metadata={
            "candidate_batch": True,
            "candidate_count": len(candidate_titles),
            "matched_candidate_count": len(hits),
        },
    )


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
        "budget_exhausted": "已完成预算内核验",
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
