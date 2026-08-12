from __future__ import annotations

from typing import Any

from app.services.agent_runtime.contracts import ExecutionContext
from app.services.research.contracts import (
    ResearchEvidence,
    ResearchReadInput,
    ResearchStateResult,
    ResearchStep,
)


async def execute_research_read(
    arguments: dict[str, Any],
    *,
    context: ExecutionContext,
) -> dict[str, Any]:
    """Return one bounded, user-owned research run slice.

    This is the on-demand counterpart of the compact research receipts in
    the Controller context: chat only knows the run exists, and reads real
    report/evidence details from ResearchRun exactly when a question needs
    them.
    """

    try:
        request = ResearchReadInput.model_validate(arguments)
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"research_read_invalid:{exc}",
            "data": {},
        }

    from app.services.research.orchestrator import (
        get_research_orchestrator,
    )

    try:
        state = await get_research_orchestrator().inspect_research_state(
            user_id=context.user_id,
            run_id=request.research_run_id,
            limit_steps=request.limit,
            limit_evidence=request.limit,
        )
    except Exception:
        return {
            "status": "empty_result",
            "error": "research_run_not_found",
            "data": {},
        }

    return {
        "status": "ok",
        "result_mode": "research_read_result",
        "research_run_id": str(request.research_run_id),
        "scope": request.scope,
        "objective": state.run.objective,
        "run_status": state.run.status,
        "data": _project(state, scope=request.scope, limit=request.limit),
    }


def _project(
    state: ResearchStateResult,
    *,
    scope: str,
    limit: int,
) -> dict[str, Any]:
    if scope == "report":
        return {
            "conclusion": _conclusion(state),
            "known_facts": list(state.state.known_facts)[:limit],
            "gaps": list(state.state.gaps)[:limit],
            "conflicts": list(state.state.conflicts)[:limit],
            "exhausted_queries": list(state.state.exhausted_queries)[:limit],
            "evidence_count": len(state.evidence),
            "source_count": len(_unique_sources(state.evidence)),
        }
    if scope == "findings":
        findings: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for evidence in state.evidence:
            key = (
                (evidence.source_url or "").strip().lower(),
                (evidence.claim or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                {
                    "claim": evidence.claim,
                    "source_title": evidence.source_title,
                    "source_url": evidence.source_url,
                    "quality": evidence.quality,
                    "relevance": evidence.relevance,
                }
            )
            if len(findings) >= limit:
                break
        return {"findings": findings}
    if scope == "sources":
        return {"sources": _unique_sources(state.evidence)[:limit]}
    if scope == "evidence":
        return {
            "evidence": [
                item.model_dump(mode="json")
                for item in state.evidence[:limit]
            ]
        }
    if scope == "steps":
        return {
            "steps": [
                item.model_dump(mode="json")
                for item in state.steps[:limit]
            ]
        }
    return {}


def _conclusion(state: ResearchStateResult) -> str:
    for step in reversed(state.steps):
        if step.step_type != "finish":
            continue
        output = step.output if isinstance(step.output, dict) else {}
        text = str(output.get("conclusion") or "").strip()
        if text:
            return text[:2_000]
    metadata = state.state.metadata if isinstance(state.state.metadata, dict) else {}
    return str(metadata.get("conclusion") or "").strip()[:2_000]


def _unique_sources(
    evidence: list[ResearchEvidence],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence:
        url = str(item.source_url or "").strip()
        if not url or url.lower() in seen:
            continue
        seen.add(url.lower())
        sources.append(
            {
                "title": item.source_title,
                "url": url,
                "quality": item.quality,
            }
        )
    return sources


__all__ = ["execute_research_read"]
