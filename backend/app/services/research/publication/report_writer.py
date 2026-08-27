from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, Field

from app.services.research.publication.publisher import publish_research_answer
from app.services.research.publication.synthesis import (
    synthesize_research_report,
    synthesize_research_report_deterministic,
)
from app.infra.llm.model_candidates import (
    record_model_failure,
    record_model_success,
)
from app.services.research.report import ResearchReport
from app.services.user_answer_contracts import (
    UserAnswerBrief,
    build_user_answer_brief,
)
from app.services.research.publication.packet import (
    ResearchPublicationPacket,
    build_research_publication_packet,
)
from app.services.research.publication.quality import evaluate_publication_axes
from app.services.research.publication.semantic_quality import (
    evaluate_semantic_answer,
)
from app.services.research.candidate_quality import (
    catalog_book_title,
    candidate_topic_supported,
    is_book_catalog_url,
    normalize_candidate_title,
)
from app.services.research.search_policy import (
    is_book_recommendation_request,
    requested_book_count,
)
from app.services.books.book_identity import normalize_book_work_title
from app.services.publication_safety import (
    contains_internal_reasoning,
    explicit_table_requested,
    markdown_table_present,
    requested_table_columns,
    table_contract_satisfied,
)
from app.services.execution_progress import (
    report_completed_step,
    report_model_completion,
)


logger = logging.getLogger(__name__)

RESEARCH_REPORT_WRITE_CONTRACT_VERSION = "research-report-write-v1"
# P95 of observed report-model calls ~17.9s (n=4, 11.6-18.0s); 45s keeps
# 2.5x headroom while bounding worst-case waits before deterministic fallback.
REPORT_TIMEOUT_SECONDS = 60
REPORT_REPAIR_TIMEOUT_SECONDS = 45
MAX_EVIDENCE = 60
REPORT_MAX_OUTPUT_TOKENS = 4200
REPORT_RETRY_EVIDENCE_LINES = 18

_URL_RE = re.compile(r"https?://[^\s)\]>]+\S*", re.IGNORECASE)
_CITATION_RE = re.compile(r"\[(\d+)\]")
_FENCE_RE = re.compile(r"^```(?:markdown)?\s*|\s*```$", re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_MODEL_SOURCES_SECTION_RE = re.compile(
    r"(?ims)^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:来源|sources)\s*[:：]?\s*(?:\*\*)?\s*$.*\Z"
)
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
class ResearchReportWriteResult(BaseModel):
    """Goal-centered final report written from admitted evidence."""

    result_mode: str = "research_report_write"
    contract_version: str = RESEARCH_REPORT_WRITE_CONTRACT_VERSION
    run_id: UUID
    objective: str
    status: str = "fallback"
    provider: str = "deterministic"
    model_id: str = ""
    report_markdown: str = ""
    cited_source_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


async def write_research_report(
    report: ResearchReport | dict[str, Any],
    *,
    model_id: str = "",
    review: dict[str, Any] | None = None,
    answer_brief: UserAnswerBrief | dict[str, Any] | None = None,
    research_sufficient: bool | None = None,
    thinking_mode: bool = False,
) -> ResearchReportWriteResult:
    """Write one report that answers the user's objective from evidence.

    The final answer is taken only from user-facing text. One bounded repair
    attempt handles format/gate failures; thinking-only, unsafe, empty, or
    repeatedly invalid output falls back to the deterministic source-backed
    renderer.
    """

    validated = ResearchReport.model_validate(report)
    language = "zh-CN" if re.search(r"[\u4e00-\u9fff]", validated.objective) else "en"
    evidence = _evidence_list(validated, limit=MAX_EVIDENCE)
    book_candidate_rows = (
        _verified_book_candidate_rows(
            objective=validated.objective,
            evidence=evidence,
        )
        if _book_recommendation_requested(validated.objective)
        else []
    )
    limitations = _limitations(validated)
    objective_plan = (
        review.get("objective_plan", {})
        if isinstance(review, dict)
        and isinstance(review.get("objective_plan"), dict)
        else {}
    )
    brief = (
        UserAnswerBrief.model_validate(answer_brief)
        if answer_brief is not None
        else build_user_answer_brief(
            validated.objective,
            task_type=str(objective_plan.get("task_type") or "general_research"),
            decision_dimensions=list(
                objective_plan.get("decision_dimensions") or []
            ),
            critical_questions=list(objective_plan.get("critical_unknowns") or []),
            answer_depth=str(objective_plan.get("answer_depth") or "deep"),
            response_style=str(
                objective_plan.get("response_style") or "conversational"
            ),
        )
    )
    presentation_style = brief.response_style
    presentation_review = {
        "objective_plan": {
            "task_type": brief.task_type,
            "decision_dimensions": list(brief.decision_dimensions),
            "critical_unknowns": list(brief.critical_questions),
            "answer_depth": brief.answer_depth,
            "response_style": brief.response_style,
        }
    }
    planned_candidate_titles = [
        " ".join(str(item or "").split()).strip()
        for item in objective_plan.get("candidate_titles", [])
        if " ".join(str(item or "").split()).strip()
    ]
    verified_candidate_titles = [
        str(item.get("candidate_title") or "").strip()
        for item in book_candidate_rows
        if str(item.get("candidate_title") or "").strip()
    ]
    allowed_candidate_titles = list(
        dict.fromkeys([*planned_candidate_titles, *verified_candidate_titles])
    )[:10]
    explicit_candidate_count = requested_book_count(validated.objective)
    model_candidate_count = int(
        objective_plan.get("recommended_candidate_count") or 0
    )
    target_candidate_count = (
        min(
            len(allowed_candidate_titles),
            max(
                explicit_candidate_count,
                model_candidate_count or len(planned_candidate_titles),
            ),
        )
        if allowed_candidate_titles
        else 0
    )
    presentation_contract = build_research_presentation_contract(
        objective=validated.objective,
        review=presentation_review,
        language=language,
        candidate_count=target_candidate_count or len(book_candidate_rows),
    )
    publication_packet = build_research_publication_packet(
        brief=brief,
        evidence=evidence,
        evidence_strategy=objective_plan.get("evidence_strategy"),
        allowed_recommendation_entities=allowed_candidate_titles,
        externally_verified_entities=verified_candidate_titles,
        candidate_portfolio=list(objective_plan.get("candidate_portfolio") or []),
        target_candidate_count=target_candidate_count,
        user_relevant_limitations=limitations,
        material_conflicts=list(validated.conflicts),
    )
    research_ready = (
        bool(research_sufficient)
        if research_sufficient is not None
        else validated.report_status == "verified"
    )

    selected_model_id = ""
    attempts: list[dict[str, Any]] = []
    error = ""
    if evidence:
        selected_model_id = model_id or _resolve_model_id("")
        if selected_model_id:
            prompt = _prompt_freeform(
                objective=validated.objective,
                language=language,
                evidence=evidence,
                limitations=limitations,
                review=presentation_review,
                presentation_contract=presentation_contract,
                allowed_candidate_titles=allowed_candidate_titles,
                publication_packet=publication_packet,
            )
            try:
                rendered, cited_ids, response, body, call_attempts = (
                    await _generate_report_once(
                        model_id=selected_model_id,
                        prompt=prompt,
                        objective=validated.objective,
                        evidence=evidence,
                        # Citation numbers and the rendered source list must
                        # share the exact same admitted-evidence sequence.
                        sources=evidence,
                        language=language,
                        presentation_style=presentation_style,
                        presentation_contract=presentation_contract,
                        publication_packet=publication_packet,
                        thinking_mode=thinking_mode,
                    )
                )
                attempts.extend(call_attempts)
                last_attempt = call_attempts[-1] if call_attempts else {}
                if not last_attempt.get("ok"):
                    cause = _attempt_cause(
                        str(last_attempt.get("error") or "")
                    )
                    if cause:
                        record_model_failure(selected_model_id, cause)
                if rendered.strip() and last_attempt.get("ok"):
                    record_model_success(selected_model_id)
                    gate_checks = dict(last_attempt.get("gate_checks") or {})
                    quality_axes = evaluate_publication_axes(
                        research_sufficient=research_ready,
                        render_ok=bool(gate_checks.get("render")),
                        entity_safe=bool(
                            gate_checks.get("candidate_entity_safe")
                        ),
                        format_ok=bool(gate_checks.get("format")),
                        candidate_quality_ok=bool(
                            gate_checks.get("verified_candidates")
                        ),
                        answer_quality_ok=bool(
                            gate_checks.get("answer_quality")
                        ),
                        delivered=bool(rendered.strip()),
                        quality_reasons=_attempt_quality_reasons(last_attempt),
                    )
                    published_book_titles = _published_book_titles(
                        body,
                        objective=validated.objective,
                    )
                    return ResearchReportWriteResult(
                        run_id=validated.run_id,
                        objective=validated.objective,
                        status="synthesized",
                        provider="runtime_llm",
                        model_id=selected_model_id,
                        report_markdown=rendered,
                        cited_source_ids=cited_ids,
                        limitations=limitations,
                        metadata={
                            "external_call": True,
                            "sections": _sections(rendered),
                            "attempts": attempts,
                            "failure_cause": "success",
                            "presentation_style": presentation_style,
                            "presentation_contract": presentation_contract,
                            "published_verified_candidate_count": len(
                                published_book_titles
                            ),
                            "published_verified_candidate_titles": (
                                published_book_titles
                            ),
                            **quality_axes.model_dump(mode="json"),
                        },
                    )
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                record_model_failure(selected_model_id, _attempt_cause(error))
                attempts.append(
                    {
                        "model_id": selected_model_id,
                        "ok": False,
                        "error": error[:200],
                        "duration_ms": 0,
                        "body_chars": 0,
                        "report_chars": 0,
                    }
                )
                logger.warning(
                    "report_writer failed model=%s error=%s",
                    selected_model_id,
                    error[:500],
                )
        if not attempts:
            error = "no model available for report writing"
        elif not error:
            error = str(attempts[-1].get("error") or "").strip()
            if not error:
                error = "report writer returned no usable markdown"
    else:
        error = "no admitted evidence to synthesize"

    cause = classify_report_failure(attempts, error=error)
    fallback = _fallback_markdown(
        validated,
        language=language,
        presentation_style=presentation_style,
        publication_packet=publication_packet,
    )
    fallback_axes = evaluate_publication_axes(
        research_sufficient=research_ready,
        render_ok=bool(fallback.strip()),
        entity_safe=True,
        format_ok=_format_contract_satisfied(
            fallback,
            objective=validated.objective,
        ),
        candidate_quality_ok=True,
        answer_quality_ok=False,
        delivered=bool(fallback.strip()),
        quality_reasons=["model_answer_unavailable"],
    )
    return ResearchReportWriteResult(
        run_id=validated.run_id,
        objective=validated.objective,
        status="fallback",
        provider="deterministic",
        model_id=selected_model_id,
        report_markdown=fallback,
        cited_source_ids=[
            source_id
            for source_id in dict.fromkeys(
                str(source_id)
                for item in (
                    book_candidate_rows
                    if _book_recommendation_requested(validated.objective)
                    else evidence
                )
                for source_id in (
                    item.get("bound_source_ids")
                    or [item.get("source_id")]
                )
                if source_id
            )
        ],
        limitations=limitations,
        error=error[:500],
        metadata={
            "external_call": False,
            "attempts": attempts,
            "failure_cause": cause,
            "presentation_style": presentation_style,
            "presentation_contract": presentation_contract,
            "published_verified_candidate_count": len(book_candidate_rows),
            "published_verified_candidate_titles": [
                str(item.get("candidate_title") or "")
                for item in book_candidate_rows
                if str(item.get("candidate_title") or "")
            ],
            **fallback_axes.model_dump(mode="json"),
        },
    )


async def _generate_report_once(
    *,
    model_id: str,
    prompt: str,
    objective: str,
    evidence: list[dict[str, Any]],
    language: str,
    presentation_style: str = "conversational",
    presentation_contract: dict[str, Any] | None = None,
    publication_packet: ResearchPublicationPacket | None = None,
    sources: list[dict[str, Any]] | None = None,
    thinking_mode: bool = False,
) -> tuple[str, list[str], Any, str, list[dict[str, Any]]]:
    """Generate a final-only answer with one bounded repair attempt."""

    from app.infra.llm import get_llm

    attempts: list[dict[str, Any]] = []
    last_response = None
    last_body = ""
    previous_error = ""
    previous_feedback = ""
    best_safe_result: tuple[str, list[str], Any, str, dict[str, Any]] | None = None
    best_safe_score = -1
    for attempt_index in range(2):
        call_started = time.perf_counter()
        response = None
        step_id = f"model:research:report:{attempt_index}:{id(prompt)}"
        await report_completed_step(
            kind="model",
            status="waiting",
            title="正在整理最终回答",
            detail="正在把研究材料组织成自然、完整且可引用的回答",
            model_name=model_id,
            step_id=step_id,
        )
        try:
            # Report writing is a publication step, not a reasoning surface.
            # Always request final-only output even when the research run used
            # provider reasoning internally.
            attempt_thinking_mode = False
            model = get_llm(
                model_id,
                thinking_mode=attempt_thinking_mode,
            )
            if hasattr(model, "bind"):
                model = model.bind(max_tokens=REPORT_MAX_OUTPUT_TOKENS)
            if attempt_index == 0:
                attempt_prompt = prompt
            elif "timeout" in previous_error.casefold():
                attempt_prompt = _compact_report_retry_prompt(prompt) + (
                    "\n\nThe previous attempt timed out. Produce a concise "
                    "complete answer first and stop after the final section."
                )
            else:
                attempt_prompt = prompt + (
                    "\n\nThe previous draft was rejected. Rewrite it from "
                    "scratch and satisfy the original output contract. "
                    "Return only the final user-facing answer."
                    + previous_feedback
                )
            timeout_seconds = (
                REPORT_TIMEOUT_SECONDS
                if attempt_index == 0
                else REPORT_REPAIR_TIMEOUT_SECONDS
            )
            response, stream_timed_out = await _invoke_report_model(
                model,
                attempt_prompt,
                timeout_seconds=timeout_seconds,
            )
            final_text, thinking_text = _message_text_and_thinking(response)
            last_response = response
            body = _safe_final_report_candidate(final_text)
            canonicalized_titles: list[dict[str, str]] = []
            if body and _book_recommendation_requested(objective):
                body, canonicalized_titles = _canonicalize_verified_book_titles(
                    body,
                    objective=objective,
                    evidence=evidence,
                    allowed_candidate_titles=(
                        publication_packet.allowed_recommendation_entities
                        if publication_packet is not None
                        else None
                    ),
                )
                body = _normalize_recommendation_evidence_column(body)
            last_body = body
            if body:
                rendered, cited_ids, render_ok = _render_final_deliverable(
                    body,
                    evidence=evidence,
                    sources=sources,
                    language=language,
                    presentation_style=presentation_style,
                )
                format_ok = _format_contract_satisfied(
                    rendered,
                    objective=objective,
                )
                candidate_ok, candidate_gate = _book_candidate_contract_result(
                    body,
                    objective=objective,
                    evidence=evidence,
                    allowed_candidate_titles=(
                        publication_packet.allowed_recommendation_entities
                        if publication_packet is not None
                        else None
                    ),
                    target_candidate_count=(
                        publication_packet.target_candidate_count
                        if publication_packet is not None
                        else 0
                    ),
                )
                structure_ok, structure_gate = _report_structure_contract(
                    body,
                    objective=objective,
                    evidence=evidence,
                    presentation_contract=presentation_contract,
                )
                candidate_entity_safe = not bool(
                    candidate_gate.get("unsupported_titles")
                )
                if publication_packet is not None:
                    # A planner portfolio is a research coverage target, not a
                    # knowledge allowlist for the final model. Extra model-known
                    # candidates remain visible as an alignment diagnostic and
                    # can trigger a repair, but are not a publication-safety
                    # failure. Legacy evidence-only callers keep the strict
                    # entity gate.
                    candidate_entity_safe = True
                # Safety and quality are different axes. Missing breadth,
                # imperfect citation placement, requested formatting, or a
                # semantic score below target should trigger a focused repair,
                # but must not erase an otherwise safe and useful model draft.
                publication_safe = render_ok and candidate_entity_safe
                semantic_quality = None
                if publication_safe and publication_packet is not None:
                    semantic_quality = await evaluate_semantic_answer(
                        model_id=model_id,
                        objective=objective,
                        packet=publication_packet,
                        answer=body,
                    )
                answer_quality_ok = (
                    semantic_quality.passed
                    if semantic_quality is not None and semantic_quality.available
                    else structure_ok
                )
                ok = (
                    publication_safe
                    and format_ok
                    and candidate_ok
                    and answer_quality_ok
                )
            else:
                rendered, cited_ids, ok = "", [], False
                render_ok = format_ok = candidate_ok = structure_ok = False
                candidate_entity_safe = False
                publication_safe = answer_quality_ok = False
                candidate_gate = {
                    "emitted_titles": [],
                    "unsupported_titles": [],
                    "uncited_titles": [],
                }
                structure_gate = {"reasons": ["empty_body"]}
                semantic_quality = None
            if not final_text.strip() and thinking_text.strip():
                gate_error = "thinking_only_response"
            elif contains_internal_reasoning(final_text) and not body:
                gate_error = "internal_reasoning_detected"
            elif stream_timed_out and not ok:
                gate_error = "TimeoutError"
            else:
                gate_error = "" if ok else "report_publication_gate_rejected"
            attempt = {
                "model_id": model_id,
                "thinking": attempt_thinking_mode,
                "ok": ok,
                "error": gate_error,
                "duration_ms": int((time.perf_counter() - call_started) * 1000),
                "body_chars": len(body or ""),
                "report_chars": len(rendered or ""),
                "stream_timed_out": stream_timed_out,
                "gate_checks": {
                    "render": render_ok,
                    "format": format_ok,
                    "verified_candidates": candidate_ok,
                    "candidate_entity_safe": candidate_entity_safe,
                    "candidate_portfolio_aligned": not bool(
                        candidate_gate.get("unsupported_titles")
                    ),
                    "structure": structure_ok,
                    "publication_safe": publication_safe,
                    "answer_quality": answer_quality_ok,
                },
                "candidate_gate": candidate_gate,
                "structure_gate": structure_gate,
                "semantic_quality": (
                    semantic_quality.model_dump(mode="json")
                    if semantic_quality is not None
                    else None
                ),
                "canonicalized_titles": canonicalized_titles,
            }
            attempts.append(attempt)
            if publication_safe:
                safe_score = (
                    (100 if answer_quality_ok else 0)
                    + (30 if candidate_ok else 0)
                    + (15 if format_ok else 0)
                    + (10 if structure_ok else 0)
                    + min(len(body), 9000) // 300
                )
                if safe_score > best_safe_score:
                    best_safe_score = safe_score
                    best_safe_result = (
                        rendered,
                        cited_ids,
                        response,
                        body,
                        attempt,
                    )
            previous_error = gate_error
            previous_feedback = _report_repair_feedback(
                render_ok=render_ok,
                format_ok=format_ok,
                candidate_gate=candidate_gate,
                structure_gate=structure_gate,
                semantic_quality=(
                    semantic_quality.model_dump(mode="json")
                    if semantic_quality is not None
                    else None
                ),
            )
            await report_model_completion(
                response,
                title="\u7814\u7a76\u62a5\u544a\u6a21\u578b\u8c03\u7528\u5b8c\u6210",
                detail=(
                    "\u7814\u7a76\u62a5\u544a\u8349\u7a3f\u5df2\u901a\u8fc7\u53d1\u5e03\u68c0\u67e5"
                    if ok
                    else (
                        "\u8349\u7a3f\u5df2\u5b89\u5168\u4fdd\u7559\uff0c\u6b63\u5728\u9488\u5bf9\u672a\u8fbe\u6807\u8d28\u91cf\u9879\u5c40\u90e8\u4fee\u8ba2"
                        if publication_safe
                        else "\u8349\u7a3f\u5305\u542b\u4e0d\u53ef\u53d1\u5e03\u5185\u5bb9\uff0c\u6b63\u5728\u91cd\u65b0\u751f\u6210"
                    )
                ),
                model_name=model_id,
                duration_ms=attempt["duration_ms"],
                status=("completed" if (ok or publication_safe) else "failed"),
                error=gate_error or None,
                step_id=step_id,
            )
            if ok:
                return rendered, cited_ids, response, body, attempts
            if attempt_index == 0:
                logger.warning(
                    "report_writer gate rejected draft, retrying once model=%s error=%s",
                    model_id,
                    gate_error,
                )
                continue
        except Exception as exc:
            await report_model_completion(
                response,
                title="\u7814\u7a76\u62a5\u544a\u6a21\u578b\u8c03\u7528\u5b8c\u6210",
                detail="\u7814\u7a76\u62a5\u544a\u6a21\u578b\u8c03\u7528\u5931\u8d25",
                model_name=model_id,
                duration_ms=int((time.perf_counter() - call_started) * 1000),
                status="failed",
                error=str(exc) or exc.__class__.__name__,
                step_id=step_id,
            )
            attempt = {
                "model_id": model_id,
                "thinking": (
                    thinking_mode
                    if attempt_index == 0
                    else False
                ),
                "ok": False,
                "error": (str(exc) or exc.__class__.__name__)[:200],
                "duration_ms": int((time.perf_counter() - call_started) * 1000),
                "body_chars": 0,
                "report_chars": 0,
            }
            attempts.append(attempt)
            previous_error = attempt["error"]
            if attempt_index == 0 and _retryable_report_exception(exc):
                logger.warning(
                    "report_writer attempt failed, retrying once model=%s error=%s",
                    model_id,
                    attempt["error"],
                )
                continue
            break
    if best_safe_result is not None:
        rendered, cited_ids, response, body, source_attempt = best_safe_result
        accepted = {
            **source_attempt,
            "ok": True,
            "error": "",
            "accepted_with_quality_warning": True,
        }
        if attempts and attempts[-1] is source_attempt:
            attempts[-1] = accepted
        else:
            attempts.append(accepted)
        return rendered, cited_ids, response, body, attempts
    return "", [], last_response, last_body, attempts


async def _invoke_report_model(
    model,
    prompt: str,
    *,
    timeout_seconds: float,
) -> tuple[Any, bool]:
    """Collect streaming output and retain safe partial text on timeout."""

    # Prefer a single bounded completion for publication. Some compatible
    # streaming clients block while emitting reasoning-only chunks and do not
    # promptly honor cancellation, turning a nominal 60-second gate into a
    # multi-minute stall. Non-stream invocation returned by the same provider
    # obeys the timeout and keeps final/reasoning channels separable.
    if hasattr(model, "ainvoke"):
        async with asyncio.timeout(timeout_seconds):
            return await model.ainvoke(prompt), False

    response = None
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in model.astream(prompt):
                response = chunk if response is None else response + chunk
    except TimeoutError:
        if response is None:
            raise
        return response, True
    if response is None:
        return type("EmptyReportResponse", (), {"content": ""})(), False
    return response, False


def _compact_report_retry_prompt(prompt: str) -> str:
    """Bound a timeout retry to the highest-ranked evidence already supplied."""

    text = str(prompt or "")
    for material_heading, source_heading in (
        ("已整理研究成果：\n", "\n\n来源：\n"),
        ("Organized research material:\n", "\n\nSources:\n"),
    ):
        if material_heading not in text or source_heading not in text:
            continue
        prefix, remainder = text.split(material_heading, 1)
        material, sources = remainder.split(source_heading, 1)
        material_lines = material.splitlines()[:REPORT_RETRY_EVIDENCE_LINES]
        source_lines = sources.splitlines()[:REPORT_RETRY_EVIDENCE_LINES]
        return (
            prefix
            + material_heading
            + "\n".join(material_lines)
            + source_heading
            + "\n".join(source_lines)
        )
    return text


def _safe_final_report_candidate(text: str) -> str:
    """Keep clean final text, or an explicitly delimited clean final segment."""

    raw = str(text or "").strip()
    if not raw:
        return ""
    if not contains_internal_reasoning(raw):
        return raw
    if not re.search(
        r"(?:最终(?:报告|答案)|(?:报告|正文|答案)如下|"
        r"Final\s+(?:report|answer)|Here\s+is\s+the\s+report)",
        raw,
        re.IGNORECASE,
    ):
        return ""
    extracted = _extract_report_body(raw)
    if (
        extracted
        and extracted != raw
        and not contains_internal_reasoning(extracted)
    ):
        return extracted
    return ""


def _format_contract_satisfied(text: str, *, objective: str) -> bool:
    """Validate only explicit, mechanically checkable user format requests."""

    return table_contract_satisfied(text, request=objective)


def _repeated_substantive_blocks(body: str) -> list[str]:
    """Find duplicated prose blocks without penalizing cross-section references.

    A recommendation may legitimately name the same candidate in the opening,
    comparison table and route.  What makes a report noisy is repeating the
    same explanatory paragraph, not repeating the entity name.
    """

    fingerprints: dict[str, tuple[int, str]] = {}
    for block in re.split(r"\n\s*\n", str(body or "")):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or any(line.startswith("|") for line in lines):
            continue
        prose = " ".join(
            re.sub(r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", "", line)
            for line in lines
            if not line.startswith("#")
        ).strip()
        if len(re.sub(r"\s+", "", prose)) < 60:
            continue
        normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", prose.casefold())
        if len(normalized) < 50:
            continue
        count, sample = fingerprints.get(normalized, (0, prose[:160]))
        fingerprints[normalized] = (count + 1, sample)
    return [sample for count, sample in fingerprints.values() if count > 1]


def _normalize_recommendation_evidence_column(body: str) -> str:
    """Move row citations into the explicit Evidence column mechanically."""

    lines = str(body or "").splitlines()
    for index in range(len(lines) - 1):
        header_line = lines[index].strip()
        separator = lines[index + 1].strip()
        if not header_line.startswith("|") or not re.fullmatch(
            r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?",
            separator,
        ):
            continue
        headers = [cell.strip() for cell in header_line.strip("|").split("|")]
        evidence_index = next(
            (
                position
                for position, header in enumerate(headers)
                if re.search(r"(?:依据|证据|来源|evidence|source|citation)", header, re.IGNORECASE)
            ),
            None,
        )
        if evidence_index is None:
            return str(body or "")
        row_index = index + 2
        while row_index < len(lines) and lines[row_index].strip().startswith("|"):
            cells = [cell.strip() for cell in lines[row_index].strip().strip("|").split("|")]
            if len(cells) == len(headers) and not _CITATION_RE.search(cells[evidence_index]):
                citations = list(
                    dict.fromkeys(
                        match.group(0)
                        for position, cell in enumerate(cells)
                        if position != evidence_index
                        for match in _CITATION_RE.finditer(cell)
                    )
                )
                if citations:
                    for position in range(len(cells)):
                        if position != evidence_index:
                            cells[position] = _CITATION_RE.sub("", cells[position]).strip()
                    cells[evidence_index] = " ".join(citations)
                    lines[row_index] = "| " + " | ".join(cells) + " |"
            row_index += 1
        break
    return "\n".join(lines)


def _report_structure_contract(
    body: str,
    *,
    objective: str,
    evidence: list[dict[str, Any]],
    presentation_contract: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Reject safe drafts that violate the planned presentation contract."""

    contract = presentation_contract or build_research_presentation_contract(
        objective=objective,
        review=None,
        language=("zh-CN" if re.search(r"[\u4e00-\u9fff]", objective) else "en"),
        candidate_count=0,
    )
    recommendation_layout = contract.get("task_type") == "book_recommendation"

    reasons: list[str] = []
    heading_lines = re.findall(r"^(#{1,6})\s+(.+?)\s*$", body, re.MULTILINE)
    h2_titles = [title.strip() for marks, title in heading_lines if marks == "##"]
    opening = re.sub(r"^\s*(?:#{1,6}\s+)?", "", body).strip()[:500]
    if re.match(
        r"(?:研究目标|研究过程|证据清单|已整理研究成果|"
        r"research objective|research process|evidence inventory)",
        opening,
        flags=re.IGNORECASE,
    ):
        reasons.append("answer_does_not_lead_with_user_value")
    if recommendation_layout:
        decision_signal = re.search(
            r"(?:推荐|首选|优先|选择|取决于|如果|更适合|"
            r"recommend|pick|choose|prefer|depends|if\b|better\s+for)",
            opening,
            flags=re.IGNORECASE,
        )
        tradeoff_signal = re.search(
            r"(?:取舍|权衡|相比|区别|适合|但是|不过|"
            r"trade[- ]?off|compared|whereas|however|best\s+for)",
            body,
            flags=re.IGNORECASE,
        )
        if not decision_signal:
            reasons.append("recommendation_lacks_decision_opening")
        if not tradeoff_signal:
            reasons.append("recommendation_lacks_tradeoffs")

    route_requested = bool(contract.get("route_required"))
    if route_requested and not re.search(
        r"(?:顺序|路线|路径|先.{0,30}再|下一步|"
        r"reading\s+order|roadmap|next\s+step|first.{0,60}then)",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        reasons.append("requested_action_route_missing")

    emoji_count = len(
        re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", body)
    )

    repeated_blocks = _repeated_substantive_blocks(body)
    if recommendation_layout and repeated_blocks:
        reasons.append("candidate_descriptions_repeated")

    unsupported_precision: list[str] = []
    precision_pattern = re.compile(
        r"(?:\d+(?:\.\d+)?\s*分|"
        r"\d+\s*[-–—~至到]\s*\d+\s*(?:周|天|个月|小时)|"
        r"评分\s*\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    consensus_pattern = re.compile(
        r"(?:业界.{0,8}主流|公认|领域泰斗|最有效|必须读|必读)",
        re.IGNORECASE,
    )
    for line in body.splitlines():
        clean = line.strip()
        if not clean:
            continue
        if (
            precision_pattern.search(clean) or consensus_pattern.search(clean)
        ) and not _CITATION_RE.search(clean):
            unsupported_precision.append(clean[:160])
    if unsupported_precision:
        reasons.append("unsupported_precision_or_consensus")

    compact_chars = len(re.sub(r"\s+", "", body))
    min_chars = int(contract.get("min_body_chars") or 450)
    max_chars = int(contract.get("max_body_chars") or 3600)
    if compact_chars < min_chars:
        reasons.append("report_too_shallow")
    if compact_chars > max_chars:
        reasons.append("report_too_long")

    return not reasons, {
        "reasons": reasons,
        "h2_sections": h2_titles[:10],
        "emoji_count": emoji_count,
        "repeated_content_blocks": repeated_blocks[:5],
        "unsupported_lines": unsupported_precision[:8],
        "compact_chars": compact_chars,
        "contract_layout": contract.get("layout"),
    }


def _report_repair_feedback(
    *,
    render_ok: bool,
    format_ok: bool,
    candidate_gate: dict[str, Any],
    structure_gate: dict[str, Any],
    semantic_quality: dict[str, Any] | None = None,
) -> str:
    failures: dict[str, Any] = {}
    if not render_ok:
        failures["render"] = "include a complete answer with valid [n] citations"
    if not format_ok:
        failures["requested_format"] = "satisfy the user's explicit format"
    unsupported = list(candidate_gate.get("unsupported_titles") or [])
    uncited = list(candidate_gate.get("uncited_titles") or [])
    if unsupported:
        failures["remove_unverified_titles"] = unsupported[:10]
    if uncited:
        failures["cite_verified_titles"] = uncited[:10]
    missing_candidate_count = int(
        candidate_gate.get("missing_candidate_count") or 0
    )
    if missing_candidate_count:
        failures["restore_candidate_breadth"] = (
            f"meaningfully cover {missing_candidate_count} additional allowed "
            "candidate(s), using their distinct portfolio roles rather than filler"
        )
    structure_reasons = list(structure_gate.get("reasons") or [])
    if structure_reasons:
        failures["structure"] = structure_reasons[:12]
    unsupported_lines = list(structure_gate.get("unsupported_lines") or [])
    if unsupported_lines:
        failures["remove_or_cite_unsupported_precision"] = unsupported_lines[:5]
    if isinstance(semantic_quality, dict) and semantic_quality.get("available"):
        missing = list(semantic_quality.get("missing_user_needs") or [])
        overstated = list(
            semantic_quality.get("unsupported_or_overstated") or []
        )
        repairs = list(semantic_quality.get("repair_instructions") or [])
        if missing:
            failures["restore_missing_user_value"] = missing[:8]
        if overstated:
            failures["remove_or_qualify_overstatement"] = overstated[:8]
        if repairs:
            failures["semantic_repair"] = repairs[:8]
    if not failures:
        return " Do not include analysis, planning, self-checks, or uncited claims."
    return (
        " Correct these exact failures: "
        + json.dumps(failures, ensure_ascii=False)
        + ". Do not discuss the corrections in the answer."
    )


def _attempt_quality_reasons(attempt: dict[str, Any]) -> list[str]:
    semantic = attempt.get("semantic_quality")
    if isinstance(semantic, dict) and semantic.get("available"):
        return list(
            dict.fromkeys(
                str(item)
                for item in [
                    *(semantic.get("missing_user_needs") or []),
                    *(semantic.get("unsupported_or_overstated") or []),
                    *(semantic.get("repair_instructions") or []),
                ]
                if str(item).strip()
            )
        )[:12]
    return list(
        (attempt.get("structure_gate") or {}).get("reasons", [])
    )[:12]


def _canonicalize_verified_book_titles(
    body: str,
    *,
    objective: str,
    evidence: list[dict[str, Any]],
    allowed_candidate_titles: list[str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Rewrite only unambiguous title variants to canonical candidate titles.

    The planner owns the recommendation portfolio. External catalog rows may
    enrich those candidates, but must not become a second, narrower entity
    allowlist. When a planner portfolio is available, use its canonical names
    and accept only aliases that resolve to exactly one portfolio item.
    """

    rows = _verified_book_candidate_rows(
        objective=objective,
        evidence=evidence,
    )
    candidates: list[tuple[str, str, set[str]]] = []
    planner_titles = [
        str(title or "").strip()
        for title in (allowed_candidate_titles or [])
        if str(title or "").strip()
    ]
    source_candidates = (
        [(title, set()) for title in planner_titles]
        if planner_titles
        else [
            (
                str(row.get("candidate_title") or "").strip(),
                {
                    str(value)
                    for value in row.get("catalog_alias_keys", [])
                    if str(value)
                },
            )
            for row in rows
        ]
    )
    for canonical, source_aliases in source_candidates:
        key = normalize_candidate_title(canonical)
        if not canonical or not key:
            continue
        aliases = _candidate_title_alias_keys(canonical) | source_aliases | {key}
        candidates.append((canonical, key, aliases))
    if not candidates:
        return body, []

    seed_keys = {
        normalize_candidate_title(title)
        for title in re.findall(r"《([^》]{1,100})》", objective)
        if normalize_candidate_title(title)
    }
    rewrites: list[dict[str, str]] = []

    def canonical_title(match: re.Match[str]) -> str:
        emitted = " ".join(match.group(1).split()).strip()
        emitted_key = normalize_candidate_title(emitted)
        if not emitted_key or emitted_key in seed_keys:
            return match.group(0)
        exact = [
            canonical
            for canonical, _key, aliases in candidates
            if emitted_key in aliases
        ]
        target = exact[0] if len(set(exact)) == 1 else ""
        if not target and len(emitted_key) >= 8:
            scored: list[tuple[float, str]] = []
            for canonical, key, aliases in candidates:
                score = max(
                    SequenceMatcher(None, emitted_key, alias).ratio()
                    for alias in aliases | {key}
                )
                scored.append((score, canonical))
            scored.sort(reverse=True)
            best_score, best_title = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            if best_score >= 0.72 and best_score - runner_up >= 0.12:
                target = best_title
        if not target or normalize_candidate_title(target) == emitted_key:
            return match.group(0)
        rewrites.append({"from": emitted[:100], "to": target[:100]})
        return f"《{target}》"

    rendered = re.sub(r"《([^》]{1,100})》", canonical_title, str(body or ""))
    unique_rewrites = list(
        {
            (item["from"], item["to"]): item
            for item in rewrites
        }.values()
    )
    return rendered, unique_rewrites[:20]


def _candidate_title_alias_keys(title: str) -> set[str]:
    """Build conservative, exact aliases for one candidate title.

    This intentionally avoids semantic prefix matching. It covers the two
    common forms produced by report models: an author suffix used only for
    disambiguation, and an unambiguous English initialism inside an otherwise
    preserved title. Every alias must still resolve to exactly one candidate.
    """

    raw = " ".join(str(title or "").split()).strip()
    if not raw:
        return set()
    variants = {raw}
    author_suffix = re.sub(
        r"\s*[（(]\s*(?:[\u4e00-\u9fff]{2,4}|[A-Z][A-Za-z.''-]+"
        r"(?:\s+[A-Z][A-Za-z.''-]+){1,3})\s*[）)]\s*$",
        "",
        raw,
    ).strip()
    if author_suffix and author_suffix != raw:
        variants.add(author_suffix)

    # A long primary title followed by “with …” is commonly shortened in
    # prose. Refuse generic one- or two-word prefixes.
    for value in list(variants):
        primary = re.split(r"\s+(?:with)\s+|\s*[:：—–]\s*", value, maxsplit=1)[0]
        if len(re.findall(r"[A-Za-z][A-Za-z''-]*", primary)) >= 3:
            variants.add(primary.strip())

    aliases: set[str] = set()
    for value in variants:
        normalized = normalize_candidate_title(value)
        if normalized:
            aliases.add(normalized)
        words = list(re.finditer(r"[A-Za-z][A-Za-z''-]*", value))
        for index in range(len(words) - 1):
            first, second = words[index], words[index + 1]
            if len(first.group(0)) < 2 or len(second.group(0)) < 2:
                continue
            acronym = first.group(0)[0] + second.group(0)[0]
            abbreviated = value[: first.start()] + acronym + value[second.end() :]
            alias = normalize_candidate_title(abbreviated)
            if alias:
                aliases.add(alias)
    return aliases


def _book_candidate_contract_satisfied(
    body: str,
    *,
    objective: str,
    evidence: list[dict[str, Any]],
) -> bool:
    ok, _details = _book_candidate_contract_result(
        body,
        objective=objective,
        evidence=evidence,
    )
    return ok


def _book_candidate_contract_result(
    body: str,
    *,
    objective: str,
    evidence: list[dict[str, Any]],
    allowed_candidate_titles: list[str] | None = None,
    target_candidate_count: int = 0,
) -> tuple[bool, dict[str, Any]]:
    if not _book_recommendation_requested(objective):
        return True, {
            "emitted_titles": [],
            "unsupported_titles": [],
            "uncited_titles": [],
        }
    seed_titles = {
        normalize_candidate_title(title)
        for title in re.findall(r"《([^》]{1,100})》", objective)
        if normalize_book_work_title(title)
    }
    evidence_number_by_id = {
        str(item.get("source_id") or ""): index
        for index, item in enumerate(evidence, start=1)
    }
    evidence_numbers: dict[str, set[int]] = {}
    for candidate in _verified_book_candidate_rows(
        objective=objective,
        evidence=evidence,
    ):
        normalized = normalize_candidate_title(
            candidate.get("candidate_title")
        )
        if not normalized:
            continue
        allowed_numbers = {
            evidence_number_by_id[source_id]
            for source_id in candidate.get("bound_source_ids", [])
            if source_id in evidence_number_by_id
        }
        # A catalog record can state an explicit translated/original title in
        # its own claim. Treat those bracketed spellings as aliases of the same
        # verified entity, without importing titles from discovery articles.
        alias_keys = {
            str(value)
            for value in candidate.get("catalog_alias_keys", [])
            if str(value)
        } | {normalized}
        for alias_key in alias_keys:
            evidence_numbers.setdefault(alias_key, set()).update(allowed_numbers)
    allowed_numbers_by_title = evidence_numbers
    allowlist_provided = allowed_candidate_titles is not None
    allowed_title_keys = {
        normalize_candidate_title(title)
        for title in (allowed_candidate_titles or [])
        if normalize_book_work_title(title)
    }
    if not allowlist_provided:
        allowed_title_keys = set(allowed_numbers_by_title)
    emitted = [
        (match, normalize_candidate_title(match.group(1)))
        for match in re.finditer(r"《([^》]{1,100})》", body)
    ]
    candidates = [
        (match, normalized)
        for match, normalized in emitted
        if normalized and normalized not in seed_titles
    ]
    # Candidate-count coverage is a research-quality signal, not a publication
    # kill switch. A planner-owned portfolio is likewise a coverage target, not
    # an allowlist over the final model''s stable knowledge.
    if not candidates:
        return False, {
            "emitted_titles": [],
            "unsupported_titles": [],
            "uncited_titles": [],
        }
    occurrences_by_title: dict[str, list[re.Match[str]]] = {}
    display_by_title: dict[str, str] = {}
    for match, normalized in candidates:
        occurrences_by_title.setdefault(normalized, []).append(match)
        display_by_title.setdefault(normalized, match.group(1))
    unsupported_titles: list[str] = []
    uncited_titles: list[str] = []
    for normalized, occurrences in occurrences_by_title.items():
        if allowed_title_keys and normalized not in allowed_title_keys:
            unsupported_titles.append(display_by_title[normalized])
            continue
        allowed_numbers = allowed_numbers_by_title.get(normalized, set())
        # A planner-owned candidate may be recommended from stable model
        # knowledge even when this run found no external page for it. Only
        # externally sourced facts need a bound citation.
        if not allowed_numbers:
            continue
        # A verified title is often repeated in the opening recommendation,
        # a comparison table, and the reading route. Requiring a citation next
        # to every repetition rejects otherwise sound reports. Keep the hard
        # entity allow-list, but require at least one bound citation for each
        # recommended title somewhere it is actually discussed.
        title_is_cited = False
        for match in occurrences:
            window = body[
                max(0, match.start() - 120):
                min(len(body), match.end() + 480)
            ]
            cited_numbers = {
                int(value)
                for value in _CITATION_RE.findall(window)
            }
            if allowed_numbers.intersection(cited_numbers):
                title_is_cited = True
                break
        if not title_is_cited:
            uncited_titles.append(display_by_title[normalized])
    details = {
        "emitted_titles": list(display_by_title.values())[:20],
        "unsupported_titles": unsupported_titles[:20],
        "uncited_titles": uncited_titles[:20],
        "target_candidate_count": max(0, int(target_candidate_count or 0)),
        "emitted_candidate_count": len(occurrences_by_title),
        "missing_candidate_count": max(
            0,
            int(target_candidate_count or 0) - len(occurrences_by_title),
        ),
        "portfolio_alignment_advisory": allowlist_provided,
        "portfolio_alignment_pass": not unsupported_titles,
        "external_citation_alignment_pass": not uncited_titles,
    }
    if allowlist_provided:
        # The final model may add a useful candidate that the planning model did
        # not select. Preserve the diagnostic, but judge this contract on the
        # requested breadth. External evidence availability must not suppress
        # model knowledge.
        return (not details["missing_candidate_count"]), details
    return (
        not unsupported_titles
        and not uncited_titles
        and not details["missing_candidate_count"]
    ), details


def _ensure_verified_candidate_coverage(
    body: str,
    *,
    objective: str,
    evidence: list[dict[str, Any]],
) -> str:
    """Append omitted verified candidates without replacing the editor's prose."""

    # Do not let deterministic completion rescue a wholly uncited model draft;
    # the normal retry/fallback path must still handle that failure.
    if not _CITATION_RE.search(str(body or "")):
        return body
    rows = _verified_book_candidate_rows(
        objective=objective,
        evidence=evidence,
    )
    emitted = {
        normalize_book_work_title(title)
        for title in re.findall(r"《([^》]{1,100})》", str(body or ""))
        if normalize_book_work_title(title)
    }
    evidence_number_by_id = {
        str(item.get("source_id") or ""): index
        for index, item in enumerate(evidence, start=1)
    }
    additions: list[str] = []
    for row in rows:
        title = str(row.get("candidate_title") or "").strip()
        key = normalize_book_work_title(title)
        if not key or key in emitted:
            continue
        citation = next(
            (
                evidence_number_by_id[source_id]
                for source_id in row.get("bound_source_ids", [])
                if source_id in evidence_number_by_id
            ),
            None,
        )
        if citation is None:
            continue
        claim = _dedupe_claim_fragments(str(row.get("claim") or "").strip())
        additions.append(f"- **《{title}》**：{claim} [{citation}]")
        emitted.add(key)
    if not additions:
        return body
    heading = "### 其他已核验候选" if re.search(r"[\u4e00-\u9fff]", objective) else "### Other verified candidates"
    return f"{body.rstrip()}\n\n{heading}\n\n" + "\n".join(additions)


def _book_recommendation_requested(objective: str) -> bool:
    return is_book_recommendation_request(objective)


def verified_book_candidate_titles(
    report: ResearchReport | dict[str, Any],
) -> list[str]:
    validated = ResearchReport.model_validate(report)
    return [
        str(item.get("candidate_title") or "")
        for item in _verified_book_candidate_rows(
            objective=validated.objective,
            evidence=_evidence_list(validated, limit=MAX_EVIDENCE),
        )
        if str(item.get("candidate_title") or "")
    ]


def _published_book_titles(body: str, *, objective: str) -> list[str]:
    if not _book_recommendation_requested(objective):
        return []
    seed_keys = {
        normalize_candidate_title(title)
        for title in re.findall(r"《([^》]{1,100})》", objective)
        if normalize_candidate_title(title)
    }
    titles: list[str] = []
    seen: set[str] = set()
    for title in re.findall(r"《([^》]{1,100})》", str(body or "")):
        display = " ".join(title.split()).strip()
        key = normalize_candidate_title(display)
        if not key or key in seed_keys or key in seen:
            continue
        seen.add(key)
        titles.append(display)
    return titles


def _verified_book_candidate_rows(
    *,
    objective: str,
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project evidence into verified book entities, never source articles.

    Editorial pages are useful for discovery, but only a recognized catalog
    entity can own a final book row.  Other evidence may support that row only
    when it explicitly names the exact same work.
    """

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence:
        title = catalog_book_title(
            item.get("source_title"),
            source_url=item.get("source_url"),
        )
        key = normalize_candidate_title(title)
        if not key or key in seen:
            continue
        candidate_alias_keys = _evidence_candidate_keys(item) | {key}
        bound = [
            other
            for other in evidence
            if candidate_alias_keys.intersection(
                _evidence_candidate_keys(other)
            )
        ]
        if not bound:
            bound = [item]
        if not candidate_topic_supported(
            objective=objective,
            candidate_title=title,
            evidence_texts=[
                value
                for other in bound
                for value in (
                    str(other.get("source_title") or ""),
                    str(other.get("claim") or ""),
                )
            ],
        ):
            continue
        row = dict(item)
        row["candidate_title"] = title
        row["catalog_alias_keys"] = sorted(candidate_alias_keys)
        row["claim"] = _best_candidate_claim(
            objective=objective,
            candidate_key=key,
            primary=item,
            bound=bound,
        )
        row["bound_source_ids"] = list(
            dict.fromkeys(
                str(other.get("source_id") or "")
                for other in bound
                if str(other.get("source_id") or "")
            )
        )
        seen.add(key)
        candidates.append(row)
    ranked = _rank_verified_book_candidates(
        candidates,
        objective=objective,
    )
    requested_count = requested_book_count(objective)
    return ranked[:requested_count] if requested_count > 0 else ranked


def _candidate_bound_publication_evidence(
    evidence: list[dict[str, Any]],
    *,
    candidate_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only evidence explicitly bound to verified recommendation entities."""

    allowed_ids = {
        str(source_id)
        for row in candidate_rows
        for source_id in (
            row.get("bound_source_ids")
            or [row.get("source_id")]
        )
        if source_id
    }
    selected = [
        item
        for item in evidence
        if str(item.get("source_id") or "") in allowed_ids
    ]
    return selected or evidence


def _evidence_candidate_keys(item: dict[str, Any]) -> set[str]:
    keys = {
        normalize_candidate_title(title)
        for title in re.findall(
            r"《([^》]{1,100})》",
            str(item.get("claim") or ""),
        )
        if normalize_candidate_title(title)
    }
    catalog_title = catalog_book_title(
        item.get("source_title"),
        source_url=item.get("source_url"),
    )
    if catalog_title and (key := normalize_candidate_title(catalog_title)):
        keys.add(key)
    return keys


def _best_candidate_claim(
    *,
    objective: str,
    candidate_key: str,
    primary: dict[str, Any],
    bound: list[dict[str, Any]],
) -> str:
    ordered = [primary, *(item for item in bound if item is not primary)]
    fallback = str(primary.get("claim") or "").strip()
    ranked: list[tuple[int, int, int, int, str]] = []
    for index, item in enumerate(ordered):
        claim = str(item.get("claim") or "").strip()
        if not claim or candidate_key not in _evidence_candidate_keys(item):
            continue
        topic_supported = candidate_topic_supported(
            objective=objective,
            candidate_title="",
            evidence_texts=[claim],
        )
        semantic_markers = sum(
            marker in claim.casefold()
            for marker in (
                "介绍",
                "讲解",
                "涵盖",
                "内容",
                "适合",
                "入门",
                "实践",
                "方法",
                "主题",
                "基础",
                "recommend",
                "beginner",
            )
        )
        catalog_owner = (
            normalize_candidate_title(
                catalog_book_title(
                    item.get("source_title"),
                    source_url=item.get("source_url"),
                )
            )
            == candidate_key
        )
        ranked.append(
            (
                1 if catalog_owner else 0,
                1 if topic_supported else 0,
                semantic_markers,
                min(len(claim), 400) - index,
                claim,
            )
        )
        if not fallback:
            fallback = claim
    selected = max(ranked, default=(0, 0, 0, 0, fallback))[4]
    return _dedupe_claim_fragments(selected)


def _rank_verified_book_candidates(
    candidates: list[dict[str, Any]],
    *,
    objective: str,
) -> list[dict[str, Any]]:
    beginner_focus = bool(
        re.search(
            r"(?:零基础|初学者|新手|入门|beginner|from\s+scratch)",
            str(objective or ""),
            flags=re.IGNORECASE,
        )
    )

    def score(index_and_item: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index, item = index_and_item
        title = str(item.get("candidate_title") or "")
        claim = str(item.get("claim") or "")
        text = f"{title} {claim}".casefold()
        value = 0
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

    return [
        item
        for _, item in sorted(
            enumerate(candidates),
            key=score,
            reverse=True,
        )
    ]


def _dedupe_claim_fragments(claim: str) -> str:
    fragments: list[str] = []
    seen: set[str] = set()
    for fragment in re.split(r"；+", str(claim or "")):
        clean = fragment.strip(" ；;，,。")
        key = re.sub(r"[^\w\u4e00-\u9fff]+", "", clean).casefold()
        if not clean or not key or key in seen:
            continue
        seen.add(key)
        fragments.append(clean)
    return "；".join(fragments)[:600]


def _retryable_report_exception(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".casefold()
    if any(
        marker in text
        for marker in (
            "401",
            "403",
            "authentication",
            "permission",
            "not found",
            "invalid request",
            "unsupported",
            "bad request",
        )
    ):
        return False
    return any(
        marker in text
        for marker in (
            "429",
            "rate limit",
            "timeout",
            "temporar",
            "connection",
            "network",
            "502",
            "503",
            "504",
        )
    )


def _render_freeform(
    body: str,
    *,
    objective: str,
    evidence: list[dict[str, Any]],
    language: str,
    presentation_style: str = "conversational",
    sources: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], bool]:
    """Freeform narrative path: extract the report with a minimal usability gate.

    Content quality belongs to the LLM; this only guards against empty or
    placeholder-only extractions so a good narrative is never discarded.
    """

    extracted = _extract_report_body(body)
    if not _looks_like_report(extracted):
        return "", [], False
    rendered, cited_ids = _sanitize_body(
        extracted,
        evidence=evidence,
        sources=sources,
        language=language,
        presentation_style=presentation_style,
    )
    return rendered, cited_ids, bool(
        rendered.strip() and (not evidence or cited_ids)
    )


def _render_final_deliverable(
    body: str,
    *,
    evidence: list[dict[str, Any]],
    language: str,
    presentation_style: str = "conversational",
    sources: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], bool]:
    """Clean the model's final answer directly (no thinking extraction)."""

    if not _looks_like_report(body):
        return "", [], False
    rendered, cited_ids = _sanitize_body(
        body,
        evidence=evidence,
        sources=sources,
        language=language,
        presentation_style=presentation_style,
    )
    return rendered, cited_ids, bool(
        rendered.strip() and (not evidence or cited_ids)
    )


MIN_USABLE_BODY_CHARS = 80


_PLACEHOLDER_BODIES = {
    "...",
    "...",
    "\u5f85\u8865\u5145",
    "todo",
    "tbd",
}


_META_TAIL_MARKERS = (
    "\u68c0\u67e5\u7ea6\u675f",
    "\u68c0\u67e5\u7ea6\u675f\u6761\u4ef6",
    "\u4fee\u6539\u540e\u7684\u6587\u672c",
    "\u7a0d\u4f5c\u4f18\u5316",
    "\u5fae\u8c03\u6587\u6848",
    "\u5fae\u8c03\u8bed\u8a00",
    "\u601d\u8003\u7ed3\u6784",
    "\u6700\u7ec8\u6587\u672c\u7ed3\u6784",
    "\u4ed4\u7ec6\u68c0\u67e5\u6bb5\u843d",
    "\u786e\u8ba4\u5f15\u7528\u6807\u8bb0",
    "\u6700\u540e\u68c0\u67e5",
    "\u6536\u5c3e\u68c0\u67e5",
    "\u68c0\u67e5\u5b8c\u6bd5",
    "\u76f4\u63a5\u8f93\u51fa",
    "\u5b8c\u6210\u3002",
    "Constraint check",
    "Revised",
    "Final answer",
)


_REPORT_ANCHORS = (
    "\u8d77\u8349\u62a5\u544a",
    "\u8d77\u8349\u5185\u5bb9",
    "\u8349\u7a3f",
    "\u62a5\u544a\u5982\u4e0b",
    "\u6700\u7ec8\u62a5\u544a",
    "\u4ee5\u4e0b\u662f\u62a5\u544a",
    "\u8f93\u51fa\u62a5\u544a",
    "\u6b63\u6587\u5982\u4e0b",
    "\u6b63\u6587\uff1a",
    "\u6700\u7ec8\u7b54\u6848",
    "\u7b54\u6848\u5982\u4e0b",
    "Final report",
    "Final answer",
    "Here is the report",
    "Report:",
)


def classify_report_failure(
    attempts: list[dict[str, Any]],
    *,
    error: str = "",
) -> str:
    """Classify the dominant reason the LLM path fell back."""

    if any(bool(item.get("ok")) for item in attempts):
        return "success"
    if not attempts:
        return "no_model"
    errors = " ".join(
        str(item.get("error") or "") for item in attempts
    ).lower()
    if any(
        token in errors
        for token in ("rate", "429", "quota", "\u989d\u5ea6", "limit exceeded")
    ):
        return "rate_limited"
    if any(token in errors for token in ("timeout", "timed out")):
        return "timeout"
    if any(token in errors for token in ("thinking", "enable_thinking")):
        return "model_config"
    if any(
        token in errors
        for token in ("not found", "auth", "401", "403", "api key")
    ):
        return "model_unavailable"
    if not any(int(item.get("body_chars") or 0) > 0 for item in attempts):
        return "empty_output"
    return "gate_rejected"


def _extract_report_body(text: str) -> str:
    """Slice the actual report out of thinking/planning drafts.

    Thinking-only models put the finished report inside their reasoning
    channel after a drafting marker. Prefer the anchored segment, then the
    last heading-led block (markdown or Chinese-numbered), and never drop
    the whole output when no heading structure exists.
    """

    source = str(text or "")
    if not source.strip():
        return ""
    for anchor in _REPORT_ANCHORS:
        index = source.find(anchor)
        if index < 0:
            continue
        tail_lines = source[index:].splitlines()
        for offset, line in enumerate(tail_lines[1:], start=1):
            if line.strip():
                return _cut_meta_tail(
                    "\n".join(tail_lines[offset:]).strip()
                )
    lines = source.splitlines()
    strong_start = None
    weak_start = None
    for index, line in enumerate(lines):
        if _strong_heading(line):
            strong_start = index
        elif _weak_heading(line):
            weak_start = index
    start = strong_start if strong_start is not None else weak_start
    if start is not None:
        rest = lines[start:]
        if sum(1 for line in rest if line.strip()) >= 3:
            return _cut_meta_tail("\n".join(rest).strip())
    return _cut_meta_tail(source.strip())


def _heading_line(line: str) -> bool:
    """Markdown or Chinese-numbered section heading line."""

    return _strong_heading(line) or _weak_heading(line)


def _strong_heading(line: str) -> bool:
    """Markdown or Chinese-numbered headings ? reliable section markers."""

    stripped = str(line or "").strip()
    if stripped.startswith(("# ", "## ", "### ")):
        return True
    for prefix in (
        "\u4e00\u3001",
        "\u4e8c\u3001",
        "\u4e09\u3001",
        "\u56db\u3001",
        "\u4e94\u3001",
        "\u516d\u3001",
        "\u4e03\u3001",
        "\u516b\u3001",
        "\u4e5d\u3001",
        "\u5341\u3001",
        "\uff08\u4e00\uff09",
        "\uff08\u4e8c\uff09",
        "\uff08\u4e09\uff09",
        "\uff08\u56db\uff09",
        "\uff08\u4e94\uff09",
    ):
        if stripped.startswith(prefix):
            return True
    return False


def _weak_heading(line: str) -> bool:
    """Plain numeric list-style heading ("1. xxx") ? fallback only."""

    stripped = str(line or "").strip()
    return (
        len(stripped) >= 3
        and stripped[0].isdigit()
        and stripped[1] in ".\u3001"
    )


def _cut_meta_tail(text: str) -> str:
    """Drop planning/self-check/closing notes trailing the report body."""

    lines = str(text or "").splitlines()
    cut = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if any(marker in stripped for marker in _META_TAIL_MARKERS):
            cut = index
            break
    return "\n".join(lines[:cut]).strip()


def _looks_like_report(text: str) -> bool:
    """Minimal usability gate: real content, not placeholder-only.

    Deliberately lenient: quality is judged by the LLM; this only blocks
    empty stubs and planning fragments from being published.
    """

    compact = " ".join(str(text or "").split())
    if len(compact) < MIN_USABLE_BODY_CHARS:
        return False
    if contains_internal_reasoning(text):
        return False
    return compact.casefold() not in _PLACEHOLDER_BODIES


def _extract_json_objects(text: str) -> list[str]:
    """Split balanced top-level JSON objects (string-aware)."""

    cleaned = str(text or "")
    objects: list[str] = []
    index = 0
    size = len(cleaned)
    while index < size:
        if cleaned[index] != "{":
            index += 1
            continue
        depth = 0
        in_string = False
        escaped = False
        cursor = index
        while cursor < size:
            char = cleaned[cursor]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            else:
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        objects.append(cleaned[index : cursor + 1])
                        break
            cursor += 1
        index = cursor + 1
    return objects


def _bounded(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text if len(text) <= limit else text[:limit].rstrip()


_FINAL_REPORT_INSTRUCTIONS_ZH = '''你是深度研究报告的主编。研究已经完成，请把模型的专业判断与外部研究材料综合成一份让用户能够做决定的最终交付。

写作标准：
1. 开头用 2—4 句话直接给出结论、首要选择依据，以及什么条件会改变这个结论。
2. 一个信息只完整表达一次。不要把同一对象依次写成独立章节、对比表和路线说明三遍；路线中只写它承担的阶段角色。
3. 比较多个对象时围绕真正改变选择的维度组织；只有表格明显更清楚时才使用一个主表，不为追求结构而堆砌字段。
4. 最终建议必须说明取舍：适合什么目标、解决什么问题、主要代价或边界是什么。判断可以有主见，但必须写成基于材料的编辑判断，不冒充行业共识。
5. 不编造精确学习时长、评分、排名、版本、作者身份、行业地位或“必读/最佳/最有效”等结论。材料没有明确支持时就不写。
6. 每个可核验事实在句末使用对应 [n]；表格中每一行也要有直接支持该对象的引用。
7. 结构服从问题：简单问题可以直接回答；复杂问题使用足以承载论证的清晰层级，并按需要选择标题、分组、要点、对比表、行动路线或总结，不要求机械齐全，也不要为每个对象重复同一套章节。
8. 直接输出 Markdown 成品，不输出分析、规划、自检、修订说明、任务复述或系统术语。'''


_FINAL_REPORT_INSTRUCTIONS_EN = '''You are the chief editor of a deep-research deliverable. The research is complete; synthesize the model's domain judgment with the external research into an answer that helps the user decide.

Editorial standard:
1. Open with two to four sentences stating the conclusion, the decisive criterion, and the condition that would change the conclusion.
2. Fully express each point once. Do not repeat every object as a standalone section, comparison-table row, and route description; a route names only the role it plays.
3. Organize comparisons around dimensions that change the choice. Use one main table only when it is materially clearer, never to fill a template with irrelevant metadata.
4. Explain the trade-off behind each recommendation: goal served, problem solved, and principal cost or boundary. Editorial judgment is welcome but must not masquerade as external consensus.
5. Do not invent precise study durations, scores, rankings, editions, author status, industry status, or claims such as must-read, best, or most effective when the material does not explicitly support them.
6. Put the matching [n] after every verifiable fact; every comparison-table row must carry a source tied to that object.
7. Let the task determine the structure: answer simple questions directly; for complex tasks, use enough hierarchy to carry the argument and choose headings, groups, bullets, a comparison table, an action path, or a summary only when useful. Do not require every form or repeat the same per-object template.
8. Output only the final Markdown deliverable, never analysis, planning, self-checks, revision notes, task restatement, or system terminology.'''

_EVIDENCE_LOCKED_INSTRUCTIONS_ZH = '''你是深度研究报告编辑。直接回答用户的原始问题。发布数据包同时包含模型候选判断和外部证据，必须区分二者的认识论角色。

要求：
1. 可以基于模型的稳定领域知识形成推荐立场和解释，但不得把未经外部材料支持的作者、日期、版本、销量、评分、代码仓库状态或行业共识写成已核验事实。
2. 每个可核验事实都在句末使用支持它的 [n]；引用必须与该条材料直接对应。
3. 用户没有要求推荐或评价时，不主动增加“最值得”“不建议”“适合人群”等判断。
4. 外部证据不足时降低相关事实的确定性并说明真正影响选择的缺口；不能仅因网页材料不足就删除模型规划的合理候选。
5. 正文不写 URL；保留输入中的名称和数据；最后由系统生成来源列表。
6. 直接输出结构清晰的 Markdown 成品，不输出思考、规划、检查过程或任务复述。'''

_EVIDENCE_LOCKED_INSTRUCTIONS_EN = '''You are the editor of a deep-research report. Answer the user's original question directly. The publication packet contains both model-owned candidate judgments and external evidence; keep their epistemic roles distinct.

Requirements:
1. You may form recommendations and explanations from stable model domain knowledge, but do not present unsupported authors, dates, editions, sales, ratings, repository status, or consensus as externally verified facts.
2. Put the supporting [n] citation at the end of every verifiable factual statement, and ensure that source directly supports it.
3. Do not add recommendations or evaluative judgments unless the user asked for them.
4. Calibrate factual confidence when external evidence is sparse; do not delete a reasonable model-planned candidate merely because this run did not find a web page for it.
5. Do not put URLs in the body; preserve useful names and figures; the system appends the source list.
6. Output only a clear Markdown deliverable, never analysis, planning, checking notes, or a restatement of the task.'''

_CONVERSATIONAL_INSTRUCTIONS_ZH = '''你是一个真诚、有判断力、懂阅读体验的助手。研究过程已经结束，你只负责把已核验结果说得自然、清楚、好读。

要求：
1. 直接回应用户，开头先给自然判断，不写“研究结论”“证据限制”“研究报告”，也不提回执、校验、流程或系统。
2. 只能使用“已整理研究成果”明确支持的事实；不补写材料没有支持的作者、日期、版本、评分或因果关系。
3. 每个可核验事实在句末使用对应 [n]，引用必须与材料直接对应。
4. 推荐类回答要说明“为什么适合”，并把用户给出的示例当作参照而不是再次推荐；可用少量贴合内容的 emoji 和简短 Markdown 分组增强阅读体验。
5. 资料不足时用一两句日常语言说明，不输出技术原因、错误码、请求 ID 或内部证据术语。
6. 正文不写裸 URL；直接输出面向用户的最终回答，不输出思考、规划、检查过程或任务复述。'''

_CONVERSATIONAL_INSTRUCTIONS_EN = '''You are a warm, opinionated assistant presenting completed research in a clear, natural way.

Requirements:
1. Answer the user directly with a natural opening. Do not label the response as a research conclusion, evidence limitations, or research report, and never mention receipts, validation, runtime, or system internals.
2. Use only facts explicitly supported by the organized material. Do not add unsupported authors, dates, editions, ratings, or causal claims.
3. Put the matching [n] citation after every verifiable factual statement.
4. For recommendations, explain why each choice fits and treat the user's examples as anchors, not candidates. Use a few meaningful emoji and short Markdown sections when they improve readability.
5. If material is insufficient, explain it in one or two ordinary sentences without technical error details.
6. Do not put raw URLs in the body. Output only the final user-facing answer, never analysis, planning, checking notes, or a task restatement.'''


def build_research_presentation_contract(
    *,
    objective: str,
    review: dict[str, Any] | None,
    language: str,
    candidate_count: int,
) -> dict[str, Any]:
    """Compile semantic planning into a topic-independent output contract."""

    plan = (
        review.get("objective_plan", {})
        if isinstance(review, dict)
        and isinstance(review.get("objective_plan"), dict)
        else {}
    )
    task_type = str(plan.get("task_type") or "general_research")
    if _book_recommendation_requested(objective):
        task_type = "book_recommendation"
    depth = str(plan.get("answer_depth") or "deep")
    if depth not in {"quick", "balanced", "deep"}:
        depth = "deep"

    def clean_list(value: Any, *, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(
            dict.fromkeys(
                " ".join(str(item or "").split())[:160]
                for item in value
                if " ".join(str(item or "").split())
            )
        )[:limit]

    dimensions = clean_list(plan.get("decision_dimensions"), limit=6)
    unknowns = clean_list(plan.get("critical_unknowns"), limit=5)
    route_required = bool(
        re.search(
            r"(?:顺序|路线|路径|怎么读|如何读|学习计划|"
            r"reading\s+order|roadmap|learning\s+path|study\s+plan)",
            objective,
            flags=re.IGNORECASE,
        )
    )
    # The semantic planner and writer choose the form. This contract carries
    # user intent and safety bounds only; it never maps a book category to a
    # fixed table, section list, or comparison taxonomy.
    layout = "adaptive_user_answer"
    required_sections = ["requested_action_route"] if route_required else []
    table_columns: list[str] = []

    length_bounds = {
        "quick": (120, 1800),
        "balanced": (350, 5000),
        # Keep the generic floor moderate. Recommendation breadth raises the
        # budget below, while semantic review handles non-list research depth
        # without imposing a topic-blind word-count rule.
        "deep": (800, 9000),
    }
    min_chars, max_chars = length_bounds[depth]
    if depth == "deep" and candidate_count:
        min_chars = max(
            min_chars,
            min(4000, 1200 + max(0, int(candidate_count)) * 240),
        )
    return {
        "version": "research-presentation-v1",
        "task_type": task_type,
        "layout": layout,
        "answer_depth": depth,
        "decision_dimensions": dimensions,
        "critical_unknowns": unknowns,
        "required_sections": required_sections,
        "table_columns": table_columns,
        "route_required": route_required,
        "candidate_count": max(0, int(candidate_count)),
        "target_candidate_mentions": max(0, int(candidate_count)),
        "min_body_chars": min_chars,
        "max_body_chars": max_chars,
        "quality_rules": [
            "one_complete_expression_per_claim",
            "distinguish_editorial_judgment_from_sourced_fact",
            "cite_every_verifiable_fact",
            "no_unsupported_precision_or_consensus",
        ],
    }


def _prompt_freeform(
    *,
    objective: str,
    language: str,
    evidence: list[dict[str, Any]],
    limitations: list[str],
    review: dict[str, Any] | None = None,
    presentation_contract: dict[str, Any] | None = None,
    allowed_candidate_titles: list[str] | None = None,
    publication_packet: ResearchPublicationPacket | None = None,
) -> str:
    """Build the prompt for one job only: write the user's final answer."""

    zh = language == "zh-CN"
    objective_plan = (
        review.get("objective_plan", {})
        if isinstance(review, dict)
        and isinstance(review.get("objective_plan"), dict)
        else {}
    )
    response_style = str(
        objective_plan.get("response_style") or "conversational"
    )
    answer_depth = str(objective_plan.get("answer_depth") or "deep")
    brief = build_user_answer_brief(
        objective,
        task_type=str(objective_plan.get("task_type") or "general_research"),
        decision_dimensions=list(objective_plan.get("decision_dimensions") or []),
        critical_questions=list(objective_plan.get("critical_unknowns") or []),
        answer_depth=answer_depth,
        response_style=response_style,
    )
    packet = publication_packet or build_research_publication_packet(
        brief=brief,
        evidence=evidence,
        evidence_strategy=objective_plan.get("evidence_strategy"),
        allowed_recommendation_entities=allowed_candidate_titles,
        user_relevant_limitations=limitations,
    )
    formal_report = packet.user_answer_brief.response_style == "formal_report"
    packet_json = json.dumps(packet.model_dump(mode="json"), ensure_ascii=False)
    if zh:
        return (
            (
                _EVIDENCE_LOCKED_INSTRUCTIONS_ZH
                if formal_report
                else _FINAL_REPORT_INSTRUCTIONS_ZH
            )
            + "\n\n你的唯一职责是交付用户现在真正要读的最终答案。"
            + "不要汇报研究过程，不要把证据逐条换序复述，也不要写成系统验收件。"
            + "先形成结论，再选择最能解释结论、取舍和行动含义的证据。"
            + "对来源事实使用 [n]，可以基于多条材料做清楚标识的编辑判断。"
            + "候选组合来自模型对问题的专业判断；外部证据用于核验事实、补强理由和校准置信度，"
            + "不能因为某本书本轮外部材料较少就自动删掉它。对于没有外部支撑的候选，"
            + "仍可给出审慎的模型判断，但不要虚构评分、版本、仓库状态等可变事实。"
            + f"至少有实质内容地覆盖 {packet.target_candidate_count} 个允许候选；"
            + "每个候选必须承担不同的选择角色，不用凑数式一句话。"
            + "结构由用户问题决定；只有在表格能明显改善比较时才使用表格。"
            + "只提会改变结论、选择或行动的不确定性。"
            + "图书名称统一写成《书名》；思考、规划、自检和修改说明一律不输出。"
            + "\n\n以下是完整且唯一的发布数据包；其中不包含研究控制指令：\n"
            + packet_json
        )
    return (
        (
            _EVIDENCE_LOCKED_INSTRUCTIONS_EN
            if formal_report
            else _FINAL_REPORT_INSTRUCTIONS_EN
        )
        + "\n\nYour only job is to deliver the final answer the user should read. "
        + "Do not report the research process, reorder evidence into a digest, "
        + "or write a system acceptance artifact. Form the conclusion first, "
        + "then select the evidence that best explains its reasoning, trade-offs "
        + "and action implications. Cite sourced facts with [n]; clearly marked "
        + "editorial judgment may synthesize multiple supplied facts. Let the "
        + "model-planned candidate portfolio define recommendation breadth; external "
        + "research verifies facts, enriches reasons, and calibrates confidence but "
        + "does not automatically delete a candidate when web evidence is sparse. "
        + f"Meaningfully cover at least {packet.target_candidate_count} allowed candidates, "
        + "each with a distinct decision role rather than filler. "
        + "For candidates without external support, use calibrated model judgment and "
        + "do not invent changing facts such as ratings, editions, or repository status. "
        + "Let the "
        + "user's task determine the structure and use a table only when it "
        + "materially improves comparison. Mention only uncertainty that changes "
        + "the conclusion, choice or action. Never output reasoning, planning, "
        + "self-checks or revision notes."
        + "\n\nThis is the complete publication packet and contains no research-control instructions:\n"
        + packet_json
    )


def _all_sources(report: ResearchReport) -> list[dict[str, Any]]:
    """Full deduplicated source list for the report's Sources section."""

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in report.sources:
        url = _safe_public_source_url(source.source_url)
        if not url or url.lower() in seen:
            continue
        seen.add(url.lower())
        sources.append(
            {
                "source_id": str(source.evidence_id),
                "source_title": source.source_title,
                "source_url": url,
                "claim": source.claim,
            }
        )
        if len(sources) >= 20:
            break
    return sources


def _evidence_list(
    report: ResearchReport,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    sources_by_id = {
        str(source.evidence_id): source for source in report.sources
    }
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    evidence_by_url: dict[str, dict[str, Any]] = {}
    decisions = [
        *((decision, "verified") for decision in report.verified_claims),
        *((decision, "uncertain") for decision in report.uncertain_claims),
    ]
    for decision, evidence_status in decisions:
        if not decision.publishable:
            continue
        for evidence_id in decision.evidence_ids:
            source = sources_by_id.get(str(evidence_id))
            if source is None or str(evidence_id) in seen:
                continue
            safe_url = _safe_public_source_url(source.source_url)
            if not safe_url:
                continue
            seen.add(str(evidence_id))
            url_key = _canonical_source_url(safe_url)
            existing = evidence_by_url.get(url_key) if url_key else None
            if existing is not None:
                claim = str(decision.claim or "").strip()
                current = str(existing.get("claim") or "").strip()
                if claim and claim not in current:
                    existing["claim"] = f"{current}；{claim}"[:600]
                if evidence_status == "verified":
                    existing["evidence_status"] = "verified"
                facet = _source_metadata_value(source, "evidence_facet")
                if facet and facet not in existing["evidence_facets"]:
                    existing["evidence_facets"].append(facet)
                continue
            source_metadata = (
                source.metadata if isinstance(source.metadata, dict) else {}
            )
            facet = _source_metadata_value(source, "evidence_facet")
            candidate_title = (
                catalog_book_title(
                    source.source_title,
                    source_url=source.source_url,
                )
                if is_book_catalog_url(source.source_url)
                else _evidence_metadata_candidate_title(
                    source_title=source.source_title,
                    claim=decision.claim,
                    metadata=source_metadata,
                )
            )
            item = {
                "source_id": str(evidence_id),
                "source_title": source.source_title,
                "source_url": safe_url,
                "claim": decision.claim,
                "quality": decision.quality,
                "relevance": source.relevance,
                "research_round": source.research_round,
                "corroborated": decision.corroborated,
                "published_date": source.published_date,
                "evidence_status": evidence_status,
                "candidate_title": candidate_title,
                "evidence_facet": facet,
                "evidence_facets": [facet] if facet else [],
                "evidence_purpose": _source_metadata_value(
                    source, "evidence_purpose"
                ),
                "source_roles": _source_metadata_list(source, "source_roles"),
            }
            evidence.append(item)
            if url_key:
                evidence_by_url[url_key] = item
    quality_rank = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
    evidence.sort(
        key=lambda item: (
            1
            if _book_recommendation_requested(report.objective)
            and is_book_catalog_url(str(item.get("source_url") or ""))
            else 0,
            1 if item.get("evidence_status") == "verified" else 0,
            1 if item.get("corroborated") else 0,
            quality_rank.get(str(item.get("quality") or "").lower(), 0),
            int(item.get("relevance") or 0),
            int(item.get("research_round") or 0),
        ),
        reverse=True,
    )
    return _balanced_evidence_slice(evidence, limit=limit)


def _source_metadata_value(source, key: str) -> str:
    metadata = source.metadata if isinstance(source.metadata, dict) else {}
    value = metadata.get(key)
    if value:
        return " ".join(str(value).split()).strip()[:240]
    source_record = metadata.get("source_record")
    if isinstance(source_record, dict):
        nested = source_record.get("metadata")
        if isinstance(nested, dict) and nested.get(key):
            return " ".join(str(nested[key]).split()).strip()[:240]
    return ""


def _source_metadata_list(source, key: str) -> list[str]:
    metadata = source.metadata if isinstance(source.metadata, dict) else {}
    value = metadata.get(key)
    if value is None and isinstance(metadata.get("source_record"), dict):
        nested = metadata["source_record"].get("metadata")
        value = nested.get(key) if isinstance(nested, dict) else None
    values = value if isinstance(value, list) else [value] if value else []
    return list(
        dict.fromkeys(
            " ".join(str(item or "").split()).strip()[:100]
            for item in values
            if " ".join(str(item or "").split()).strip()
        )
    )[:8]


def _evidence_metadata_candidate_title(
    *,
    source_title: str,
    claim: str,
    metadata: dict[str, Any],
) -> str:
    values = metadata.get("evidence_candidate_titles")
    if values is None and isinstance(metadata.get("source_record"), dict):
        nested = metadata["source_record"].get("metadata")
        values = (
            nested.get("evidence_candidate_titles")
            if isinstance(nested, dict)
            else None
        )
    candidates = values if isinstance(values, list) else []
    haystack = normalize_candidate_title(f"{source_title} {claim}")
    matches = [
        " ".join(str(title or "").split()).strip()
        for title in candidates
        if normalize_candidate_title(title)
        and normalize_candidate_title(title) in haystack
    ]
    return matches[0][:160] if len(matches) == 1 else ""


def _balanced_evidence_slice(
    evidence: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Preserve candidate/facet coverage before spending remaining rank budget."""

    bounded = max(1, int(limit or 1))
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in evidence:
        key = (
            normalize_candidate_title(item.get("candidate_title")) or "_cross",
            str(item.get("evidence_facet") or "_general").casefold(),
        )
        groups.setdefault(key, []).append(item)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    depth = 0
    while len(selected) < bounded:
        added = False
        for group in groups.values():
            if depth >= len(group):
                continue
            item = group[depth]
            source_id = str(item.get("source_id") or "")
            if source_id not in selected_ids:
                selected.append(item)
                selected_ids.add(source_id)
                added = True
            if len(selected) >= bounded:
                break
        if not added:
            break
        depth += 1
    return selected


def _canonical_source_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return str(value or "").strip().casefold()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", ""))


def _safe_public_source_url(value: object) -> str:
    url = str(value or "").strip()
    if (
        not url
        or any(char.isspace() or ord(char) < 32 for char in url)
        or _INVALID_PERCENT_ESCAPE_RE.search(url)
    ):
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _limitations(report: ResearchReport) -> list[str]:
    values: list[str] = []
    limited_count = sum(1 for item in report.uncertain_claims if item.publishable)
    excluded_count = len(report.uncertain_claims) - limited_count
    if limited_count:
        values.append(
            f"{limited_count} \u6761\u6750\u6599\u8bc1\u636e\u5f3a\u5ea6\u6709\u9650\uff0c\u5df2\u6309\u7ebf\u7d22\u4fdd\u5b88\u5448\u73b0\u3002"
        )
    if excluded_count:
        values.append(
            f"{excluded_count} \u6761\u6750\u6599\u56e0\u4e0d\u53ef\u53d1\u5e03\u672a\u4f5c\u4e3a\u7ed3\u8bba\u3002"
        )
    if report.rejected_claims:
        values.append(
            f"{len(report.rejected_claims)} 条\u5019\u9009\u8bc1\u636e\u672a\u901a\u8fc7\u53d1\u5e03\u8d28\u91cf\u68c0\u67e5\u3002"
        )
    values.extend(f"\u5f85\u8865\u8bc1\u636e\uff1a{gap}" for gap in report.gaps[:3])
    if not values and not report.verified_claims:
        values.append(
            "\u73b0\u6709\u6765\u6e90\u672a\u901a\u8fc7\u76f8\u5173\u6027\u548c\u53ef\u53d1\u5e03\u6027\u68c0\u67e5\u3002"
        )
    return values[:5]


def _sanitize_body(
    body: str,
    *,
    evidence: list[dict[str, Any]],
    language: str,
    presentation_style: str = "conversational",
    sources: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str]]:
    text = _FENCE_RE.sub("", body).strip()
    # The source list is system-owned because citation numbering is bound to
    # the admitted evidence sequence. Discard any model-authored Sources block
    # before appending the canonical list below.
    text = _MODEL_SOURCES_SECTION_RE.sub("", text).rstrip("- \n")
    safe_evidence = [
        item
        for item in evidence
        if not str(item.get("source_url") or "").strip()
        or _safe_public_source_url(item.get("source_url"))
    ]
    allowed = {
        item["source_id"]: index + 1
        for index, item in enumerate(evidence)
        if not str(item.get("source_url") or "").strip()
        or _safe_public_source_url(item.get("source_url"))
    }
    allowed_by_number = {index: source_id for source_id, index in allowed.items()}
    text = _URL_RE.sub("", text)
    cited_ids: list[str] = []

    def keep_admitted_citation(match: re.Match[str]) -> str:
        source_id = allowed_by_number.get(int(match.group(1)))
        if source_id is not None and source_id not in cited_ids:
            cited_ids.append(source_id)
        return match.group(0) if source_id is not None else ""

    text = _CITATION_RE.sub(keep_admitted_citation, text)
    lines = text.splitlines()
    cleaned_lines: list[str] = []
    for line in lines:
        if line.strip().startswith(("[1]", "[2]", "[3]", "[4]", "[5]", "[6]", "[7]", "[8]", "[9]")):
            continue
        cleaned_lines.append(line)
    body_text = "\n".join(cleaned_lines).strip()
    body_text = re.sub(r"\n{3,}", "\n\n", body_text)
    source_items = sources if sources is not None else safe_evidence
    if body_text and source_items:
        zh = language == "zh-CN"
        source_lines: list[str] = []
        for index, item in enumerate(source_items, start=1):
            title = str(item.get("source_title") or item.get("source_url") or f"Source {index}")
            raw_url = str(item.get("source_url") or "").strip()
            url = _safe_public_source_url(raw_url)
            if raw_url and not url:
                continue
            line = f"{index}. [{title}]({url})" if url else f"{index}. {title}"
            source_lines.append(line)
        if presentation_style == "formal_report":
            sources_label = "\u6765\u6e90" if zh else "Sources"
            source_heading = f"## {sources_label}"
        else:
            sources_label = "\u53c2\u8003\u6765\u6e90" if zh else "Sources"
            source_heading = f"## {sources_label}"
        if source_lines:
            body_text += f"\n\n{source_heading}\n" + "\n".join(source_lines)
    return body_text, cited_ids


def _fallback_markdown(
    report: ResearchReport,
    *,
    language: str,
    presentation_style: str = "conversational",
    publication_packet: ResearchPublicationPacket | None = None,
) -> str:
    synthesis = synthesize_research_report_deterministic(report)
    published = publish_research_answer(
        report,
        synthesis,
        presentation_style=(
            "formal_report"
            if presentation_style == "formal_report"
            else "conversational"
        ),
    )
    answer = str(published.answer or "").strip()
    book_recommendation_mode = _book_recommendation_requested(report.objective)
    book_table_mode = bool(
        explicit_table_requested(report.objective)
        and book_recommendation_mode
    )
    if (
        not book_recommendation_mode
        and _format_contract_satisfied(answer, objective=report.objective)
    ):
        return answer
    evidence = _evidence_list(report, limit=MAX_EVIDENCE)
    if book_recommendation_mode:
        if publication_packet is not None and publication_packet.candidate_dossiers:
            evidence_by_citation = {
                item.citation_id: item
                for item in publication_packet.evidence_index
            }
            target = (
                publication_packet.target_candidate_count
                or len(publication_packet.candidate_dossiers)
            )
            evidence = []
            for dossier in publication_packet.candidate_dossiers[:target]:
                source = next(
                    (
                        evidence_by_citation.get(citation_id)
                        for citation_id in dossier.all_citation_ids
                        if evidence_by_citation.get(citation_id) is not None
                    ),
                    None,
                )
                reasoning = "；".join(
                    item
                    for item in (
                        dossier.portfolio_role,
                        dossier.model_rationale,
                        dossier.expected_fit,
                        "；".join(dossier.model_tradeoffs),
                    )
                    if item
                )
                evidence.append(
                    {
                        "candidate_title": dossier.candidate_title,
                        "claim": reasoning or (source.claim if source else "模型规划中的差异化候选"),
                        "source_title": source.source_title if source else "",
                        "source_url": source.source_url if source else "",
                        "quality": source.confidence if source else "model_knowledge",
                        "external_evidence_status": dossier.external_evidence_status,
                    }
                )
        else:
            evidence = _verified_book_candidate_rows(
                objective=report.objective,
                evidence=evidence,
            )
    elif not evidence:
        return answer
    zh = language == "zh-CN"
    if book_recommendation_mode and not book_table_mode:
        return _book_candidate_list_markdown(evidence, zh=zh)
    columns = requested_table_columns(report.objective) or (
        (
            ["书名", "推荐理由", "证据强度", "来源"]
            if zh
            else ["Book", "Why it fits", "Evidence strength", "Source"]
        )
        if book_table_mode
        else (
            ["结论", "证据强度", "来源"]
            if zh
            else ["Finding", "Evidence strength", "Source"]
        )
    )
    if book_table_mode and not evidence:
        evidence = [
            {
                "candidate_unavailable": True,
                "candidate_title": "",
                "claim": "",
                "source_title": "",
                "source_url": "",
                "quality": "insufficient",
                "evidence_status": "uncertain",
            }
        ]
    rows = []
    for item in evidence:
        rows.append(
            "| "
            + " | ".join(
                _report_table_value(
                    column,
                    item,
                    zh=zh,
                )
                for column in columns
            )
            + " |"
        )
    heading = "### 主要发现" if zh else "### Main findings"
    header = (
        "| "
        + " | ".join(_table_value(column) for column in columns)
        + " |\n| "
        + " | ".join("---" for _ in columns)
        + " |"
    )
    table = f"{heading}\n\n{header}\n" + "\n".join(rows)
    if book_table_mode:
        verified_count = sum(
            1 for item in evidence if not item.get("candidate_unavailable")
        )
        note = _book_candidate_note(verified_count, zh=zh)
        note_label = "说明" if zh else "Note"
        return f"{table}\n\n> **{note_label}：** {note}"
    return table + (f"\n\n{answer}" if answer else "")


def _book_candidate_list_markdown(
    evidence: list[dict[str, Any]],
    *,
    zh: bool,
) -> str:
    heading = "### 可以优先考虑的书" if zh else "### Books to consider first"
    opening = (
        "下面保留模型为这个目标规划的完整候选组合；外部材料用于标出哪些判断得到进一步核验。"
        if zh
        else "This preserves the model-planned candidate set; external material indicates which judgments received additional verification."
    )
    lines = [opening, "", heading, ""]
    for item in evidence:
        title = _table_value(item.get("candidate_title"))
        url = _safe_public_source_url(item.get("source_url"))
        linked_title = (
            f"[《{title}》]({url})" if url else f"《{title}》"
        )
        claim = _table_value(item.get("claim"))
        lines.append(f"- **{linked_title}**：{claim}" if zh else f"- **{linked_title}**: {claim}")
    if not evidence:
        lines.append(
            "当前资料不足以可靠推荐具体书名，我先不拿不相关的选项凑数。"
            if zh
            else "The available material is not sufficient for a reliable title recommendation, so unrelated options were not added."
        )
    label = "选择提示" if zh else "Choice note"
    externally_supported_count = sum(
        1
        for item in evidence
        if str(item.get("external_evidence_status") or "")
        in {"verified", "partial"}
        or str(item.get("source_url") or "").strip()
    )
    lines.extend(
        [
            "",
            f"> **{label}：** {_book_candidate_note(len(evidence), zh=zh, externally_supported_count=externally_supported_count)}",
        ]
    )
    return "\n".join(lines)


def _book_candidate_note(
    count: int,
    *,
    zh: bool,
    externally_supported_count: int | None = None,
) -> str:
    if externally_supported_count is not None:
        if zh:
            return (
                f"共保留 {count} 本模型候选，其中 {externally_supported_count} 本获得本轮外部材料补强；"
                "其余候选仍可作为专业建议，但不对缺少材料的版本、评分或配套资源状态作确定陈述。"
            )
        return (
            f"The model portfolio retains {count} candidate(s); {externally_supported_count} received external support in this run. "
            "The others remain professional recommendations, without asserting unverified editions, ratings, or resource status."
        )
    if zh:
        return (
            f"这里保留了 {count} 本有公开书目和内容依据的候选；"
            "资料没有充分覆盖的阅读门槛、版本差异和适用边界，不据此做强判断。"
            if count
            else "当前资料不足以支持具体书名推荐，因此暂不补充未经确认的候选。"
        )
    return (
        f"{count} candidate book(s) have traceable catalog and content support; "
        "reading difficulty, edition differences and fit are not overstated when the material is sparse."
        if count
        else "The available material does not support a specific title recommendation, so unconfirmed candidates were not added."
    )


def _report_table_value(
    column: str,
    item: dict[str, Any],
    *,
    zh: bool,
) -> str:
    key = re.sub(r"\s+", "", str(column or "")).casefold()
    unavailable = bool(item.get("candidate_unavailable"))
    if unavailable:
        if any(marker in key for marker in ("书名", "title", "book")):
            return "—（暂无可核验书目）" if zh else "— (no verified book)"
        if any(marker in key for marker in ("理由", "依据", "finding", "reason", "summary")):
            return "现有证据不足，未形成可靠候选" if zh else "Insufficient evidence for a reliable candidate"
        if any(marker in key for marker in ("局限", "取舍", "注意", "limit", "trade")):
            return "需要补充可核验的书籍目录来源" if zh else "More verifiable catalog evidence is needed"
        return "—"
    claim = _table_value(item.get("claim"))
    source_title = _table_value(item.get("source_title") or "source")
    source_url = _safe_public_source_url(item.get("source_url"))
    source = (
        f"[{source_title}]({source_url})"
        if source_url
        else source_title
    )
    if any(marker in key for marker in ("书名", "title", "book")):
        candidate_title = _table_value(item.get("candidate_title"))
        if "candidate_title" in item:
            if not candidate_title:
                return "—"
            return (
                f"[《{candidate_title}》]({source_url})"
                if source_url
                else f"《{candidate_title}》"
            )
        match = re.search(r"《([^》]{1,100})》", str(item.get("claim") or ""))
        return _table_value(match.group(1) if match else item.get("source_title"))
    if any(marker in key for marker in ("结论", "理由", "依据", "简介", "finding", "reason", "summary")):
        return _candidate_reason(item, claim=claim, zh=zh)
    if any(marker in key for marker in ("来源", "链接", "source", "link")):
        return source
    if any(marker in key for marker in ("证据", "强度", "quality", "confidence")):
        return _table_value(item.get("quality") or "unknown")
    if any(marker in key for marker in ("局限", "取舍", "注意", "limit", "trade")):
        return (
            "证据强度有限"
            if item.get("evidence_status") == "uncertain"
            else ("现有证据未明确说明" if zh else "Not specified by the evidence")
        )
    if any(marker in key for marker in ("作者", "author")):
        author = _explicit_claim_value(
            str(item.get("claim") or ""),
            patterns=(
                r"(?:作者|著者)\s*(?:为|是|[:：])\s*([^，；。]{2,100})",
                r"\bby\s+([A-Z][A-Za-z .,'-]{2,100})",
            ),
        )
        return author or (
            "现有证据未明确说明" if zh else "Not specified by the evidence"
        )
    if any(marker in key for marker in ("适合", "人群", "audience")):
        audience = _explicit_claim_value(
            str(item.get("claim") or ""),
            patterns=(
                r"(适合[^，；。]{1,80}(?:读者|人群|初学者|新手|学习者))",
                r"(?:for|suited to)\s+([^.;]{2,100})",
            ),
        )
        if audience:
            return audience
        title = str(item.get("candidate_title") or "")
        if re.search(r"(?:零基础|初学者|新手|入门|漫画|极简)", title):
            return (
                "零基础或初学者（依据目录书名定位）"
                if zh
                else "Beginners (based on the catalog title)"
            )
        return "现有证据未明确说明" if zh else "Not specified by the evidence"
    if any(marker in key for marker in ("作者", "适合", "人群", "author", "audience")):
        return "现有证据未明确说明" if zh else "Not specified by the evidence"
    return "现有证据未提供该字段" if zh else "Not provided by the evidence"


def _candidate_reason(
    item: dict[str, Any],
    *,
    claim: str,
    zh: bool,
) -> str:
    candidate_key = normalize_candidate_title(
        item.get("candidate_title")
    )
    ranked: list[tuple[int, int, str]] = []
    for index, fragment in enumerate(re.split(r"；+", claim)):
        clean = fragment.strip(" ；;，,。")
        if not clean:
            continue
        mentioned = {
            normalize_candidate_title(title)
            for title in re.findall(r"《([^》]{1,100})》", clean)
            if normalize_candidate_title(title)
        }
        if mentioned and any(key != candidate_key for key in mentioned):
            continue
        semantic_text = re.sub(r"《[^》]{1,100}》", " ", clean)
        semantic_score = sum(
            marker in semantic_text.casefold()
            for marker in (
                "介绍",
                "讲解",
                "涵盖",
                "内容",
                "适合",
                "入门",
                "基础",
                "通俗",
                "漫画",
                "故事",
                "实践",
                "方法",
                "beginner",
            )
        )
        metadata_only = bool(
            re.search(
                r"作者(?:为|是|[:：])|出版社|出版时间|出版年|"
                r"豆瓣页面显示评分",
                clean,
            )
        ) and semantic_score == 0
        if metadata_only:
            continue
        ranked.append((semantic_score, -index, clean))
    if ranked:
        return max(ranked)[2][:260]
    title = str(item.get("candidate_title") or "")
    if re.search(r"(?:零基础|初学者|新手|入门|漫画|极简)", title):
        return (
            "目录书名明确体现入门定位，与零基础学习目标直接相关。"
            if zh
            else "The catalog title explicitly signals a beginner-oriented scope."
        )
    return (
        "目录页已核验书名与作者，但现有材料未充分说明其对零基础读者的具体优势。"
        if zh
        else "The catalog verifies the title and author, but the available evidence does not clearly establish a beginner-specific advantage."
    )


def _explicit_claim_value(
    claim: str,
    *,
    patterns: tuple[str, ...],
) -> str:
    for pattern in patterns:
        match = re.search(pattern, str(claim or ""), flags=re.IGNORECASE)
        if match:
            return _table_value(match.group(1))[:120]
    return ""


def _table_value(value: Any) -> str:
    return " ".join(str(value or "").split()).replace("|", "｜")


def _presentation_style(review: dict[str, Any] | None) -> str:
    if not isinstance(review, dict):
        return "conversational"
    plan = review.get("objective_plan")
    if not isinstance(plan, dict):
        return "conversational"
    return (
        "formal_report"
        if str(plan.get("response_style") or "") == "formal_report"
        else "conversational"
    )


def _sections(markdown: str) -> list[str]:
    return [
        line.strip().lstrip("#").strip()
        for line in markdown.splitlines()
        if line.strip().startswith("## ")
    ]


def _message_text_and_thinking(response: Any) -> tuple[str, str]:
    """Split the response into the final answer and the thinking content.

    DashScope thinking models put the final deliverable as a bare string at
    the end of ``content``, with reasoning in ``type="thinking"`` blocks and
    an empty ``type="text"`` block.
    """

    additional = getattr(response, "additional_kwargs", None)
    provider_reasoning = (
        str(additional.get("reasoning_content") or "")
        if isinstance(additional, dict)
        else ""
    )
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip(), provider_reasoning.strip()
    if isinstance(content, list):
        parts: list[str] = []
        thinking: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "text":
                text = str(item.get("text") or "")
                if text.strip():
                    parts.append(text)
            elif item_type == "thinking":
                thinking.append(str(item.get("thinking") or ""))
        if provider_reasoning.strip():
            thinking.append(provider_reasoning)
        return "".join(parts).strip(), "".join(thinking).strip()
    return str(content or "").strip(), provider_reasoning.strip()


def _message_text(response: Any) -> str:
    """Extract the assistant's final answer; fall back to thinking."""

    final, thinking = _message_text_and_thinking(response)
    return final or thinking


def _attempt_cause(error: str) -> str:
    """Map an attempt error to a health cooldown cause."""

    lowered = str(error or "").lower()
    if any(token in lowered for token in ("rate", "429", "quota", "\u989d\u5ea6", "limit exceeded")):
        return "rate_limited"
    if any(token in lowered for token in ("timeout", "timed out")):
        return "timeout"
    if any(
        token in lowered
        for token in ("not found", "auth", "401", "403", "api key")
    ):
        return "model_unavailable"
    return ""


def _resolve_model_id(requested: str) -> str:
    if requested:
        return requested
    from app.infra.llm.manager import get_model_manager

    manager = get_model_manager()
    return manager.default_llm_id or manager.get_first_active_llm_id() or ""


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _workspace_from_review(review: dict[str, Any] | None) -> dict[str, Any]:
    """Project the final evolving workspace into the report prompt."""

    if not isinstance(review, dict):
        return {}
    metadata = review.get("metadata")
    workspace = metadata.get("evolving_report") if isinstance(metadata, dict) else None
    payload = workspace if isinstance(workspace, dict) else {}
    facts = payload.get("confirmed_facts") if isinstance(payload, dict) else None
    if not isinstance(facts, list):
        facts = []
    return {
        "objective": str(payload.get("objective") or "")[:300],
        "confirmed_facts": [
            {
                "content": str(item.get("content") or "")[:320],
                "confidence": str(item.get("confidence") or "medium"),
                "source": str(item.get("source") or "")[:300],
            }
            for item in facts[:5]
            if isinstance(item, dict)
        ],
        "information_gaps": [
            str(item)[:300]
            for item in (payload.get("information_gaps") or [])[:5]
        ],
        "conflicts": [
            str(item)[:300]
            for item in (payload.get("conflicts") or [])[:5]
        ],
        "missing_questions": [
            str(item)[:300]
            for item in (review.get("missing_questions") or [])[:5]
        ],
    }


__all__ = ["ResearchReportWriteResult", "write_research_report"]
