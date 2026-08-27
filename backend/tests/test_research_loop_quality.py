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
                recommendation_candidate_titles=["沟通候选：实践篇", "财商候选"],
            ),
            objective="推荐沟通与财商方向的书",
        )
        self.assertTrue(result.satisfied)
        self.assertEqual(result.metadata["recommendation_candidate_count"], 2)

    def test_editorial_article_titles_do_not_count_as_book_candidates(self) -> None:
        records = [
            ResearchSourceRecord(
                source_type="web",
                source_title=f"AI 入门书单文章 {index}",
                source_url=f"https://editorial{index}.example.org/ai-reading-list",
                claim="该来源整理了人工智能初学者图书选择与阅读路线建议。",
                excerpt="该来源整理了人工智能初学者图书选择与阅读路线建议。",
                quality="medium",
                relevance=4,
            )
            for index in range(1, 5)
        ]

        result = evaluate_research_evidence_gaps(
            round_index=1,
            records=records,
            rejection_reason_codes=[],
            exhausted_queries=["AI 入门书单"],
            budget=ResearchLoopBudget(
                max_search_rounds=3,
                required_evidence_quality="medium",
                min_recommendation_candidates=4,
            ),
            objective="推荐 AI 入门书",
        )

        self.assertEqual(result.metadata["publishable_record_count"], 4)
        self.assertEqual(result.metadata["recommendation_candidate_count"], 0)
        self.assertFalse(result.satisfied)
        self.assertIn("insufficient_recommendation_candidates", result.gaps)

    def test_titles_mentioned_by_one_catalog_page_do_not_inflate_count(self) -> None:
        record = ResearchSourceRecord(
            source_type="web",
            source_title="小狗钱钱 (豆瓣)",
            source_url="https://book.douban.com/subject/42/",
            claim=(
                "《小狗钱钱》介绍储蓄和个人理财习惯，"
                "页面还提到可与《金钱心理学》对照阅读。"
            ),
            excerpt="适合个人理财入门。",
            quality="medium",
            relevance=5,
        )
        result = evaluate_research_evidence_gaps(
            round_index=1,
            records=[record],
            rejection_reason_codes=[],
            exhausted_queries=[],
            budget=ResearchLoopBudget(
                max_search_rounds=3,
                required_evidence_quality="medium",
                min_recommendation_candidates=2,
            ),
            objective="推荐个人理财书",
        )

        self.assertEqual(result.metadata["recommendation_candidate_count"], 1)
        self.assertIn("insufficient_recommendation_candidates", result.gaps)

    def test_unrelated_catalog_identity_does_not_satisfy_candidate_count(self) -> None:
        records = [
            ResearchSourceRecord(
                source_type="web",
                source_title=title + " (豆瓣)",
                source_url=f"https://book.douban.com/subject/{index}/",
                claim=claim,
                excerpt=claim,
                quality="medium",
                relevance=5,
            )
            for index, (title, claim) in enumerate(
                [
                    ("人工智能：一种现代的方法", "本书系统介绍人工智能的主要方法与应用。"),
                    ("深度学习", "本书介绍人工智能领域的神经网络与深度学习。"),
                    ("机器学习实战", "本书通过实例介绍人工智能与机器学习算法。"),
                    ("广东社会科学", "页面只列出该刊物的作者、出版社与出版信息。"),
                ],
                start=10,
            )
        ]

        result = evaluate_research_evidence_gaps(
            round_index=2,
            records=records,
            rejection_reason_codes=[],
            exhausted_queries=["人工智能入门图书"],
            budget=ResearchLoopBudget(
                max_search_rounds=3,
                required_evidence_quality="medium",
                min_recommendation_candidates=4,
                recommendation_candidate_titles=[
                    "人工智能：一种现代的方法",
                    "深度学习",
                    "机器学习实战",
                    "广东社会科学",
                ],
            ),
            objective="推荐至少4本适合零基础读者的人工智能入门书",
        )

        self.assertEqual(result.metadata["publishable_record_count"], 3)
        self.assertEqual(result.metadata["recommendation_candidate_count"], 3)
        self.assertFalse(result.satisfied)
        self.assertIn("insufficient_recommendation_candidates", result.gaps)

    def test_topic_matched_catalog_discoveries_count_beyond_seed_frontier(self) -> None:
        records = [
            ResearchSourceRecord(
                source_type="web",
                source_title=title + " (豆瓣)",
                source_url=f"https://book.douban.com/subject/{index}/",
                claim=claim,
                excerpt=claim,
                quality="medium",
                relevance=5,
            )
            for index, (title, claim) in enumerate(
                [
                    ("深度学习", "本书介绍机器学习、神经网络和深度学习。"),
                    ("Python深度学习", "本书用 Python 讲解神经网络和深度学习实践。"),
                    ("PyTorch深度学习实战", "本书以 PyTorch 展开深度学习实战。"),
                    ("神经网络与深度学习", "本书系统讲解神经网络与深度学习。"),
                ],
                start=30,
            )
        ]

        result = evaluate_research_evidence_gaps(
            round_index=4,
            records=records,
            rejection_reason_codes=[],
            exhausted_queries=["深度学习目录检索"],
            budget=ResearchLoopBudget(
                max_search_rounds=4,
                required_evidence_quality="medium",
                min_recommendation_candidates=4,
                recommendation_candidate_titles=["深度学习"],
            ),
            objective="有什么深度学习书籍推荐",
        )

        self.assertEqual(result.metadata["recommendation_candidate_count"], 4)
        self.assertTrue(result.satisfied)
        self.assertEqual(result.gaps, [])

    def test_catalog_subpages_never_count_as_book_entities(self) -> None:
        records = [
            ResearchSourceRecord(
                source_type="web",
                source_title=f"人工智能候选 {index} (豆瓣)",
                source_url=(
                    f"https://book.douban.com/subject/{index}/doulists"
                ),
                claim="该页面讨论人工智能与机器学习入门图书。",
                excerpt="该页面讨论人工智能与机器学习入门图书。",
                quality="medium",
                relevance=5,
            )
            for index in range(1, 5)
        ]

        result = evaluate_research_evidence_gaps(
            round_index=1,
            records=records,
            rejection_reason_codes=[],
            exhausted_queries=["人工智能入门书"],
            budget=ResearchLoopBudget(
                max_search_rounds=3,
                required_evidence_quality="medium",
                min_recommendation_candidates=4,
            ),
            objective="推荐人工智能入门书",
        )

        self.assertEqual(result.metadata["recommendation_candidate_count"], 0)
        self.assertFalse(result.satisfied)
        self.assertIn("insufficient_recommendation_candidates", result.gaps)


if __name__ == "__main__":
    unittest.main()
