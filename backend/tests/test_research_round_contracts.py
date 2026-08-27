from app.services.external_search.contracts import SearchRequest
from app.services.research.loop.contracts import (
    ResearchGapAssessment,
    ResearchRoundSources,
    ResearchSearchTask,
)
from app.services.research.reviewer import ResearchReview


def test_fourth_research_round_is_supported_across_runtime_contracts() -> None:
    task = ResearchSearchTask(
        round_index=4,
        objective="compare verified deep-learning books",
        query="deep learning books official catalog",
    )

    assert SearchRequest(query=task.query, round_index=4).round_index == 4
    assert ResearchRoundSources(round_index=4, task=task).round_index == 4
    assert ResearchGapAssessment(
        round_index=4,
        stop_reason="search_budget_exhausted",
    ).round_index == 4
    assert ResearchReview(
        round_index=4,
        objective=task.objective,
        verdict="budget_exhausted",
    ).round_index == 4
