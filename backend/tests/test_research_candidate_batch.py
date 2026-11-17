from __future__ import annotations

import asyncio
import unittest

from app.services.book_search_contracts import BookCandidateSearchResult
from app.services.external_search.contracts import (
    SearchHit,
    SearchRequest,
    SearchResult,
)
from app.services.research.deep_research_runner import (
    _best_candidate_subject_hit,
    _catalog_companion_tasks,
    _best_candidate_evidence_hit,
    _execute_search_task,
    _fair_candidate_source_records,
    _filter_recommendation_candidate_records,
    _merge_parallel_round_sources,
    _rank_candidate_hits,
    _unverified_candidate_hints,
)
from app.services.research.loop import (
    ResearchLoopBudget,
    ResearchRoundSources,
    ResearchSearchTask,
)
from app.services.research.source_acquisition import ResearchSourceRecord


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


class _EmptyCatalog:
    async def search(self, query: str, *, limit: int) -> BookCandidateSearchResult:
        del limit
        return BookCandidateSearchResult(
            query=query,
            provider_query=query,
            status="empty_result",
            source="douban_catalog",
        )


class _Catalog:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int) -> BookCandidateSearchResult:
        self.queries.append(query)
        rows = {
            "深度学习": [
                {
                    "title": "深度学习",
                    "url": "https://book.douban.com/subject/27087503/",
                    "author_name": "Ian Goodfellow",
                    "book_published_date": "2017",
                }
            ],
            "机器学习": [
                {
                    "title": "机器学习",
                    "url": "https://book.douban.com/subject/26708119/",
                    "author_name": "周志华",
                    "book_published_date": "2016",
                },
                {
                    "title": "零基础学机器学习",
                    "url": "https://book.douban.com/subject/35264202/",
                    "author_name": "黄佳",
                    "book_published_date": "2020",
                },
            ],
        }.get(query, [])[:limit]
        return BookCandidateSearchResult(
            query=query,
            provider_query=query,
            status="ok" if rows else "empty_result",
            candidates=rows,
            source="douban_catalog",
            duration_ms=5,
        )


class ResearchCandidateBatchTests(unittest.TestCase):
    def test_catalog_companions_cover_decision_dimensions_in_same_round(self) -> None:
        from app.services.evidence_strategy import EvidenceFacet, EvidenceStrategy

        tasks = _catalog_companion_tasks(
            objective="推荐稳健个人理财书并比较风险观",
            round_index=2,
            budget=ResearchLoopBudget(max_results_per_round=8),
            evidence_strategy=EvidenceStrategy(
                facets=[
                    EvidenceFacet(
                        name="风险观",
                        query_terms=["风险观", "对比"],
                        importance="high",
                    ),
                    EvidenceFacet(
                        name="中国适用性",
                        query_terms=["中国适用性"],
                        importance="high",
                    ),
                ]
            ),
            candidate_titles=[],
            used_queries=set(),
        )

        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            [item.purpose for item in tasks],
            ["evidence_strategy_facet", "evidence_strategy_facet"],
        )
        self.assertEqual(tasks[0].target_gaps, ["风险观"])

    def test_parallel_round_merge_preserves_catalog_and_dimension_evidence(self) -> None:
        catalog_task = ResearchSearchTask(
            round_index=2,
            objective="推荐稳健个人理财书",
            purpose="catalog_candidate_verification",
            query="《小狗钱钱》",
        )
        dimension_task = catalog_task.model_copy(
            update={
                "purpose": "decision_dimension_evidence",
                "query": "理财书 风险观 对比",
            }
        )
        catalog = ResearchRoundSources(
            round_index=2,
            task=catalog_task,
            provider="catalog",
            source_records=[
                ResearchSourceRecord(
                    source_title="小狗钱钱 (豆瓣)",
                    source_url="https://book.douban.com/subject/42/",
                    claim="《小狗钱钱》；作者：博多·舍费尔。",
                )
            ],
            source_count=1,
            publishable_source_count=1,
        )
        dimension = ResearchRoundSources(
            round_index=2,
            task=dimension_task,
            provider="web",
            source_records=[
                ResearchSourceRecord(
                    source_title="理财方法比较",
                    source_url="https://example.org/finance",
                    claim="稳健方法强调先控制损失和现金流。",
                )
            ],
            source_count=1,
            publishable_source_count=1,
        )

        merged = _merge_parallel_round_sources([catalog, dimension])

        self.assertEqual(len(merged.source_records), 2)
        self.assertEqual(merged.publishable_source_count, 2)
        self.assertEqual(merged.metadata["parallel_branch_count"], 2)
        self.assertEqual(
            merged.metadata["parallel_branch_purposes"],
            ["catalog_candidate_verification", "decision_dimension_evidence"],
        )

    def test_unverified_candidate_hints_exclude_owned_catalog_entities(self) -> None:
        remaining = _unverified_candidate_hints(
            {
                "小狗钱钱": "小狗钱钱 博多舍费尔",
                "金钱心理学": "金钱心理学 摩根豪泽尔",
            },
            [
                ResearchSourceRecord(
                    source_title="小狗钱钱 (豆瓣)",
                    source_url="https://book.douban.com/subject/42/",
                    claim="《小狗钱钱》是一本财商读物。",
                )
            ],
        )

        self.assertEqual(remaining, {"金钱心理学": "金钱心理学 摩根豪泽尔"})

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
            _execute_search_task(
                gateway=gateway,
                request=request,
                task=task,
                catalog_provider=_EmptyCatalog(),
            )
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
            _execute_search_task(
                gateway=gateway,
                request=request,
                task=task,
                catalog_provider=_EmptyCatalog(),
            )
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

    def test_structured_catalog_resolves_exact_and_discovers_topic_candidates(self) -> None:
        gateway = _Gateway()
        catalog = _Catalog()
        request = SearchRequest(
            query="AI candidate batch",
            max_results=8,
            detail="standard",
            include_domains=["book.douban.com"],
            include_url_prefixes=["https://book.douban.com/subject/"],
        )
        task = ResearchSearchTask(
            round_index=2,
            objective="推荐人工智能与机器学习入门书",
            query="《深度学习》 机器学习",
            metadata={
                "candidate_titles": ["深度学习"],
                "candidate_search_hints": {"深度学习": "深度学习"},
                "catalog_discovery_queries": ["机器学习"],
            },
        )

        result = asyncio.run(
            _execute_search_task(
                gateway=gateway,
                request=request,
                task=task,
                catalog_provider=catalog,
            )
        )

        self.assertEqual(catalog.queries, ["深度学习", "机器学习"])
        self.assertEqual(gateway.queries, [])
        self.assertEqual(result.outcome, "found")
        self.assertEqual(
            [item.url for item in result.hits],
            [
                "https://book.douban.com/subject/35264202/",
                "https://book.douban.com/subject/27087503/",
                "https://book.douban.com/subject/26708119/",
            ],
        )
        self.assertEqual(result.metadata["matched_candidate_count"], 1)
        self.assertEqual(result.metadata["catalog_discovered_count"], 2)
        self.assertTrue(
            any(
                "作者：Ian Goodfellow" in item.content
                for item in result.hits
            )
        )

    def test_ai_objective_expands_catalog_queries_beyond_one_narrow_term(self) -> None:
        from app.services.research.candidate_quality import (
            objective_topic_anchors,
        )

        anchors = objective_topic_anchors(
            "请推荐至少4本适合零基础读者的人工智能入门书。"
            "请按书名、作者、推荐理由、适合人群、来源链接的5列表格输出。"
        )

        self.assertEqual(
            anchors[:3],
            ["人工智能入门", "机器学习入门", "深度学习入门"],
        )
        self.assertNotIn("输出", anchors)
        self.assertNotIn("链接", anchors)

    def test_deep_learning_catalog_facets_precede_verbose_model_themes(self) -> None:
        from app.services.research.candidate_quality import (
            objective_topic_anchors,
        )

        anchors = objective_topic_anchors(
            "有什么深度学习书籍推荐",
            themes=[
                "theoretical foundations of representation learning",
                "practical applications for advanced readers",
            ],
        )

        self.assertEqual(
            anchors,
            [
                "深度学习",
                "深度学习入门",
                "深度学习实战",
                "Python 深度学习",
                "神经网络与深度学习",
                "机器学习实战",
            ],
        )

    def test_deep_learning_specific_books_rank_before_generic_ml_books(self) -> None:
        ranked = _rank_candidate_hits(
            [
                SearchHit(
                    title="统计学习方法 (豆瓣)",
                    url="https://book.douban.com/subject/1/",
                    snippet="机器学习基础教材",
                    provider="douban_catalog",
                ),
                SearchHit(
                    title="Python深度学习 (豆瓣)",
                    url="https://book.douban.com/subject/2/",
                    snippet="Python 与神经网络实践",
                    provider="douban_catalog",
                ),
                SearchHit(
                    title="PyTorch深度学习实战 (豆瓣)",
                    url="https://book.douban.com/subject/3/",
                    snippet="PyTorch 深度学习实战",
                    provider="douban_catalog",
                ),
            ],
            objective="有什么深度学习书籍推荐",
            limit=3,
        )

        self.assertEqual(
            [item.title for item in ranked[:2]],
            ["PyTorch深度学习实战 (豆瓣)", "Python深度学习 (豆瓣)"],
        )

    def test_candidate_record_budget_is_fair_across_catalog_urls(self) -> None:
        records = [
            ResearchSourceRecord(
                source_type="web",
                source_title=f"候选 {url_index}",
                source_url=f"https://book.douban.com/subject/{url_index}/",
                claim=f"候选 {url_index} 的第 {claim_index} 条事实。",
                excerpt=f"候选 {url_index} 的第 {claim_index} 条事实。",
                quality="medium",
                relevance=5,
            )
            for url_index in range(1, 4)
            for claim_index in range(1, 4)
        ]

        selected = _fair_candidate_source_records(records, limit=5)

        self.assertEqual(
            [item.source_url for item in selected[:3]],
            [
                "https://book.douban.com/subject/1/",
                "https://book.douban.com/subject/2/",
                "https://book.douban.com/subject/3/",
            ],
        )
        self.assertEqual(len(selected), 5)

    def test_catalog_identity_inherits_topic_support_from_same_source(self) -> None:
        source_url = "https://book.douban.com/subject/42/"
        task = ResearchSearchTask(
            round_index=2,
            objective="推荐稳健个人理财书",
            query="《小狗钱钱》",
            metadata={"candidate_titles": ["小狗钱钱"]},
        )
        round_sources = ResearchRoundSources(
            round_index=2,
            task=task,
            source_records=[
                ResearchSourceRecord(
                    source_title="小狗钱钱 (豆瓣)",
                    source_url=source_url,
                    claim="《小狗钱钱》；作者：博多·舍费尔。",
                    excerpt="《小狗钱钱》；作者：博多·舍费尔。",
                    quality="medium",
                    relevance=5,
                ),
                ResearchSourceRecord(
                    source_title="小狗钱钱 (豆瓣)",
                    source_url=source_url,
                    claim="本书用故事介绍储蓄、预算和长期个人理财习惯。",
                    excerpt="本书用故事介绍储蓄、预算和长期个人理财习惯。",
                    quality="medium",
                    relevance=5,
                ),
            ],
            source_count=2,
            publishable_source_count=2,
        )

        filtered = _filter_recommendation_candidate_records(
            round_sources,
            objective="推荐稳健个人理财书",
            themes=["个人理财"],
        )

        self.assertEqual(len(filtered.source_records), 2)
        self.assertEqual(filtered.publishable_source_count, 2)


if __name__ == "__main__":
    unittest.main()
