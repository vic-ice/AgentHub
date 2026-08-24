from __future__ import annotations

import asyncio
import unittest

from app.services.external_search.contracts import (
    SearchHit,
    SearchRequest,
    SearchResult,
)
from app.services.research.deep_research_runner import (
    _best_candidate_subject_hit,
    _best_candidate_evidence_hit,
    _execute_search_task,
)
from app.services.research.loop import ResearchSearchTask


class _Gateway:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, request: SearchRequest) -> SearchResult:
        self.queries.append(request.query)
        title = "候选甲" if "候选甲" in request.query else "候选乙"
        suffix = "1" if title == "候选甲" else "2"
        return SearchResult(
            outcome="found",
            provider="test",
            query=request.query,
            hits=[
                SearchHit(
                    title=f"{title}实践篇 (豆瓣)",
                    url=f"https://book.douban.com/subject/{suffix}9/",
                    provider="test",
                    score=0.95,
                ),
                SearchHit(
                    title=f"{title} (豆瓣)",
                    url=f"https://book.douban.com/subject//{suffix}/?from=search",
                    provider="test",
                    score=0.90,
                    snippet=f"作者：{'甲作者' if title == '候选甲' else '乙作者'}",
                ),
            ],
        )


class _RetryGateway:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, request: SearchRequest) -> SearchResult:
        self.queries.append(request.query)
        if "豆瓣读书" not in request.query:
            return SearchResult(
                outcome="found",
                provider="test",
                query=request.query,
                hits=[
                    SearchHit(
                        title="候选甲 - 百科",
                        url="https://example.org/book",
                        provider="test",
                    )
                ],
            )
        return SearchResult(
            outcome="found",
            provider="test",
            query=request.query,
            hits=[
                SearchHit(
                    title="候选甲 (豆瓣)",
                    url="https://book.douban.com/subject/42/",
                    provider="test",
                )
            ],
        )


class ResearchCandidateBatchTests(unittest.TestCase):
    def test_unmatched_title_retries_douban_before_broad_web_fallback(self) -> None:
        gateway = _RetryGateway()
        request = SearchRequest(
            query="候选验证",
            max_results=8,
            include_domains=["book.douban.com"],
            include_url_prefixes=["https://book.douban.com/subject/"],
        )
        task = ResearchSearchTask(
            round_index=1,
            objective="推荐同类图书",
            query="《候选甲》",
            metadata={"candidate_titles": ["候选甲"]},
        )
        result = asyncio.run(
            _execute_search_task(gateway=gateway, request=request, task=task)
        )
        self.assertEqual(len(gateway.queries), 2)
        self.assertIn("豆瓣读书", gateway.queries[1])
        self.assertEqual(result.hits[0].url, "https://book.douban.com/subject/42/")

    def test_same_title_is_disambiguated_by_author_hint(self) -> None:
        selected = _best_candidate_subject_hit(
            [
                SearchHit(
                    title="影响力 (豆瓣)",
                    url="https://book.douban.com/subject/1/",
                    provider="test",
                    score=0.99,
                    snippet="作者王婷婷，企业管理出版社。",
                ),
                SearchHit(
                    title="影响力 (豆瓣)",
                    url="https://book.douban.com/subject/2/",
                    provider="test",
                    score=0.80,
                    snippet="罗伯特·西奥迪尼的社会心理学作品。",
                ),
            ],
            title="影响力",
            search_hint="影响力 罗伯特西奥迪尼 说服心理学",
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.url, "https://book.douban.com/subject/2/")

    def test_exact_editorial_fallback_wins_over_social_and_unrelated_pages(self) -> None:
        selected = _best_candidate_evidence_hit(
            [
                SearchHit(
                    title="候选甲 推荐",
                    url="https://www.instagram.com/p/example",
                    provider="test",
                    score=0.99,
                ),
                SearchHit(
                    title="另一本候选甲",
                    url="https://example.org/unrelated",
                    provider="test",
                    score=0.95,
                ),
                SearchHit(
                    title="候选甲（图书）_百度百科",
                    url="https://baike.baidu.com/item/example",
                    provider="test",
                    score=0.80,
                ),
            ],
            title="候选甲",
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.url, "https://baike.baidu.com/item/example")

    def test_each_candidate_is_searched_and_exact_subject_wins(self) -> None:
        gateway = _Gateway()
        request = SearchRequest(
            query="candidate batch",
            max_results=8,
            detail="deep",
            include_domains=["book.douban.com"],
            include_url_prefixes=["https://book.douban.com/subject/"],
        )
        task = ResearchSearchTask(
            round_index=1,
            objective="推荐同类图书",
            query="《候选甲》《候选乙》",
            metadata={
                "candidate_titles": ["候选甲", "候选乙"],
                "candidate_search_hints": {
                    "候选甲": "候选甲 甲作者",
                    "候选乙": "候选乙 乙作者",
                },
            },
        )

        result = asyncio.run(
            _execute_search_task(gateway=gateway, request=request, task=task)
        )

        self.assertEqual(len(gateway.queries), 2)
        self.assertEqual(
            gateway.queries,
            [
                "候选甲 site:book.douban.com/subject",
                "候选乙 site:book.douban.com/subject",
            ],
        )
        self.assertEqual([item.title for item in result.hits], ["候选甲 (豆瓣)", "候选乙 (豆瓣)"])
        self.assertEqual(
            [item.url for item in result.hits],
            [
                "https://book.douban.com/subject/1/",
                "https://book.douban.com/subject/2/",
            ],
        )
        self.assertEqual(result.metadata["matched_candidate_count"], 2)


if __name__ == "__main__":
    unittest.main()
