from __future__ import annotations

import unittest

from app.services.research.loop import (
    ResearchLoopBudget,
    evaluate_research_evidence_gaps,
)
from app.services.research.source_acquisition import ResearchSourceRecord


class ResearchLoopQualityTests(unittest.TestCase):
    def _record(self, *, quality: str) -> ResearchSourceRecord:
        return ResearchSourceRecord(
            source_type="web",
            source_title="年度推荐书单",
            source_url="https://example.org/reading-list",
            claim="《候选图书》被该来源列入推荐书单。",
            excerpt="《候选图书》被该来源列入推荐书单。",
            quality=quality,
            relevance=4,
        )

    def test_low_quality_lead_does_not_stop_medium_quality_research(self) -> None:
        result = evaluate_research_evidence_gaps(
            round_index=1,
            records=[self._record(quality="low")],
            rejection_reason_codes=[],
            exhausted_queries=["年度推荐书单"],
            budget=ResearchLoopBudget(
                max_search_rounds=3,
                required_evidence_quality="medium",
            ),
            objective="今年有哪些值得读的书推荐",
        )
        self.assertFalse(result.satisfied)
        self.assertTrue(result.should_continue)
        self.assertEqual(result.gaps, ["insufficient_evidence_quality"])

    def test_medium_quality_candidate_uses_same_threshold_as_final_verifier(self) -> None:
        result = evaluate_research_evidence_gaps(
            round_index=1,
            records=[self._record(quality="medium")],
            rejection_reason_codes=[],
            exhausted_queries=["年度推荐书单"],
            budget=ResearchLoopBudget(
                max_search_rounds=3,
                required_evidence_quality="medium",
            ),
            objective="今年有哪些值得读的书推荐",
        )
        self.assertTrue(result.satisfied)
        self.assertFalse(result.should_continue)
        self.assertEqual(result.gaps, [])

    def test_verified_candidate_can_be_identified_from_exact_subject_title(self) -> None:
        records = [
            ResearchSourceRecord(
                source_type="web",
                source_title="沟通候选：实践篇 (豆瓣)",
                source_url="https://book.douban.com/subject/1/",
                claim="该书被来源列入推荐书单，并介绍高难度沟通方法。",
                excerpt="该书被来源列入推荐书单，并介绍高难度沟通方法。",
                quality="medium",
                relevance=5,
            ),
            ResearchSourceRecord(
                source_type="web",
                source_title="财商候选 (豆瓣)",
                source_url="https://book.douban.com/subject/2/",
                claim="该书被来源列入推荐书单，并介绍长期财务决策。",
                excerpt="该书被来源列入推荐书单，并介绍长期财务决策。",
                quality="medium",
                relevance=5,
            ),
        ]
        result = evaluate_research_evidence_gaps(
            round_index=1,
            records=records,
            rejection_reason_codes=[],
            exhausted_queries=[],
            budget=ResearchLoopBudget(
                max_search_rounds=3,
                required_evidence_quality="medium",
                min_recommendation_candidates=2,
                recommendation_candidate_titles=["沟通候选", "财商候选"],
            ),
            objective="推荐沟通与财商方向的书",
        )
        self.assertTrue(result.satisfied)
        self.assertEqual(result.metadata["recommendation_candidate_count"], 2)


if __name__ == "__main__":
    unittest.main()
