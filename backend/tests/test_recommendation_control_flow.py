from __future__ import annotations

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

from app.models.book import Book
from app.services.agent_core.capabilities import CapabilityRegistry
from app.services.agent_core.contracts import (
    AgentCoreTurnResult,
    ControllerOutput,
    ControllerToolCall,
)
from app.services.agent_core.controller_client import ControllerClient
from app.services.agent_core.compiler import WorkflowCompiler
from app.services.agent_core.harness import AgentCoreHarness
from app.services.agent_core.publication.deterministic import (
    DeterministicReceiptRenderer,
)
from app.services.agent_core.prompt_contracts import (
    ControllerContextSnapshot,
    ControllerModelRequest,
)
from app.services.agent_core.proposal_validator import ProposalValidator
from app.services.agent_core.receipt_projector import ReceiptContextProjector
from app.services.agent_core.turn_loop import TurnControllerLoop
from app.services.agent_runtime.contracts import (
    ActionPlan,
    ActionReceipt,
    ExecutionContext,
    PlanReceipt,
    PlannedAction,
)
from app.services.book_search import (
    BookSearchCacheResult,
    _merge_book_candidates,
    search_external_book_candidates,
)
from app.services.book_search_contracts import BookCandidateSearchResult
from app.services.books.recommendation_service import (
    RecommendationService,
    _clean_catalog_description,
    _discovery_queries,
)
from app.services.books.douban_catalog import (
    DoubanCatalogProvider,
    catalog_query_backoffs,
    normalize_douban_suggestions,
)
from app.services.recommendation_projection import _shelf_state_for_book
from app.services.recommendation_constraints import (
    PersonalizedRecommendationConstraints,
    _add_shelf_reference_titles,
)
from app.services.external_capabilities.availability import (
    ExternalCapabilityAvailability,
)
from app.services.external_capabilities.contracts import BookSearchInput
from app.services.external_search.contracts import SearchHit, SearchResult


def _registry() -> CapabilityRegistry:
    return CapabilityRegistry(
        availability=ExternalCapabilityAvailability(book_search=True)
    )


class RecommendationProposalTests(unittest.TestCase):
    def test_exact_book_lookup_uses_receipt_response_without_second_model(self) -> None:
        output = ControllerOutput(
            mode="capability_proposals",
            tool_calls=[
                ControllerToolCall(
                    call_id="lookup",
                    name="book_search",
                    arguments={
                        "query": "目标作品",
                        "mode": "lookup",
                    },
                )
            ],
        )
        plan = WorkflowCompiler().compile(
            ProposalValidator(_registry()).validate(output),
            goal="查找目标作品",
        )
        self.assertEqual(plan.response_mode, "receipt")
        receipt = PlanReceipt(
            plan_id=plan.plan_id,
            request_id="lookup-receipt",
            route_type=plan.route_type,
            intent=plan.intent,
            status="completed",
            actions=[
                ActionReceipt(
                    action_id=plan.actions[0].action_id,
                    capability="books",
                    operation="book_search_v1",
                    status="completed",
                    admitted=True,
                    output={
                        "status": "ok",
                        "query": "目标作品",
                        "candidate_count": 1,
                        "sources": [
                            {
                                "title": "目标作品",
                                "url": "https://book.example/exact",
                                "snippet": "8.0 热门 ### 5 有用 2020-01-01 10:20 评论内容",
                            }
                        ],
                    },
                )
            ],
        )
        answer = DeterministicReceiptRenderer().render(plan, receipt)
        self.assertIn("[目标作品](https://book.example/exact)", answer.content)
        self.assertNotIn("评论内容", answer.content)

    def test_multiple_anchor_searches_collapse_to_one_owner_call(self) -> None:
        output = ControllerOutput(
            mode="capability_proposals",
            tool_calls=[
                ControllerToolCall(
                    call_id="communication",
                    name="book_search",
                    arguments={
                        "query": "类似沟通类参考书的实用读物",
                        "genres": ["沟通"],
                        "reference_titles": ["示例甲"],
                        "limit": 5,
                    },
                ),
                ControllerToolCall(
                    call_id="finance",
                    name="book_search",
                    arguments={
                        "query": "类似财商类参考书的入门读物",
                        "genres": ["财商"],
                        "reference_titles": ["示例乙"],
                        "excluded_titles": ["不要推荐的书"],
                        "publication_year_from": 2025,
                        "limit": 8,
                    },
                ),
            ],
        )

        batch = ProposalValidator(_registry()).validate(output)

        self.assertEqual(len(batch.proposals), 1)
        proposal = batch.proposals[0]
        self.assertEqual(proposal.capability, "book_search")
        self.assertIn("沟通", proposal.arguments["query"])
        self.assertIn("财商", proposal.arguments["query"])
        self.assertEqual(proposal.arguments["genres"], ["沟通", "财商"])
        self.assertEqual(
            proposal.arguments["reference_titles"],
            ["示例甲", "示例乙"],
        )
        self.assertEqual(
            proposal.arguments["excluded_titles"],
            ["示例甲", "示例乙", "不要推荐的书"],
        )
        self.assertEqual(proposal.arguments["publication_year_from"], 2025)
        self.assertEqual(proposal.arguments["limit"], 8)

    def test_owner_merge_bounds_many_themes_without_losing_each_branch(self) -> None:
        output = ControllerOutput(
            mode="capability_proposals",
            tool_calls=[
                ControllerToolCall(
                    call_id="communication-many",
                    name="book_search",
                    arguments={
                        "query": "沟通与倾听方向的深入推荐",
                        "themes": [
                            "沟通表达",
                            "真正倾听",
                            "非暴力沟通",
                            "人际关系",
                            "冲突处理",
                        ],
                        "reference_titles": ["非暴力沟通"],
                        "response_depth": "deep",
                    },
                ),
                ControllerToolCall(
                    call_id="finance-many",
                    name="book_search",
                    arguments={
                        "query": "成年人理财与金钱观的深入推荐",
                        "themes": [
                            "日常理财",
                            "金钱心理",
                            "小狗钱钱",
                            "长期投资",
                            "消费决策",
                        ],
                        "reference_titles": ["小狗钱钱"],
                        "response_depth": "deep",
                    },
                ),
            ],
        )

        batch = ProposalValidator(_registry()).validate(output)

        self.assertEqual(len(batch.proposals), 1)
        themes = batch.proposals[0].arguments["themes"]
        self.assertLessEqual(len(themes), 6)
        self.assertIn("沟通表达", themes)
        self.assertIn("日常理财", themes)
        self.assertNotIn("非暴力沟通", themes)
        self.assertNotIn("小狗钱钱", themes)

    def test_dirty_cached_source_cannot_break_receipt_projection(self) -> None:
        receipt = PlanReceipt(
            plan_id="dirty-source-plan",
            request_id="dirty-source-request",
            route_type="slow_path",
            intent="book_recommendation",
            status="completed",
            actions=[
                ActionReceipt(
                    action_id="dirty-source-action",
                    capability="books",
                    operation="book_search_v1",
                    status="completed",
                    output={
                        "status": "ok",
                        "query": "任意换表达",
                        "sources": [
                            {"title": "旧脏缓存", "url": "", "snippet": ""}
                        ],
                    },
                    admitted=True,
                )
            ],
        )
        projected = ReceiptContextProjector().project(receipt)
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0].sources, [])

    def test_web_search_cannot_bypass_recommendation_owner(self) -> None:
        registry = CapabilityRegistry(
            availability=ExternalCapabilityAvailability(
                book_search=True,
                web_search=True,
            )
        )
        output = ControllerOutput(
            mode="capability_proposals",
            tool_calls=[
                ControllerToolCall(
                    call_id="books",
                    name="book_search",
                    arguments={
                        "query": "完整保留用户给出的多个参考标题",
                        "mode": "recommendation",
                    },
                ),
                ControllerToolCall(
                    call_id="web",
                    name="web_search",
                    arguments={"query": "同一个推荐任务的网页候选"},
                ),
            ],
        )
        batch = ProposalValidator(registry).validate(output)
        self.assertEqual(
            [proposal.capability for proposal in batch.proposals],
            ["book_search"],
        )


class RecommendationContractTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_description_drops_search_page_chrome(self) -> None:
        self.assertEqual(
            _clean_catalog_description(
                "投诉建议 举报不良信息 使用百度前必读 百科协议 京ICP证030173号"
            ),
            "",
        )

    def test_fallback_recommendation_expands_only_explicit_reference_titles(self) -> None:
        request = BookSearchInput(
            query="类似《非暴力沟通》、《小狗钱钱》的书 推荐",
            mode="recommendation",
            reference_titles=["非暴力沟通", "小狗钱钱"],
            excluded_titles=["非暴力沟通", "小狗钱钱"],
        )

        self.assertEqual(
            _discovery_queries(request=request, effective_query=request.query),
            [
                "类似《非暴力沟通》的书 推荐",
                "类似《小狗钱钱》的书 推荐",
                "类似《非暴力沟通》、《小狗钱钱》的书 推荐",
            ],
        )

    def test_shelf_personalization_adds_concrete_discovery_anchors(self) -> None:
        request = BookSearchInput(
            query="根据我的书架推荐不重复的新书",
            mode="recommendation",
        )

        self.assertEqual(
            _discovery_queries(
                request=request,
                effective_query=request.query,
                personalization_reference_titles=["小狗钱钱", "Python深度学习"],
            ),
            [
                "类似《小狗钱钱》的书 推荐",
                "类似《Python深度学习》的书 推荐",
                "根据我的书架推荐不重复的新书",
            ],
        )

    async def test_shelf_context_prefers_positive_reading_assets(self) -> None:
        service = AsyncMock()
        service.list_entries.return_value = (
            [
                SimpleNamespace(
                    title="正在读且喜欢",
                    reading_status="reading",
                    evaluation="liked",
                    rating=None,
                ),
                SimpleNamespace(
                    title="已读且喜欢",
                    reading_status="read",
                    evaluation="liked",
                    rating=5,
                ),
                SimpleNamespace(
                    title="想读且喜欢",
                    reading_status="want_to_read",
                    evaluation="liked",
                    rating=None,
                ),
                SimpleNamespace(
                    title="明确不喜欢",
                    reading_status="read",
                    evaluation="disliked",
                    rating=None,
                ),
            ],
            4,
        )
        constraints = PersonalizedRecommendationConstraints(
            user_id=uuid.uuid4(),
            original_query="根据我的书架推荐不重复的新书",
            effective_query="根据我的书架推荐不重复的新书",
        )

        with patch(
            "app.services.books.reading_service.ReadingService",
            return_value=service,
        ):
            await _add_shelf_reference_titles(
                None,
                user_id=constraints.user_id,
                constraints=constraints,
            )

        self.assertEqual(
            constraints.shelf_reference_titles,
            ["已读且喜欢", "正在读且喜欢", "想读且喜欢"],
        )
        self.assertEqual(constraints.metadata["shelf_context_status"], "available")
        self.assertEqual(constraints.metadata["shelf_entry_count"], 4)

    def test_sparse_model_candidates_are_supplemented_from_reference_titles(self) -> None:
        request = BookSearchInput(
            query="类似《非暴力沟通》、《小狗钱钱》的书 推荐",
            mode="recommendation",
            limit=3,
            candidate_titles=["人性的弱点", "富爸爸穷爸爸"],
            reference_titles=["非暴力沟通", "小狗钱钱"],
            excluded_titles=["非暴力沟通", "小狗钱钱"],
        )

        self.assertEqual(
            _discovery_queries(request=request, effective_query=request.query),
            [
                "《人性的弱点》 作者 出版社 内容简介",
                "《富爸爸穷爸爸》 作者 出版社 内容简介",
                "类似 非暴力沟通 的书 推荐",
                "类似 小狗钱钱 的书 推荐",
            ],
        )

    async def test_candidate_fusion_deduplicates_editions_and_canonical_urls(self) -> None:
        candidates = [
            Book(
                title="通用作品名 (修订版)",
                source_name="douban",
                source_url="https://book.douban.com/subject/1234567",
                external_id="1234567",
            ),
            Book(
                title="通用作品名（修订版）",
                source_name="douban",
                source_url="https://book.douban.com/subject/1234567/",
                external_id="1234567",
            ),
            Book(
                title="通用作品名",
                source_name="catalog",
                source_url="https://catalog.example/books/work",
            ),
        ]

        merged = _merge_book_candidates(candidates, limit=5)

        self.assertEqual(len(merged), 1)

    async def test_catalog_provider_rejects_douban_tag_pages(self) -> None:
        gateway = AsyncMock()
        gateway.search.return_value = SearchResult(
            outcome="found",
            provider="fixture",
            query="通用推荐",
            hits=[
                SearchHit(
                    title="标签聚合页",
                    url="https://book.douban.com/tag/example",
                    snippet="多本书的聚合内容",
                    provider="fixture",
                ),
                SearchHit(
                    title="具体候选书",
                    url="https://book.douban.com/subject/1234567/",
                    snippet="作者：示例作者",
                    provider="fixture",
                ),
            ],
        )
        with patch(
            "app.services.book_search.DoubanCatalogProvider.search",
            AsyncMock(
                return_value=BookCandidateSearchResult(
                    query="通用推荐",
                    provider_query="通用推荐",
                    status="empty_result",
                    source="douban_catalog",
                )
            ),
        ), patch(
            "app.services.book_search.get_search_gateway",
            return_value=gateway,
        ):
            result = await search_external_book_candidates("通用推荐", limit=5)
        self.assertEqual(result.status, "ok")
        self.assertEqual(
            [item["title"] for item in result.candidates],
            ["具体候选书"],
        )
        request = gateway.search.await_args.args[0]
        self.assertIn("豆瓣读书", request.query)
        self.assertEqual(request.include_domains, ["book.douban.com"])
        self.assertEqual(request.include_url_prefixes, [])

    async def test_recommendation_discovery_prefers_semantic_web_results(self) -> None:
        gateway = AsyncMock()
        gateway.search.return_value = SearchResult(
            outcome="found",
            provider="tavily,ddgs",
            query="沟通倾听",
            hits=[
                SearchHit(
                    title="网页发现候选 - 豆瓣读书",
                    url="https://book.douban.com/subject/7654321/",
                    snippet="围绕沟通和倾听的书目介绍。",
                    provider="tavily",
                )
            ],
        )
        catalog = BookCandidateSearchResult(
            query="沟通倾听",
            provider_query="沟通",
            status="ok",
            source="douban_catalog",
            candidates=[
                {
                    "title": "目录候选",
                    "url": "https://book.douban.com/subject/1234567/",
                    "author_name": "示例作者",
                }
            ],
        )
        with patch(
            "app.services.book_search.DoubanCatalogProvider.search",
            AsyncMock(return_value=catalog),
        ), patch(
            "app.services.book_search.get_search_gateway",
            return_value=gateway,
        ):
            result = await search_external_book_candidates(
                "沟通倾听",
                limit=4,
                federated_discovery=True,
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(
            [item["title"] for item in result.candidates],
            ["网页发现候选"],
        )
        request = gateway.search.await_args.args[0]
        self.assertEqual(request.strategy, "federated")
        self.assertEqual(request.provider_budget, 3)

    def test_douban_catalog_normalizer_keeps_only_exact_book_records(self) -> None:
        candidates = normalize_douban_suggestions(
            [
                {
                    "title": "候选书",
                    "url": "https://book.douban.com/subject/1234567/",
                    "pic": "https://img.example/cover.jpg",
                    "author_name": "示例作者",
                    "year": "2024",
                    "type": "b",
                },
                {
                    "title": "聚合页",
                    "url": "https://book.douban.com/tag/example",
                    "type": "b",
                },
            ],
            limit=5,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "候选书")
        self.assertEqual(candidates[0]["author_name"], "示例作者")

    def test_catalog_backoff_is_lexical_and_topic_agnostic(self) -> None:
        self.assertEqual(
            catalog_query_backoffs("抽象维度"),
            ["抽象维度", "抽象维", "抽象"],
        )
        self.assertEqual(
            catalog_query_backoffs("system design"),
            ["system design", "system", "design"],
        )
        phrase_backoffs = catalog_query_backoffs("好好表达 沟通倾听技巧")
        self.assertEqual(phrase_backoffs[0], "好好表达 沟通倾听技巧")
        self.assertIn("好好表达", phrase_backoffs)
        self.assertIn("沟通倾听技巧", phrase_backoffs)
        audience_backoffs = catalog_query_backoffs("成人理财 金钱观")
        self.assertLess(
            audience_backoffs.index("理财"),
            audience_backoffs.index("成人理"),
        )

    async def test_catalog_backoff_keeps_searching_when_first_hit_is_too_narrow(self) -> None:
        payloads = {
            "亲密关系沟通": [
                {
                    "title": "亲密关系",
                    "url": "https://book.douban.com/subject/1000001/",
                    "type": "b",
                }
            ],
            "亲密关系沟": [],
            "亲密关系": [
                {
                    "title": "爱的沟通练习",
                    "url": "https://book.douban.com/subject/1000002/",
                    "type": "b",
                }
            ],
        }

        class FakeResponse:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self, content_type=None):
                return self.payload

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, _url, *, params):
                return FakeResponse(payloads.get(params["q"], []))

        with patch(
            "app.services.books.douban_catalog.aiohttp.ClientSession",
            return_value=FakeSession(),
        ):
            result = await DoubanCatalogProvider().search(
                "亲密关系沟通",
                limit=2,
            )

        self.assertEqual(
            [item["title"] for item in result.candidates],
            ["亲密关系", "爱的沟通练习"],
        )
        self.assertIn("亲密关系", result.provider_query)

    async def test_structured_adult_theme_rejects_explicit_child_packaging(self) -> None:
        child = Book(
            title="儿童绘本财商启蒙（小学生版）",
            authors=["甲"],
            tags=[],
            source_name="douban",
            source_url="https://book.example/child",
            raw_data={},
        )
        adult = Book(
            title="成年人日常理财方法",
            authors=["乙"],
            tags=[],
            source_name="douban",
            source_url="https://book.example/adult",
            raw_data={},
        )
        unrelated = Book(
            title="全国成人理工医专升本考试大纲",
            authors=["丙"],
            tags=[],
            source_name="douban",
            source_url="https://book.example/exam",
            raw_data={},
        )
        cache = BookSearchCacheResult(
            query="成人理财 金钱观",
            status="ok",
            books=[child, unrelated, adult],
        )
        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache),
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                BookSearchInput(
                    query="成人沟通技巧与理财金钱观阅读推荐",
                    mode="recommendation",
                    themes=["成人理财 金钱观"],
                ),
                user_id=None,
            )

        self.assertEqual([item.title for item in evidence.items], ["成年人日常理财方法"])

    async def test_audience_neutral_request_rejects_strong_child_packaging(self) -> None:
        child = Book(
            title="儿童财商启蒙全书",
            authors=["甲"],
            tags=[],
            source_name="douban",
            source_url="https://book.example/child",
            raw_data={},
        )
        general = Book(
            title="日常理财方法",
            authors=["乙"],
            tags=[],
            source_name="douban",
            source_url="https://book.example/general",
            raw_data={},
        )
        cache = BookSearchCacheResult(
            query="理财 金钱观",
            status="ok",
            books=[child, general],
        )
        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache),
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                BookSearchInput(
                    query="理财与金钱观阅读推荐",
                    mode="recommendation",
                    themes=["理财 金钱观"],
                ),
                user_id=None,
            )

        self.assertEqual([item.title for item in evidence.items], ["日常理财方法"])

    async def test_structured_child_audience_allows_child_packaging(self) -> None:
        child = Book(
            title="儿童绘本财商启蒙（小学生版）",
            authors=["甲"],
            tags=[],
            source_name="douban",
            source_url="https://book.example/child",
            raw_data={},
        )
        cache = BookSearchCacheResult(
            query="儿童财商启蒙",
            status="ok",
            books=[child],
        )
        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache),
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                BookSearchInput(
                    query="儿童财商启蒙阅读推荐",
                    mode="recommendation",
                    themes=["儿童财商启蒙"],
                    audience="小学生",
                ),
                user_id=None,
            )

        self.assertEqual([item.title for item in evidence.items], ["儿童绘本财商启蒙（小学生版）"])

    async def test_edition_variant_of_shelf_book_is_not_a_new_candidate(self) -> None:
        candidate = Book(
            title="通用作品名（修订版）",
            authors=[],
            tags=[],
            source_name="catalog",
            source_url="https://book.example/revised",
            raw_data={},
        )
        state = _shelf_state_for_book(
            candidate,
            {"title:通用作品名": ("want_to_read", None)},
        )
        self.assertEqual(state, ("want_to_read", None))

    async def test_reference_books_are_excluded_at_recommendation_owner_boundary(self) -> None:
        reference = Book(
            title="小狗钱钱 - 读书",
            authors=["博多·舍费尔"],
            tags=[],
            summary="参考书",
            source_name="douban",
            source_url="https://book.example/reference",
            raw_data={},
        )
        candidate = Book(
            title="候选财商读物",
            authors=["示例作者"],
            tags=[],
            summary="轻松易读的财商入门书。",
            source_name="douban",
            source_url="https://book.example/candidate",
            raw_data={},
        )
        cache_result = BookSearchCacheResult(
            query="轻松易读的财商入门",
            status="ok",
            books=[reference, candidate],
        )
        request = BookSearchInput(
            query="轻松易读的财商入门",
            mode="recommendation",
            reference_titles=["《小狗钱钱》"],
        )
        self.assertIn("《小狗钱钱》", request.excluded_titles)
        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache_result),
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                request,
                user_id=None,
            )

        self.assertEqual(
            [source.title for source in evidence.sources],
            ["候选财商读物"],
        )
        personalization = evidence.metadata["personalization"]
        self.assertEqual(
            personalization["request_exclusions"],
            ["小狗钱钱 - 读书"],
        )
        self.assertIn("reference_titles_excluded", evidence.filters_applied)


    async def test_all_theme_contract_keeps_intersection_query_and_admits_only_cross_domain_books(self) -> None:
        communication_only = Book(
            title="关键沟通练习",
            authors=["甲"],
            tags=[],
            summary="聚焦冲突沟通与倾听。",
            source_name="douban",
            source_url="https://book.example/communication-only",
            raw_data={},
        )
        cross_domain = Book(
            title="谈钱不伤感情",
            authors=["乙"],
            tags=[],
            summary="同时讨论家庭理财决策、金钱观与沟通协商。",
            source_name="douban",
            source_url="https://book.example/cross-domain",
            raw_data={},
        )
        search = AsyncMock(
            return_value=BookSearchCacheResult(
                query="用理财思维沟通，用沟通技巧理财",
                status="ok",
                books=[communication_only, cross_domain],
            )
        )
        request = BookSearchInput(
            query="用理财思维沟通，用沟通技巧理财",
            mode="recommendation",
            themes=["沟通", "理财"],
            theme_match="all",
            reference_titles=["非暴力沟通", "小狗钱钱"],
            limit=6,
        )

        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            search,
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                request,
                user_id=None,
            )

        self.assertEqual(search.await_count, 1)
        self.assertEqual(
            search.await_args.kwargs["query"],
            "用理财思维沟通，用沟通技巧理财",
        )
        self.assertEqual(
            [item.title for item in evidence.items],
            ["谈钱不伤感情"],
        )
        self.assertEqual(evidence.theme_match, "all")
        self.assertEqual(evidence.items[0].theme, "沟通 × 理财")

    async def test_multiple_semantic_themes_keep_one_semantic_query(self) -> None:
        communication = Book(
            title="沟通候选",
            authors=["甲"],
            tags=[],
            summary="实用沟通方法。",
            source_name="douban",
            source_url="https://book.example/communication",
            raw_data={},
        )
        finance = Book(
            title="财商候选",
            authors=["乙"],
            tags=[],
            summary="轻松理解金钱观。",
            source_name="douban",
            source_url="https://book.example/finance",
            raw_data={},
        )
        search = AsyncMock(
            return_value=BookSearchCacheResult(
                query="轻松提升沟通和金钱观",
                status="ok",
                books=[communication, finance],
            )
        )
        request = BookSearchInput(
            query="轻松提升沟通和金钱观",
            mode="recommendation",
            themes=["沟通技巧", "金钱观"],
            limit=6,
        )

        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            search,
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                request,
                user_id=None,
            )

        self.assertEqual(search.await_count, 1)
        self.assertEqual(
            search.await_args.kwargs["query"],
            "轻松提升沟通和金钱观",
        )
        self.assertTrue(search.await_args.kwargs["federated_discovery"])
        self.assertEqual(
            [source.title for source in evidence.sources],
            ["沟通候选", "财商候选"],
        )
        self.assertEqual(
            [item.theme for item in evidence.items],
            ["沟通技巧", "金钱观"],
        )
        self.assertEqual(
            [(item.theme, item.status) for item in evidence.coverage],
            [("沟通技巧", "partial"), ("金钱观", "partial")],
        )
        self.assertEqual(
            evidence.metadata["discovery"]["discovery_strategy"],
            "single_query",
        )

    async def test_exact_candidates_use_catalog_fast_path_and_stop_at_limit(self) -> None:
        def result_for(query: str) -> BookSearchCacheResult:
            title = query.split("》", 1)[0].lstrip("《")
            return BookSearchCacheResult(
                query=query,
                status="ok",
                books=[
                    Book(
                        title=title,
                        authors=["作者"],
                        source_name="douban",
                        source_url=(
                            "https://book.douban.com/subject/"
                            f"{1000000 + len(search.await_args_list)}/"
                        ),
                        raw_data={},
                    )
                ],
            )

        search = AsyncMock(
            side_effect=lambda _session, **kwargs: result_for(kwargs["query"])
        )
        request = BookSearchInput(
            query="精准候选书目",
            mode="recommendation",
            candidate_titles=["候选一", "候选二", "候选三", "候选四"],
            limit=3,
        )

        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            search,
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                request,
                user_id=None,
            )

        self.assertEqual(search.await_count, 3)
        self.assertEqual(evidence.candidate_count, 3)
        self.assertTrue(
            all(
                call.kwargs["federated_discovery"] is False
                for call in search.await_args_list
            )
        )

    async def test_filtered_theme_coverage_comes_from_one_semantic_discovery(self) -> None:
        communication = Book(
            title="沟通方法",
            authors=["甲"],
            tags=[],
            summary="系统讲解沟通技巧、倾听和冲突处理方法。",
            source_name="douban",
            source_url="https://book.example/communication-gap",
            raw_data={},
        )
        finance = Book(
            title="金钱观入门",
            authors=["乙"],
            tags=[],
            summary="帮助普通读者理解金钱观和长期理财决策。",
            source_name="douban",
            source_url="https://book.example/finance-gap",
            raw_data={},
        )
        search = AsyncMock(
            return_value=BookSearchCacheResult(
                query="提升沟通能力和金钱观",
                status="ok",
                books=[communication, finance],
            )
        )
        request = BookSearchInput(
            query="提升沟通能力和金钱观",
            mode="recommendation",
            themes=["沟通技巧", "金钱观"],
            response_depth="quick",
            limit=4,
        )

        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            search,
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                request,
                user_id=None,
            )

        self.assertEqual(search.await_count, 1)
        self.assertEqual(
            search.await_args.kwargs["query"],
            "提升沟通能力和金钱观",
        )
        self.assertEqual(
            [item.title for item in evidence.items],
            ["沟通方法", "金钱观入门"],
        )
        self.assertEqual(
            [(item.theme, item.status) for item in evidence.coverage],
            [("沟通技巧", "complete"), ("金钱观", "complete")],
        )

    async def test_sparse_catalog_candidate_is_not_verified_by_query_provenance(self) -> None:
        broad_sparse = Book(
            title="宽泛目录候选",
            authors=["甲"],
            tags=[],
            summary="作者：甲",
            source_name="douban",
            source_url="https://book.example/broad-sparse",
            raw_data={},
        )
        supplemental_sparse = Book(
            title="补搜目录候选",
            authors=["乙"],
            tags=[],
            summary="作者：乙",
            source_name="douban",
            source_url="https://book.example/supplemental-sparse",
            raw_data={},
        )
        search = AsyncMock(
            return_value=BookSearchCacheResult(
                query="提升沟通能力和金钱观",
                status="ok",
                books=[broad_sparse, supplemental_sparse],
            )
        )
        request = BookSearchInput(
            query="提升沟通能力和金钱观",
            mode="recommendation",
            themes=["沟通技巧", "金钱观"],
            response_depth="quick",
            limit=4,
        )

        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            search,
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                request,
                user_id=None,
            )

        self.assertEqual(search.await_count, 1)
        self.assertEqual(evidence.items, [])
        self.assertNotIn(
            "宽泛目录候选",
            [source.title for source in evidence.sources],
        )
        self.assertEqual(
            [(item.theme, item.status) for item in evidence.coverage],
            [("沟通技巧", "missing"), ("金钱观", "missing")],
        )

    async def test_catalog_themes_are_not_polluted_by_audience_prose(self) -> None:
        search = AsyncMock(
            return_value=BookSearchCacheResult(
                query="财商理财",
                status="empty_result",
                books=[],
            )
        )
        request = BookSearchInput(
            query="适合普通读者的财商读物",
            mode="recommendation",
            themes=["财商理财"],
            audience="普通读者",
        )

        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            search,
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                request,
                user_id=None,
            )

        self.assertEqual(
            search.await_args.kwargs["query"],
            "适合普通读者的财商读物",
        )
        self.assertEqual(evidence.coverage[0].status, "missing")

    async def test_owner_enriches_catalog_candidate_from_federated_sources(self) -> None:
        book = Book(
            title="金钱心理学",
            authors=["摩根·豪泽尔"],
            tags=[],
            summary="",
            source_name="douban",
            source_url="https://book.douban.com/subject/1/",
            raw_data={},
        )
        cache = BookSearchCacheResult(
            query="财商心理",
            status="ok",
            books=[book],
        )
        gateway = AsyncMock()
        gateway.search.return_value = SearchResult(
            outcome="found",
            provider="tavily,ddgs",
            query="金钱心理学",
            hits=[
                SearchHit(
                    title="《金钱心理学》内容简介",
                    url="https://publisher.example/money",
                    snippet="讨论行为、情绪与长期金钱决策之间的关系。",
                    provider="tavily",
                ),
                SearchHit(
                    title="金钱心理学适读人群",
                    url="https://review.example/money",
                    snippet="面向希望建立长期金钱观的普通读者。",
                    provider="ddgs",
                ),
            ],
            metadata={"search_strategy": "federated"},
        )
        request = BookSearchInput(
            query="财商心理读物",
            mode="recommendation",
            themes=["财商心理"],
            response_depth="deep",
        )

        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache),
        ):
            evidence = await RecommendationService(
                AsyncMock(),
                search_gateway=gateway,
                enable_external_enrichment=True,
            ).search(request, user_id=None)

        item = evidence.items[0]
        self.assertIn("长期金钱决策", item.summary)
        self.assertEqual(item.theme, "财商心理")
        self.assertEqual(item.evidence_provider_count, 2)
        self.assertEqual(len(item.evidence_sources), 3)
        sent = gateway.search.await_args.args[0]
        self.assertEqual(sent.strategy, "federated")
        self.assertEqual(sent.provider_budget, 3)

    async def test_balanced_enrichment_keeps_every_admitted_candidate(self) -> None:
        books = [
            Book(
                title=f"候选{index}",
                authors=["作者"],
                tags=[],
                summary=f"候选{index}简介",
                source_name="douban",
                source_url=f"https://book.example/{index}",
                raw_data={},
            )
            for index in range(1, 4)
        ]
        cache = BookSearchCacheResult(
            query="候选书单",
            status="ok",
            books=books,
        )
        gateway = AsyncMock()
        gateway.search.return_value = SearchResult(
            outcome="empty",
            provider="ddgs",
            query="候选书单",
        )
        request = BookSearchInput(
            query="候选书单",
            mode="recommendation",
            response_depth="balanced",
            limit=3,
        )

        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache),
        ):
            evidence = await RecommendationService(
                AsyncMock(),
                search_gateway=gateway,
                enable_external_enrichment=True,
            ).search(request, user_id=None)

        self.assertEqual(
            [item.title for item in evidence.items],
            ["候选1", "候选2", "候选3"],
        )
        self.assertEqual(gateway.search.await_count, 3)
        self.assertTrue(
            all(
                call.args[0].provider_budget == 2
                for call in gateway.search.await_args_list
            )
        )

    async def test_empty_candidates_skip_enrichment_without_error(self) -> None:
        cache = BookSearchCacheResult(
            query="没有匹配项的主题",
            status="empty_result",
            books=[],
        )
        gateway = AsyncMock()
        request = BookSearchInput(
            query="没有匹配项的主题",
            mode="recommendation",
            response_depth="balanced",
            limit=3,
        )

        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache),
        ):
            evidence = await RecommendationService(
                AsyncMock(),
                search_gateway=gateway,
                enable_external_enrichment=True,
            ).search(request, user_id=None)

        self.assertEqual(evidence.status, "empty_result")
        self.assertEqual(evidence.items, [])
        self.assertEqual(evidence.sources, [])
        gateway.search.assert_not_awaited()

    async def test_publication_year_is_sent_to_discovery_and_verified(self) -> None:
        old = Book(
            title="旧候选",
            authors=["甲"],
            tags=[],
            summary="",
            rating=Decimal("8.0"),
            source_name="douban",
            source_url="https://book.example/old",
            raw_data={"publication_year": 2024},
        )
        current = Book(
            title="新候选",
            authors=["乙"],
            tags=[],
            summary="",
            rating=Decimal("8.5"),
            source_name="douban",
            source_url="https://book.example/current",
            raw_data={"publication_year": 2026},
        )
        cache_result = BookSearchCacheResult(
            query="query",
            status="ok",
            books=[old, current],
        )
        request = BookSearchInput(
            query="实用型个人成长读物",
            mode="lookup",
            publication_year_from=2026,
            publication_year_to=2026,
            limit=5,
        )
        search = AsyncMock(return_value=cache_result)

        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            search,
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                request,
                user_id=uuid.uuid4(),
            )

        self.assertEqual([source.title for source in evidence.sources], ["新候选"])
        self.assertEqual(evidence.candidate_count, 1)
        self.assertIn("publication_year_from_verified", evidence.filters_applied)
        kwargs = search.await_args.kwargs
        self.assertTrue(kwargs["force_external"])
        self.assertIn("2026", kwargs["query"])

    async def test_catalog_page_chrome_is_removed_from_book_evidence(self) -> None:
        book = Book(
            title="通用作品",
            authors=[],
            tags=[],
            summary=(
                "购买纸质书 - 示例商城 19.90 元\n"
                "## 在哪儿借这本书\n示例图书馆\n"
                "## 谁读这本书\n"
                "[...] 《通用作品》通过一个完整故事介绍基础知识，"
                "并给初学者提供清晰、可实践的方法。\n"
                "## 通用作品的创作者\n示例作者 作者\n"
                "[...] 第一章 开始 第二章 继续 第三章 总结"
            ),
            source_name="douban",
            source_url="https://book.example/catalog-entry",
            raw_data={},
        )
        cache_result = BookSearchCacheResult(
            query="通用作品",
            status="ok",
            books=[book],
        )
        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache_result),
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                BookSearchInput(query="通用作品", mode="lookup", limit=5),
                user_id=None,
            )

        self.assertEqual(evidence.status, "ok")
        self.assertEqual(len(evidence.sources), 1)
        snippet = evidence.sources[0].snippet
        self.assertIn("提供清晰、可实践的方法", snippet)
        self.assertNotIn("购买纸质书", snippet)
        self.assertNotIn("图书馆", snippet)
        self.assertNotIn("第一章", snippet)

    async def test_author_biography_is_not_admitted_as_catalog_description(self) -> None:
        book = Book(
            title="财商入门书",
            authors=["示例作者"],
            tags=[],
            summary=(
                "作者：示例作者 作者简介 示例作者出生于某地，"
                "毕业于某大学，现任企业家并著有多部作品。"
            ),
            source_name="douban",
            source_url="https://book.example/money",
            raw_data={},
        )
        cache_result = BookSearchCacheResult(
            query="财商入门书",
            status="ok",
            books=[book],
        )
        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache_result),
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                BookSearchInput(query="财商入门书", mode="lookup", limit=5),
                user_id=None,
            )

        self.assertEqual(evidence.sources[0].snippet, "作者：示例作者")
        self.assertNotIn("出生于", evidence.sources[0].snippet)

    async def test_inline_content_section_survives_author_biography_cleanup(self) -> None:
        book = Book(
            title="人工智能入门",
            authors=["示例作者"],
            tags=[],
            summary=(
                "作者简介 示例作者出生于某地并任职于某大学。 "
                "内容简介 本书通过案例讲解人工智能基础知识，"
                "并为初学者提供循序渐进的练习。 作者介绍 其他资料"
            ),
            source_name="douban",
            source_url="https://book.example/ai",
            raw_data={},
        )
        cache_result = BookSearchCacheResult(
            query="人工智能入门",
            status="ok",
            books=[book],
        )
        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache_result),
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                BookSearchInput(query="人工智能入门", mode="lookup", limit=5),
                user_id=None,
            )

        snippet = evidence.sources[0].snippet
        self.assertIn("通过案例讲解人工智能基础知识", snippet)
        self.assertNotIn("出生于", snippet)
        self.assertNotIn("其他资料", snippet)

    async def test_lookup_keeps_exact_title_and_drops_review_snippets(self) -> None:
        exact = Book(
            title="目标作品",
            authors=[],
            tags=[],
            summary="读书很慢，但整体不错。我要写书评",
            source_name="douban",
            source_url="https://book.example/exact",
            raw_data={},
        )
        similar = Book(
            title="目标作品实践篇 - 读书",
            authors=[],
            tags=[],
            summary="目标作品简介 / 001 第一章 开始 / 020 第二章 继续",
            source_name="douban",
            source_url="https://book.example/similar",
            raw_data={},
        )
        cache_result = BookSearchCacheResult(
            query="目标作品",
            status="ok",
            books=[exact, similar],
        )
        with patch(
            "app.services.books.recommendation_service.search_and_cache_books_with_status",
            AsyncMock(return_value=cache_result),
        ):
            evidence = await RecommendationService(AsyncMock()).search(
                BookSearchInput(query="《目标作品》", mode="lookup", limit=5),
                user_id=None,
            )

        self.assertEqual([source.title for source in evidence.sources], ["目标作品"])
        self.assertEqual(evidence.sources[0].snippet, "")


class _SynthesisModel:
    def __init__(self) -> None:
        self.bind_called = False

    def bind_tools(self, *args, **kwargs):
        self.bind_called = True
        raise AssertionError("synthesis must not bind tools")

    async def ainvoke(self, messages):
        del messages
        return AIMessage(content="基于已取得的证据完成回答。")


class ControllerSynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesis_phase_exposes_no_tools(self) -> None:
        model = _SynthesisModel()
        client = ControllerClient(
            registry=_registry(),
            model_factory=lambda _: model,
        )
        output = await client.decide(
            ControllerModelRequest(
                phase="synthesis",
                model_name="test",
                current_user_message="推荐几本书",
                context=ControllerContextSnapshot(),
            )
        )
        self.assertEqual(output.mode, "direct_answer")
        self.assertFalse(model.bind_called)


class _RepeatedBookController:
    def __init__(self) -> None:
        self.requests = []

    async def decide(self, request):
        self.requests.append(request)
        return ControllerOutput(
            mode="capability_proposals",
            tool_calls=[
                ControllerToolCall(
                    call_id=f"books-{len(self.requests)}",
                    name="book_search",
                    arguments={"query": "通用自然语言推荐意图"},
                )
            ],
        )


class _BookEvidenceHarness:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, output, *, goal, context, user_input=None):
        if output.mode == "direct_answer":
            return await AgentCoreHarness(registry=_registry()).run(
                output,
                goal=goal,
                context=context,
                user_input=user_input,
            )
        del user_input
        self.calls += 1
        plan = ActionPlan(
            plan_id="book-plan",
            source="controller_proposal",
            route_type="slow_path",
            intent="book_recommendation",
            goal=goal,
            response_mode="model",
            actions=[
                PlannedAction(
                    action_id="book-action",
                    capability="books",
                    operation="book_search_v1",
                )
            ],
        )
        receipt = PlanReceipt(
            plan_id=plan.plan_id,
            request_id=context.request_id,
            route_type=plan.route_type,
            intent=plan.intent,
            status="completed",
            actions=[
                ActionReceipt(
                    action_id="book-action",
                    capability="books",
                    operation="book_search_v1",
                    status="completed",
                    output={
                        "status": "ok",
                        "query": "通用自然语言推荐意图",
                        "candidate_count": 1,
                        "sources": [
                            {
                                "title": "候选书",
                                "url": "https://book.example/candidate",
                                "snippet": "作者：示例作者",
                            }
                        ],
                    },
                    admitted=True,
                )
            ],
        )
        return AgentCoreTurnResult(
            execution_mode="live",
            output=ControllerOutput(
                mode="capability_proposals",
                tool_calls=[
                    ControllerToolCall(
                        call_id="books",
                        name="book_search",
                        arguments={"query": "通用自然语言推荐意图"},
                    )
                ],
            ),
            plan=plan,
            receipt=receipt,
        )


class RecommendationLoopTerminationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_search_in_synthesis_is_not_executed(self) -> None:
        controller = _RepeatedBookController()
        harness = _BookEvidenceHarness()
        result = await TurnControllerLoop(
            controller=controller,
            harness=harness,
        ).run(
            model_request=ControllerModelRequest(
                model_name="test",
                current_user_message="换一种表达也应该稳定推荐",
            ),
            context=ExecutionContext(
                user_id=uuid.uuid4(),
                thread_id=uuid.uuid4(),
                request_id="recommendation-loop-test",
            ),
            goal="换一种表达也应该稳定推荐",
        )

        self.assertEqual(harness.calls, 1)
        self.assertEqual(len(controller.requests), 2)
        self.assertEqual(controller.requests[1].phase, "synthesis")
        self.assertEqual(
            controller.requests[1].context.response_view.books[0].title,
            "候选书",
        )
        self.assertEqual(result.status, "completed")
        self.assertIn("候选书", result.final_answer.content)
        self.assertNotEqual(result.status, "limit_exceeded")

    async def test_invalid_model_synthesis_falls_back_to_receipt_evidence(self) -> None:
        class _Controller:
            def __init__(self) -> None:
                self.calls = 0

            async def decide(self, request):
                self.calls += 1
                if self.calls == 1:
                    return ControllerOutput(
                        mode="capability_proposals",
                        tool_calls=[
                            ControllerToolCall(
                                call_id="books",
                                name="book_search",
                                arguments={"query": "可替换的推荐表达"},
                            )
                        ],
                    )
                self.assert_synthesis(request)
                return ControllerOutput(
                    mode="direct_answer",
                    text='book_search(query="候选书")',
                )

            @staticmethod
            def assert_synthesis(request):
                if request.phase != "synthesis":
                    raise AssertionError("expected synthesis phase")

        result = await TurnControllerLoop(
            controller=_Controller(),
            harness=_BookEvidenceHarness(),
        ).run(
            model_request=ControllerModelRequest(
                model_name="test",
                current_user_message="可替换的推荐表达",
            ),
            context=ExecutionContext(
                user_id=uuid.uuid4(),
                thread_id=uuid.uuid4(),
                request_id="invalid-synthesis-fallback-test",
            ),
            goal="可替换的推荐表达",
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.final_answer.publication_mode, "deterministic_receipt")
        self.assertIn("候选书", result.final_answer.content)
        self.assertNotIn("book_search(", result.final_answer.content)


if __name__ == "__main__":
    unittest.main()
