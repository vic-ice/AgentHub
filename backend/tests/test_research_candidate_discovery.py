from __future__ import annotations

import unittest

from app.services.research.deep_research_runner import (
    _candidate_discovery_enabled,
    _discovered_candidate_hints,
    _discovered_candidate_titles,
)
from app.services.research.loop import ResearchRoundSources, ResearchSearchTask
from app.services.research.source_acquisition import ResearchSourceRecord
from app.services.research.candidate_quality import is_probable_book_title


def _round(*records: ResearchSourceRecord, verification: bool = False):
    return ResearchRoundSources(
        round_index=1,
        task=ResearchSearchTask(
            round_index=1,
            objective="推荐人工智能入门书",
            query="人工智能入门图书",
            metadata={"candidate_titles": ["待核验书"]} if verification else {},
        ),
        source_records=list(records),
        source_count=len(records),
        publishable_source_count=len(records),
    )


def _record(*, title: str, claim: str, subject_id: int = 1):
    return ResearchSourceRecord(
        source_type="web",
        source_title=f"{title} (豆瓣)",
        source_url=f"https://book.douban.com/subject/{subject_id}/",
        claim=claim,
        excerpt=claim,
        quality="medium",
        relevance=4,
    )


def _search_record(*, title: str, claim: str, url: str):
    return ResearchSourceRecord(
        source_type="web",
        source_title=title,
        source_url=url,
        claim=claim,
        excerpt=claim,
        quality="medium",
        relevance=4,
    )


class ResearchCandidateDiscoveryTests(unittest.TestCase):
    def _discover(self, record, *, objective="推荐人工智能入门书"):
        return _discovered_candidate_titles(
            _round(record),
            excluded_titles=[],
            objective=objective,
            themes=["人工智能"],
        )

    def test_unrelated_catalog_title_is_not_discovered(self) -> None:
        titles = self._discover(
            _record(
                title="广东社会科学",
                claim="页面只列出该刊物的作者、出版社与出版信息。",
            )
        )
        self.assertEqual(titles, [])

    def test_relevant_catalog_title_is_discovered(self) -> None:
        titles = self._discover(
            _record(
                title="深度学习",
                claim="本书介绍人工智能领域的神经网络与深度学习方法。",
            )
        )
        self.assertEqual(titles, ["深度学习"])

    def test_discovery_preserves_explicit_author_as_disambiguation_hint(self) -> None:
        hints = _discovered_candidate_hints(
            _round(
                _record(
                    title="深度学习",
                    claim=(
                        "《深度学习》的作者为 Ian Goodfellow；"
                        "本书介绍机器学习与神经网络。"
                    ),
                )
            ),
            excluded_titles=[],
            objective="推荐人工智能入门书",
            themes=["人工智能"],
        )

        self.assertEqual(hints, {"深度学习": "深度学习 Ian Goodfellow"})

    def test_generic_book_request_fails_open(self) -> None:
        titles = _discovered_candidate_titles(
            _round(
                _record(
                    title="可靠候选",
                    claim="该书有完整的作者、出版社和内容简介。",
                )
            ),
            excluded_titles=[],
            objective="推荐4本好书",
            themes=[],
        )
        self.assertEqual(titles, ["可靠候选"])

    def test_explicit_periodical_is_not_a_book_candidate(self) -> None:
        titles = self._discover(
            _record(
                title="人工智能学报",
                claim="这是一本按期发行的 ISSN 学术期刊，第3期讨论人工智能。",
            )
        )
        self.assertEqual(titles, [])

    def test_verification_round_cannot_expand_candidate_frontier(self) -> None:
        self.assertFalse(_candidate_discovery_enabled(_round(verification=True).task))
        self.assertTrue(_candidate_discovery_enabled(_round().task))

    def test_title_shaped_search_result_becomes_catalog_verification_lead(self) -> None:
        titles = _discovered_candidate_titles(
            _round(
                _search_record(
                    title="Hands-On Machine Learning with Scikit-Learn, Keras, and TensorFlow",
                    claim="This book covers neural networks and practical deep learning.",
                    url="https://www.oreilly.com/library/view/hands-on-machine-learning/",
                )
            ),
            excluded_titles=[],
            objective="有什么深度学习书籍推荐",
            themes=["deep learning", "neural networks"],
        )
        self.assertEqual(
            titles,
            ["Hands-On Machine Learning with Scikit-Learn, Keras, and TensorFlow"],
        )

    def test_editorial_search_title_does_not_become_a_book_entity(self) -> None:
        titles = _discovered_candidate_titles(
            _round(
                _search_record(
                    title="2026 年最值得读的 10 本深度学习书籍推荐",
                    claim="这是一篇汇总深度学习教材的编辑书单。",
                    url="https://example.com/deep-learning-list",
                )
            ),
            excluded_titles=[],
            objective="有什么深度学习书籍推荐",
            themes=["deep learning"],
        )
        self.assertEqual(titles, [])

    def test_planner_headline_is_not_a_probable_book_title(self) -> None:
        self.assertFalse(is_probable_book_title("2024年最佳个人理财书籍"))
        self.assertFalse(
            is_probable_book_title("Top 10 Best Personal Finance Books")
        )
        self.assertTrue(is_probable_book_title("金钱心理学"))

    def test_embedded_book_title_is_recovered_from_truncated_editorial_title(self) -> None:
        titles = _discovered_candidate_titles(
            _round(
                _search_record(
                    title="文末赠书 | 2025 年 | 《深度学习：基础与概念",
                    claim="该书讨论机器学习、神经网络与深度学习基础。",
                    url="https://example.com/giveaway",
                )
            ),
            excluded_titles=[],
            objective="有什么深度学习书籍推荐",
            themes=["deep learning"],
        )
        self.assertEqual(titles, ["深度学习：基础与概念"])


if __name__ == "__main__":
    unittest.main()
