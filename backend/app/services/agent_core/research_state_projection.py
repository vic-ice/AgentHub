from __future__ import annotations

from typing import Any

from app.services.agent_core.prompt_contracts import (
    TrustedResearchFact,
    TrustedResearchStateContext,
)
from app.services.research.projection import (
    EvolvingResearchReport,
    project_evolving_report,
)


def project_research_controller_context(
    receipts: list[Any],
) -> TrustedResearchStateContext | None:
    """Project research receipts into the Controller's bounded workspace.

    Pure projection with no I/O. Returns None when no research activity is
    visible, so non-research turns keep an empty research workspace.
    """

    report = project_evolving_report(receipts)
    if report is None:
        return None
    return _to_trusted_context(report)


def _to_trusted_context(
    report: EvolvingResearchReport,
) -> TrustedResearchStateContext:
    return TrustedResearchStateContext(
        objective=report.objective,
        status=report.status,
        step_count=report.step_count,
        confirmed_facts=[
            TrustedResearchFact(
                content=fact.content,
                source=fact.source,
                confidence=fact.confidence,
                step=fact.step,
            )
            for fact in report.confirmed_facts
        ],
        open_questions=list(report.open_questions),
        information_gaps=list(report.information_gaps),
        current_focus=report.current_focus,
        exhausted_queries=list(report.exhausted_queries),
        conflicts=list(report.conflicts),
    )


__all__ = ["project_research_controller_context"]
