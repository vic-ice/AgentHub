from __future__ import annotations

import asyncio
import time
import unittest

from app.services.external_search import (
    SearchAttempt,
    SearchGateway,
    SearchHit,
    SearchRequest,
    SearchResult,
)
from app.services.external_search.policy import provider_order
from app.services.research.loop.contracts import ResearchSearchTask
from app.services.research.loop.source_projection import (
    project_research_search_output,
)
from app.services.research.search_policy import build_research_search_request
from app.services.provider_config import (
    get_provider_registry,
    reset_provider_registry,
)


class _FakeProvider:
    def __init__(
        self,
        name: str,
        outcome: str,
        *,
        url: str = "",
    ) -> None:
        self.name = name
        self.outcome = outcome
        self.url = url or f"https://{name}.example/result"
        self.calls = 0

    async def search(self, request: SearchRequest) -> SearchResult:
        self.calls += 1
        hits = (
            [
                SearchHit(
                    title=f"{self.name} result",
                    url=self.url,
                    snippet="A source-backed result.",
                    provider=self.name,
                )
            ]
            if self.outcome == "found"
            else []
        )
        return SearchResult(
            outcome=self.outcome,
            provider=self.name,
            query=request.query,
            effective_query=request.query,
            hits=hits,
            error=(
                f"{self.name} unavailable"
                if self.outcome == "unavailable"
                else ""
            ),
            attempts=[
                SearchAttempt(
                    provider=self.name,
                    outcome=self.outcome,
                    error=(
                        f"{self.name} unavailable"
                        if self.outcome == "unavailable"
                        else ""
                    ),
                )
            ],
        )


class _RaisingProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def search(self, request: SearchRequest) -> SearchResult:
        del request
        raise RuntimeError(f"{self.name} exploded")


class _SlowProvider:
    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self.delay = delay

    async def search(self, request: SearchRequest) -> SearchResult:
        await asyncio.sleep(self.delay)
        return SearchResult(
            outcome="found",
            provider=self.name,
            query=request.query,
            effective_query=request.query,
            hits=[
                SearchHit(
                    title="late",
                    url=f"https://{self.name}.example/late",
                    snippet="late result",
                    provider=self.name,
                )
            ],
        )


class SearchPolicyTests(unittest.TestCase):
    def test_default_registry_exposes_anonymous_anysearch(self) -> None:
        reset_provider_registry()
        providers = get_provider_registry().list_configs().providers
        anysearch = next(
            item for item in providers if item.provider_key == "anysearch"
        )
        self.assertTrue(anysearch.enabled)
        self.assertEqual(anysearch.provider_type, "web_search")
        self.assertIn("web_search", anysearch.capabilities)
        self.assertTrue(anysearch.settings["allow_anonymous"])
        self.assertNotEqual(
            anysearch.health.status,
            "missing_credentials",
        )

    def test_douban_book_constraint_targets_entity_pages(self) -> None:
        request = build_research_search_request(
            "豆瓣好评图书，给我5本好书推荐",
            constraints=[
                {
                    "field": "source_domain",
                    "operator": "in",
                    "value": ["book.douban.com"],
                },
                {"field": "rating", "operator": "gte", "value": "好评"},
                {"field": "result_limit", "operator": "equals", "value": 5},
            ],
        )
        self.assertEqual(request.include_domains, ["book.douban.com"])
        self.assertIn("site:book.douban.com/subject/", request.query)
        self.assertEqual(
            request.include_url_prefixes,
            ["https://book.douban.com/subject/"],
        )
        self.assertIn("review", request.requirements)
        self.assertIn("result_limit", request.requirements)
        self.assertEqual(request.max_results, 5)

    def test_chinese_general_query_prefers_tavily(self) -> None:
        request = SearchRequest(query="今天有什么新闻")
        self.assertEqual(
            provider_order(request),
            ("tavily", "ddgs", "anysearch"),
        )

    def test_explicit_domain_order_is_quality_first(self) -> None:
        request = SearchRequest(
            query="well reviewed books",
            include_domains=["example.test"],
        )
        self.assertEqual(
            provider_order(request),
            ("tavily", "ddgs", "anysearch"),
        )

    def test_chinese_domain_query_prefers_tavily(self) -> None:
        request = SearchRequest(
            query="豆瓣好评图书",
            include_domains=["book.douban.com"],
        )
        self.assertEqual(
            provider_order(request),
            ("tavily", "ddgs", "anysearch"),
        )

    def test_next_round_moves_used_provider_to_end(self) -> None:
        request = SearchRequest(query="中文图书")
        self.assertEqual(
            provider_order(request, previously_used=("tavily",)),
            ("ddgs", "anysearch", "tavily"),
        )


class SearchGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_federated_search_keeps_complete_fast_success_without_laggard(self) -> None:
        gateway = SearchGateway(
            {
                "tavily": _FakeProvider(
                    "tavily",
                    "found",
                    url="https://fast.example/result",
                ),
                "ddgs": _SlowProvider("ddgs", delay=5.0),
            }
        )
        started = time.perf_counter()
        result = await gateway.search(
            SearchRequest(
                query="fast partial success",
                strategy="federated",
                provider_budget=2,
                max_results=1,
            )
        )
        elapsed = time.perf_counter() - started

        self.assertEqual(result.outcome, "found")
        self.assertEqual([hit.provider for hit in result.hits], ["tavily"])
        self.assertLess(elapsed, 2.5)
        self.assertTrue(
            any(
                attempt.error_type == "laggard_cancelled"
                for attempt in result.attempts
            )
        )

    async def test_federated_search_waits_briefly_when_recall_is_low(self) -> None:
        gateway = SearchGateway(
            {
                "tavily": _FakeProvider(
                    "tavily",
                    "found",
                    url="https://fast.example/result",
                ),
                "ddgs": _SlowProvider("ddgs", delay=0.1),
            }
        )
        result = await gateway.search(
            SearchRequest(
                query="recall-aware portfolio",
                strategy="federated",
                provider_budget=2,
                max_results=5,
            )
        )

        self.assertEqual(result.outcome, "found")
        self.assertEqual(
            [hit.provider for hit in result.hits],
            ["tavily", "ddgs"],
        )

    async def test_gateway_enforces_include_and_exclude_domains(self) -> None:
        gateway = SearchGateway(
            {
                "tavily": _FakeProvider(
                    "tavily",
                    "found",
                    url="https://blocked.example/result",
                ),
                "ddgs": _FakeProvider(
                    "ddgs",
                    "found",
                    url="https://sub.allowed.example/result",
                ),
            }
        )
        result = await gateway.search(
            SearchRequest(
                query="domain constrained",
                strategy="federated",
                provider_budget=2,
                include_domains=["allowed.example"],
                exclude_domains=["blocked.example"],
            )
        )

        self.assertEqual(result.outcome, "found")
        self.assertEqual(
            [hit.url for hit in result.hits],
            ["https://sub.allowed.example/result"],
        )

    async def test_federated_search_merges_providers_in_balanced_order(self) -> None:
        tavily = _FakeProvider(
            "tavily", "found", url="https://one.example/a"
        )
        ddgs = _FakeProvider(
            "ddgs", "found", url="https://two.example/b"
        )
        anysearch = _FakeProvider(
            "anysearch", "found", url="https://three.example/c"
        )
        gateway = SearchGateway(
            {"tavily": tavily, "ddgs": ddgs, "anysearch": anysearch}
        )

        result = await gateway.search(
            SearchRequest(
                query="multi source books",
                strategy="federated",
                provider_budget=3,
                max_results=3,
            )
        )

        self.assertEqual(result.outcome, "found")
        self.assertEqual(result.provider, "tavily,ddgs,anysearch")
        self.assertEqual(
            [hit.provider for hit in result.hits],
            ["tavily", "ddgs", "anysearch"],
        )
        self.assertEqual(
            result.metadata["successful_providers"],
            ["tavily", "ddgs", "anysearch"],
        )
        self.assertEqual(tavily.calls, 1)
        self.assertEqual(ddgs.calls, 1)
        self.assertEqual(anysearch.calls, 1)

    async def test_federated_search_deduplicates_same_source_url(self) -> None:
        gateway = SearchGateway(
            {
                "tavily": _FakeProvider(
                    "tavily", "found", url="https://example.test/book?q=1"
                ),
                "ddgs": _FakeProvider(
                    "ddgs", "found", url="https://example.test/book?q=2"
                ),
            }
        )

        result = await gateway.search(
            SearchRequest(
                query="same source",
                strategy="federated",
                provider_budget=2,
                max_results=5,
            )
        )

        self.assertEqual(len(result.hits), 1)
        self.assertEqual(len(result.attempts), 2)

    async def test_federated_success_keeps_provider_failure_as_warning(self) -> None:
        gateway = SearchGateway(
            {
                "tavily": _FakeProvider("tavily", "unavailable"),
                "ddgs": _FakeProvider("ddgs", "found"),
            }
        )

        result = await gateway.search(
            SearchRequest(
                query="multi source fallback",
                strategy="federated",
                provider_budget=2,
            )
        )

        self.assertEqual(result.outcome, "found")
        self.assertEqual(result.error, "")
        self.assertEqual(result.metadata["provider_warning_count"], 1)
        self.assertEqual(len(result.attempts), 2)

    async def test_tavily_failure_falls_back_to_ddgs(self) -> None:
        tavily = _FakeProvider("tavily", "unavailable")
        ddgs = _FakeProvider(
            "ddgs",
            "found",
            url="https://example.test/ddgs-result",
        )
        gateway = SearchGateway(
            {"tavily": tavily, "ddgs": ddgs}
        )
        result = await gateway.search(
            SearchRequest(
                query="well reviewed books",
                include_domains=["example.test"],
            )
        )
        self.assertEqual(result.outcome, "found")
        self.assertEqual(result.provider, "ddgs")
        self.assertEqual(
            [attempt.provider for attempt in result.attempts],
            ["tavily", "ddgs"],
        )
        self.assertEqual(tavily.calls, 1)
        self.assertEqual(ddgs.calls, 1)

    async def test_tavily_and_ddgs_failure_fall_back_to_anysearch(self) -> None:
        tavily = _FakeProvider("tavily", "unavailable")
        ddgs = _FakeProvider("ddgs", "unavailable")
        anysearch = _FakeProvider("anysearch", "found")
        gateway = SearchGateway(
            {"tavily": tavily, "ddgs": ddgs, "anysearch": anysearch}
        )
        result = await gateway.search(SearchRequest(query="中文图书推荐"))
        self.assertEqual(result.outcome, "found")
        self.assertEqual(result.provider, "anysearch")
        self.assertEqual(tavily.calls, 1)
        self.assertEqual(ddgs.calls, 1)
        self.assertEqual(anysearch.calls, 1)

    async def test_adapter_exception_does_not_break_failover(self) -> None:
        gateway = SearchGateway(
            {
                "tavily": _RaisingProvider("tavily"),
                "anysearch": _FakeProvider(
                    "anysearch",
                    "found",
                    url="https://example.test/anysearch-result",
                ),
            }
        )
        result = await gateway.search(
            SearchRequest(
                query="site result",
                include_domains=["example.test"],
            )
        )
        self.assertEqual(result.outcome, "found")
        self.assertEqual(result.provider, "anysearch")
        self.assertEqual(result.attempts[0].error_type, "adapter_error")

    async def test_url_filter_can_fall_back_to_next_provider(self) -> None:
        gateway = SearchGateway(
            {
                "tavily": _FakeProvider(
                    "tavily",
                    "found",
                    url="https://example.test/chart",
                ),
                "anysearch": _FakeProvider(
                    "anysearch",
                    "found",
                    url="https://example.test/subject/42",
                ),
            }
        )
        result = await gateway.search(
            SearchRequest(
                query="books",
                include_domains=["example.test"],
                include_url_prefixes=["https://example.test/subject/"],
            )
        )
        self.assertEqual(result.provider, "anysearch")
        self.assertEqual(result.outcome, "found")
        self.assertEqual(result.attempts[0].error_type, "filtered_empty")

    async def test_both_unavailable_is_completed_domain_outcome(self) -> None:
        gateway = SearchGateway(
            {
                "tavily": _FakeProvider("tavily", "unavailable"),
                "anysearch": _FakeProvider("anysearch", "unavailable"),
            }
        )
        result = await gateway.search(
            SearchRequest(
                query="豆瓣好评图书",
                include_domains=["book.douban.com"],
            )
        )
        self.assertEqual(result.execution_status, "completed")
        self.assertEqual(result.outcome, "unavailable")
        self.assertEqual(len(result.attempts), 3)
        self.assertEqual(
            [attempt.provider for attempt in result.attempts],
            ["tavily", "ddgs", "anysearch"],
        )

        projected = project_research_search_output(
            task=ResearchSearchTask(
                round_index=1,
                objective="豆瓣好评图书",
                query="豆瓣好评图书",
                include_domains=["book.douban.com"],
            ),
            provider_output=result.model_dump(mode="json"),
        )
        self.assertEqual(projected.status, "completed")
        self.assertEqual(projected.provider_status, "unavailable")
        self.assertEqual(projected.source_count, 0)

    async def test_empty_is_distinct_from_unavailable(self) -> None:
        gateway = SearchGateway(
            {
                "tavily": _FakeProvider("tavily", "empty"),
                "anysearch": _FakeProvider("anysearch", "unavailable"),
            }
        )
        result = await gateway.search(
            SearchRequest(query="rare", include_domains=["example.test"])
        )
        self.assertEqual(result.outcome, "empty")
        self.assertEqual(result.execution_status, "completed")


if __name__ == "__main__":
    unittest.main()
