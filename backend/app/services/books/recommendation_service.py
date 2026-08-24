"""Authoritative ordinary book lookup and recommendation owner."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.book import Book
from app.crud.book import (
    create_book_interaction,
    create_recommendation_event,
    get_book,
    get_or_create_preference_profile,
    list_books,
    list_recommendation_events,
    update_preference_profile,
)
from app.schemas.book import BookInteractionCreate, UserPreferenceProfileUpdate
from app.services.recommendation_signals import RecommendationSignalCreate
from app.services.book_search import search_and_cache_books_with_status
from app.services.book_search import BookSearchCacheResult
from app.services.books.book_identity import normalize_book_work_title
from app.services.external_capabilities.contracts import (
    BookEvidence,
    BookRecommendationItem,
    BookSearchInput,
    BookThemeCoverage,
    ExternalEvidenceSource,
)
from app.services.external_search import SearchRequest, get_search_gateway
from app.services.recommendation_constraints import (
    build_personalized_recommendation_constraints,
)
from app.services.recommendation_projection import RecommendationProjector


class RecommendationService:
    """Own discovery policy; Search only supplies public candidate evidence."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        search_gateway=None,
        enable_external_enrichment: bool | None = None,
    ) -> None:
        self.session = session
        self._search_gateway = search_gateway
        self._enable_external_enrichment = bool(enable_external_enrichment)

    async def list_catalog(
        self,
        *,
        query: str | None,
        limit: int,
        offset: int,
    ):
        return await list_books(
            db=self.session,
            query=query,
            limit=limit,
            offset=offset,
        )

    async def get_catalog_book(self, book_id: UUID):
        return await get_book(db=self.session, book_id=book_id)

    async def record_interaction(self, interaction: BookInteractionCreate):
        return await create_book_interaction(
            db=self.session,
            interaction=interaction,
        )

    async def record_signal(self, signal: RecommendationSignalCreate):
        return await create_recommendation_event(
            db=self.session,
            signal=signal,
        )

    async def list_signals(
        self,
        *,
        user_id: UUID,
        book_title: str,
        event_types: list[str] | None,
        limit: int,
        offset: int,
    ):
        return await list_recommendation_events(
            db=self.session,
            user_id=user_id,
            book_title=book_title,
            event_types=event_types,
            limit=limit,
            offset=offset,
        )

    async def get_preference_profile(self, user_id: UUID):
        return await get_or_create_preference_profile(self.session, user_id)

    async def update_preference_profile(
        self,
        user_id: UUID,
        update: UserPreferenceProfileUpdate,
    ):
        return await update_preference_profile(
            db=self.session,
            user_id=user_id,
            update=update,
        )

    async def search_catalog(
        self,
        request: BookSearchInput,
    ) -> BookSearchCacheResult:
        """Authoritative catalog lookup used by non-Chat API consumers."""

        if request.mode != "lookup":
            raise ValueError("catalog API lookup requires mode=lookup")
        effective_query = _apply_discovery_constraints(request.query, request)
        result, books = await self._discover(
            request=request,
            effective_query=effective_query,
        )
        books = _filter_lookup_candidates(books, request)
        books = books[: request.limit]
        status = result.status
        if not books and status == "ok":
            status = "empty_result"
        kept_ids = {str(book.id) for book in books}
        return replace(
            result,
            query=request.query,
            status=status,
            books=books,
            candidate_sources={
                book_id: payload
                for book_id, payload in result.candidate_sources.items()
                if book_id in kept_ids
            },
            metadata={
                **result.metadata,
                "owner": "RecommendationService",
                "effective_query": effective_query,
                "filters_applied": _filters_applied(request),
                "candidate_count_after_verified_filters": len(books),
            },
        )

    async def search(
        self,
        request: BookSearchInput,
        *,
        user_id: UUID | None,
    ) -> BookEvidence:
        effective_query = request.query
        constraints_payload: dict[str, Any] = {}
        if request.mode == "recommendation" and user_id is not None:
            constraints = await build_personalized_recommendation_constraints(
                self.session,
                user_id=user_id,
                query=request.query,
            )
            effective_query = constraints.effective_query or request.query
            constraints_payload = constraints.model_dump(mode="json")

        effective_query = _apply_discovery_constraints(effective_query, request)
        result, books = await self._discover(
            request=request,
            effective_query=effective_query,
        )
        books = _filter_lookup_candidates(books, request)
        books, request_exclusions = _exclude_request_titles(books, request)
        projection_payload: dict[str, Any] = {}
        if request.mode == "recommendation" and user_id is not None and books:
            projection = await RecommendationProjector(self.session).project_books(
                user_id=user_id,
                query=request.query,
                books=books,
                candidate_sources=result.candidate_sources,
            )
            projection_payload = projection.model_dump(mode="json")
            by_id = {str(book.id): book for book in books}
            books = [
                by_id[str(candidate.book_id)]
                for candidate in projection.candidates
                if candidate.book_id is not None
                and str(candidate.book_id) in by_id
            ]

        selected_books = books[: request.limit]
        coverage = _theme_coverage(
            selected_books,
            request=request,
            discovery_metadata=result.metadata,
        )
        items, enrichment_metadata = await _recommendation_items(
            selected_books,
            request=request,
            discovery_metadata=result.metadata,
            gateway=self._search_gateway,
            enable_external_enrichment=self._enable_external_enrichment,
        )
        sources = [
            source
            for book in selected_books
            if (source := _book_source(book)) is not None
        ]
        if sources:
            status = "ok"
            error = ""
        elif result.status == "empty_result" or (
            request.mode == "recommendation" and result.books
        ):
            status = "empty_result"
            error = ""
        else:
            status = "unavailable"
            error = "book_source_unavailable"
        filters_applied = _filters_applied(request)
        limitations = _limitations(
            request=request,
            candidate_count=len(sources),
        )
        return BookEvidence(
            status=status,
            query=request.query,
            sources=sources,
            error=error,
            candidate_count=len(sources),
            response_depth=request.response_depth,
            items=items,
            coverage=coverage,
            filters_applied=filters_applied,
            limitations=limitations,
            metadata={
                "owner": "RecommendationService",
                "mode": request.mode,
                "effective_query": effective_query,
                "search_status": result.status,
                "discovery": result.metadata,
                "candidate_count_before_verified_filters": len(result.books),
                "candidate_count_after_verified_filters": len(books),
                "filters_applied": filters_applied,
                "limitations": limitations,
                "enrichment": enrichment_metadata,
                "personalization": {
                    "enabled": (
                        request.mode == "recommendation" and user_id is not None
                    ),
                    "constraints": constraints_payload,
                    "projection": projection_payload,
                    "suppressed_books": projection_payload.get(
                        "suppressed_candidates",
                        [],
                    ),
                    "reference_titles": request.reference_titles,
                    "request_exclusions": request_exclusions,
                },
            },
        )

    async def _discover(
        self,
        *,
        request: BookSearchInput,
        effective_query: str,
    ) -> tuple[BookSearchCacheResult, list[Book]]:
        strict_year_filter = (
            request.publication_year_from is not None
            or request.publication_year_to is not None
        )
        candidate_limit = min(10, max(request.limit, request.limit * 2))
        discovery_queries = _discovery_queries(
            request=request,
            effective_query=effective_query,
        )
        per_query_limit = min(
            10,
            max(3, (candidate_limit + len(discovery_queries) - 1) // len(discovery_queries) + 2),
        )
        results = []
        for query in discovery_queries:
            results.append(
                await search_and_cache_books_with_status(
                    self.session,
                    query=query,
                    limit=(candidate_limit if len(discovery_queries) == 1 else per_query_limit),
                    force_external=(
                        strict_year_filter or request.mode == "recommendation"
                    ),
                    federated_discovery=(request.mode == "recommendation"),
                )
            )
        result = _merge_discovery_results(
            results,
            query=effective_query,
            limit=candidate_limit,
            discovery_queries=discovery_queries,
        )
        return result, _filter_verified_constraints(
            list(result.books),
            request,
            candidate_theme_by_id=dict(
                result.metadata.get("candidate_theme_by_id") or {}
            ),
        )


def _discovery_queries(
    *,
    request: BookSearchInput,
    effective_query: str,
) -> list[str]:
    """Compile one semantic request into bounded provider queries.

    `themes` is authored by the Controller's single semantic pass.  This
    function only applies the remaining typed constraints; it never parses the
    user's natural-language message.
    """

    if request.mode != "recommendation" or not request.themes:
        return [effective_query]
    queries = [
        " ".join(str(theme or "").split()).strip()
        for theme in request.themes
        if str(theme or "").strip()
    ]
    return list(dict.fromkeys(queries)) or [effective_query]


def _merge_discovery_results(
    results: list[BookSearchCacheResult],
    *,
    query: str,
    limit: int,
    discovery_queries: list[str],
) -> BookSearchCacheResult:
    """Round-robin merge keeps multi-theme recall balanced under one owner."""

    if len(results) == 1:
        result = results[0]
        return replace(
            result,
            query=query,
            metadata={
                **result.metadata,
                "discovery_queries": discovery_queries,
                "discovery_strategy": "single_query",
                "candidate_theme_by_id": {
                    _book_mapping_key(book): discovery_queries[0]
                    for book in result.books
                },
            },
        )

    merged: list[Book] = []
    seen: set[str] = set()
    max_depth = max((len(result.books) for result in results), default=0)
    for index in range(max_depth):
        for result in results:
            if index >= len(result.books):
                continue
            book = result.books[index]
            identity = normalize_book_work_title(str(book.title or ""))
            if not identity:
                identity = str(getattr(book, "id", "") or "")
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(book)
            if len(merged) >= limit:
                break
        if len(merged) >= limit:
            break

    candidate_sources: dict[str, dict] = {}
    candidate_theme_by_id: dict[str, str] = {}
    for query_index, result in enumerate(results):
        candidate_sources.update(result.candidate_sources)
        theme = discovery_queries[query_index]
        for book in result.books:
            candidate_theme_by_id.setdefault(_book_mapping_key(book), theme)
    statuses = [result.status for result in results]
    if merged:
        status = "ok"
    elif statuses and all(item == "empty_result" for item in statuses):
        status = "empty_result"
    else:
        status = next(
            (item for item in statuses if item != "empty_result"),
            "empty_result",
        )
    sources = list(dict.fromkeys(result.source for result in results if result.source))
    errors = list(dict.fromkeys(str(result.error) for result in results if result.error))
    return BookSearchCacheResult(
        query=query,
        status=status,
        books=merged,
        source="+".join(sources) or "external_search",
        next_action_hint=next(
            (result.next_action_hint for result in results if result.next_action_hint),
            "",
        ),
        error="; ".join(errors) or None,
        duration_ms=sum(result.duration_ms for result in results),
        metadata={
            "discovery_queries": discovery_queries,
            "discovery_strategy": "semantic_theme_fanout_round_robin",
            "query_results": [
                {
                    "query": result.query,
                    "status": result.status,
                    "result_count": len(result.books),
                    "source": result.source,
                }
                for result in results
            ],
            "merged_result_count": len(merged),
            "candidate_theme_by_id": candidate_theme_by_id,
        },
        candidate_sources=candidate_sources,
    )


def _theme_coverage(
    books: list[Book],
    *,
    request: BookSearchInput,
    discovery_metadata: dict[str, Any],
) -> list[BookThemeCoverage]:
    if request.mode != "recommendation" or not request.themes:
        return []
    mapping = {
        str(book_id): str(theme or "").strip()
        for book_id, theme in dict(
            discovery_metadata.get("candidate_theme_by_id") or {}
        ).items()
    }
    counts = {theme: 0 for theme in request.themes}
    for book in books:
        theme = mapping.get(_book_mapping_key(book), "")
        if theme in counts:
            counts[theme] += 1
    if request.response_depth == "quick":
        complete_at = 1
    else:
        complete_at = min(
            2,
            max(1, request.limit // max(1, len(request.themes))),
        )
    return [
        BookThemeCoverage(
            theme=theme,
            candidate_count=counts.get(theme, 0),
            status=(
                "complete"
                if counts.get(theme, 0) >= complete_at
                else "partial"
                if counts.get(theme, 0) > 0
                else "missing"
            ),
        )
        for theme in request.themes
    ]


async def _recommendation_items(
    books: list[Book],
    *,
    request: BookSearchInput,
    discovery_metadata: dict[str, Any],
    gateway,
    enable_external_enrichment: bool,
) -> tuple[list[BookRecommendationItem], list[dict[str, Any]]]:
    mapping = {
        str(book_id): str(theme or "").strip()
        for book_id, theme in dict(
            discovery_metadata.get("candidate_theme_by_id") or {}
        ).items()
    }
    if (
        request.mode == "lookup"
        or request.response_depth == "quick"
        or not enable_external_enrichment
    ):
        items = [
            _base_recommendation_item(
                book,
                theme=mapping.get(_book_mapping_key(book), ""),
            )
            for book in books
        ]
        return items, []

    gateway = gateway or get_search_gateway()
    provider_budget = 3 if request.response_depth == "deep" else 2
    semaphore = asyncio.Semaphore(3)

    async def enrich(book: Book):
        async with semaphore:
            return await _enrich_recommendation_book(
                book,
                theme=mapping.get(_book_mapping_key(book), ""),
                gateway=gateway,
                provider_budget=provider_budget,
            )

    results = await asyncio.gather(
        *(enrich(book) for book in books),
        return_exceptions=True,
    )
    items: list[BookRecommendationItem] = []
    metadata: list[dict[str, Any]] = []
    for book, raw in zip(books, results, strict=True):
        theme = mapping.get(_book_mapping_key(book), "")
        if isinstance(raw, Exception):
            items.append(_base_recommendation_item(book, theme=theme))
            metadata.append(
                {
                    "title": str(book.title or ""),
                    "status": "unavailable",
                    "providers": [],
                    "source_domains": [],
                }
            )
            continue
        item, item_metadata = raw
        items.append(item)
        metadata.append(item_metadata)
    return items, metadata


async def _enrich_recommendation_book(
    book: Book,
    *,
    theme: str,
    gateway,
    provider_budget: int,
) -> tuple[BookRecommendationItem, dict[str, Any]]:
    title = " ".join(str(book.title or "").split()).strip()
    authors = [str(item).strip() for item in (book.authors or []) if str(item).strip()]
    author_hint = authors[0] if authors else ""
    query = " ".join(
        item
        for item in [f"《{title}》", author_hint, "内容简介", "适合读者"]
        if item
    )[:300]
    result = await gateway.search(
        SearchRequest(
            query=query,
            max_results=5,
            detail="deep" if provider_budget >= 3 else "standard",
            language="zh",
            zone="cn",
            strategy="federated",
            provider_budget=provider_budget,
        )
    )
    base = _book_source(book)
    evidence_sources = [base] if base is not None else []
    seen_urls = {
        _canonical_public_url(source.url)
        for source in evidence_sources
        if _canonical_public_url(source.url)
    }
    summaries: list[str] = []
    base_summary = _clean_catalog_description(book.summary)
    if base_summary:
        summaries.append(base_summary)
    providers: list[str] = []
    domains: list[str] = []
    for hit in result.hits:
        if not _hit_matches_book(hit.title, hit.snippet or hit.content, title):
            continue
        url = str(hit.url or "").strip()
        canonical = _canonical_public_url(url)
        if not canonical or canonical in seen_urls:
            continue
        seen_urls.add(canonical)
        snippet = _clean_catalog_description(hit.snippet or hit.content)
        evidence_sources.append(
            ExternalEvidenceSource(
                title=str(hit.title or title).strip()[:300],
                url=url[:2000],
                snippet=snippet[:1000],
                published_date=str(hit.published_date or "")[:64],
            )
        )
        if snippet and snippet not in summaries:
            summaries.append(snippet)
        provider = str(hit.provider or "").strip()
        if provider and provider not in providers:
            providers.append(provider)
        domain = urlsplit(url).netloc.casefold()
        if domain and domain not in domains:
            domains.append(domain)
        if len(evidence_sources) >= 5:
            break
    item = _base_recommendation_item(book, theme=theme).model_copy(
        update={
            "summary": _merge_admitted_summaries(summaries),
            "evidence_sources": evidence_sources,
            "evidence_provider_count": len(providers),
        }
    )
    return item, {
        "title": title,
        "status": result.outcome,
        "providers": providers,
        "source_domains": domains,
        "source_count": len(evidence_sources),
        "search_strategy": result.metadata.get("search_strategy", ""),
    }


def _base_recommendation_item(
    book: Book,
    *,
    theme: str,
) -> BookRecommendationItem:
    raw = book.raw_data if isinstance(book.raw_data, dict) else {}
    source = _book_source(book)
    return BookRecommendationItem(
        title=str(book.title or "").strip(),
        authors=[str(item).strip() for item in (book.authors or []) if str(item).strip()],
        theme=theme,
        summary=_clean_catalog_description(book.summary),
        catalog_url=(source.url if source is not None else ""),
        cover_url=str(book.cover_url or raw.get("cover_source_url") or "").strip(),
        published_date=str(
            raw.get("book_published_date")
            or raw.get("published_date")
            or raw.get("source_published_date")
            or ""
        )[:64],
        evidence_sources=[source] if source is not None else [],
        evidence_provider_count=0,
    )


def _hit_matches_book(title: str, snippet: str, book_title: str) -> bool:
    wanted = _text_identity(book_title)
    if not wanted:
        return False
    haystack = _text_identity(f"{title} {snippet}")
    return wanted in haystack


def _text_identity(value: object) -> str:
    return re.sub(
        r"[^\w\u3400-\u9fff]+",
        "",
        str(value or "").casefold(),
    )


def _canonical_public_url(value: object) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/") or "/",
            "",
            "",
        )
    )


def _book_mapping_key(book: Book) -> str:
    book_id = getattr(book, "id", None)
    if book_id is not None:
        return f"id:{book_id}"
    return f"title:{normalize_book_work_title(str(book.title or ''))}"


def _clean_catalog_description(value: object) -> str:
    """Remove provider-page chrome while preserving an admitted description.

    Catalog providers sometimes return an entire subject page as the snippet.
    This is source-format normalization only: it does not infer user intent or
    invent book facts.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""

    candidates: list[str] = []
    for match in re.finditer(
        r"\[\s*\.{3}\s*\]\s*(.+?)(?=\n\s*##\s|\Z)",
        raw,
        flags=re.DOTALL,
    ):
        cleaned = " ".join(match.group(1).split()).strip(" ·#;；")
        if len(cleaned) >= 20:
            candidates.append(cleaned)

    for match in re.finditer(
        r"(?:^|\n)\s*#{0,2}\s*(?:内容简介|作品简介|图书简介)\s*[:：]?\s*"
        r"(.+?)(?=\n\s*##\s|\Z)",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        cleaned = " ".join(match.group(1).split()).strip(" ·#;；")
        if len(cleaned) >= 20:
            candidates.append(cleaned)

    if candidates:
        def score(text: str) -> int:
            navigation_hits = len(
                re.findall(
                    r"购买|商城|图书馆|书单|第一章|第二章|目录|原文摘录",
                    text,
                )
            )
            return min(len(text), 700) - navigation_hits * 250

        return max(candidates, key=score)[:700]

    compact = " ".join(raw.split()).strip(" ·#;；")
    if re.search(r"我要写书评|(?:^|\s)短评(?:\s|$)|写书评", compact):
        return ""
    chapter_hits = len(
        re.findall(r"第[一二三四五六七八九十百\d]+章|/\s*\d{3}(?:\s|$)", compact)
    )
    if chapter_hits >= 2:
        return ""
    navigation_hits = len(
        re.findall(r"购买|商城|图书馆|书单|人在读|人读过|人想读", compact)
    )
    return "" if navigation_hits >= 2 else compact[:700]


def _merge_admitted_summaries(values: list[str]) -> str:
    """Compact duplicate provider snippets without adding new meaning."""

    selected: list[str] = []
    identities: list[str] = []
    for value in values:
        cleaned = _clean_catalog_description(value)
        identity = _text_identity(cleaned)
        if not cleaned or not identity:
            continue
        if any(identity in existing or existing in identity for existing in identities):
            continue
        selected.append(cleaned)
        identities.append(identity)
        if len(selected) >= 3:
            break
    return " ".join(selected)[:1200]


def _book_source(book: Book) -> ExternalEvidenceSource | None:
    raw = book.raw_data if isinstance(book.raw_data, dict) else {}
    url = str(
        book.source_url
        or raw.get("url")
        or raw.get("search_url")
        or ""
    ).strip()
    if not url:
        return None
    authors = "、".join(str(item) for item in (book.authors or []) if item)
    summary = _clean_catalog_description(book.summary)
    parts = []
    if authors:
        parts.append(f"作者：{authors}")
    if book.rating is not None:
        parts.append(f"评分：{book.rating}")
    if summary:
        parts.append(summary)
    return ExternalEvidenceSource(
        title=str(book.title or "").strip()[:300],
        url=url[:2000],
        snippet="；".join(parts)[:1000],
        published_date=str(
            raw.get("source_published_date")
            or raw.get("published_date")
            or ""
        )[:64],
    )


def _apply_discovery_constraints(query: str, request: BookSearchInput) -> str:
    """Translate already-structured intent into provider search constraints.

    This is deterministic execution of Controller-owned fields, not a second
    semantic interpretation of the user's natural language.
    """

    additions: list[str] = []
    additions.extend(request.authors)
    additions.extend(request.genres)
    if request.audience:
        additions.append(request.audience)
    if request.language:
        additions.append(request.language)
    if request.publication_year_from is not None:
        additions.append(str(request.publication_year_from))
    if (
        request.publication_year_to is not None
        and request.publication_year_to != request.publication_year_from
    ):
        additions.append(str(request.publication_year_to))
    values = [query.strip()]
    query_folded = query.casefold()
    for value in additions:
        cleaned = " ".join(str(value or "").split()).strip()
        if cleaned and cleaned.casefold() not in query_folded:
            values.append(cleaned)
    return " ".join(values)[:300]


def _filter_lookup_candidates(
    books: list[Book],
    request: BookSearchInput,
) -> list[Book]:
    if request.mode != "lookup":
        return books
    wanted = normalize_book_work_title(request.query)
    exact = [
        book
        for book in books
        if normalize_book_work_title(book.title) == wanted
    ]
    return exact[:1] if exact else books


def _exclude_request_titles(
    books: list[Book],
    request: BookSearchInput,
) -> tuple[list[Book], list[str]]:
    """Execute Controller-owned recommendation exclusions at the owner boundary.

    No natural-language interpretation occurs here.  The service compares only
    structured titles already emitted by the one semantic planning pass.
    """

    if request.mode != "recommendation" or not request.excluded_titles:
        return books, []
    excluded = {
        normalized
        for title in request.excluded_titles
        if (normalized := normalize_book_work_title(title))
    }
    kept: list[Book] = []
    removed: list[str] = []
    for book in books:
        if normalize_book_work_title(book.title) in excluded:
            removed.append(str(book.title or "").strip())
            continue
        kept.append(book)
    return kept, removed


def _filter_verified_constraints(
    books: list[Book],
    request: BookSearchInput,
    *,
    candidate_theme_by_id: dict[str, str] | None = None,
) -> list[Book]:
    filtered: list[Book] = []
    for book in books:
        if (
            request.publication_year_from is not None
            or request.publication_year_to is not None
        ):
            year = _publication_year(book)
            if year is None:
                continue
            if (
                request.publication_year_from is not None
                and year < request.publication_year_from
            ):
                continue
            if (
                request.publication_year_to is not None
                and year > request.publication_year_to
            ):
                continue
        theme = str(
            (candidate_theme_by_id or {}).get(_book_mapping_key(book), "")
        )
        if (
            request.mode == "recommendation"
            and request.themes
            and theme
            and not _matches_structured_theme(book, theme=theme)
        ):
            continue
        if _has_explicit_audience_conflict(
            book,
            structured_target=" ".join(
                value
                for value in [theme, request.audience, request.query]
                if value
            ),
        ):
            continue
        filtered.append(book)
    return filtered


def _matches_structured_theme(book: Book, *, theme: str) -> bool:
    """Validate lexical relevance to Controller-owned structured themes."""

    candidate = " ".join(
        [
            str(book.title or ""),
            str(book.summary or ""),
            " ".join(str(item or "") for item in (book.tags or [])),
        ]
    ).casefold()
    tokens = [
        item
        for item in re.split(r"[\s,，、;；/|]+", str(theme or "").casefold())
        if item
    ]
    variants: list[str] = []
    for token in tokens:
        for marker in (
            "成年人",
            "小学生",
            "青少年",
            "成人",
            "职场",
            "幼儿",
            "少儿",
        ):
            if token.startswith(marker) and len(token) - len(marker) >= 2:
                token = token[len(marker):]
                break
        if len(token) < 2:
            continue
        variants.append(token)
        if re.fullmatch(r"[\u3400-\u9fff]{3,}", token):
            variants.extend([token[:2], token[-2:]])
    return not variants or any(value in candidate for value in variants)


def _has_explicit_audience_conflict(
    book: Book,
    *,
    structured_target: str,
) -> bool:
    """Reject only explicit target/package conflicts in structured metadata."""

    target = str(structured_target or "").casefold()
    target_is_adult = any(
        marker in target for marker in ("成年人", "成人", "职场", "adult")
    )
    target_is_child = any(
        marker in target
        for marker in ("小学生", "幼儿", "少儿", "青少年", "child", "teen")
    )
    raw = book.raw_data if isinstance(book.raw_data, dict) else {}
    candidate = " ".join(
        [
            str(book.title or ""),
            " ".join(str(item or "") for item in (book.tags or [])),
            str(raw.get("audience") or ""),
            str(raw.get("subtitle") or ""),
        ]
    ).casefold()
    child_packaging = any(
        marker in candidate
        for marker in (
            "小学生",
            "幼儿",
            "儿童",
            "少儿读物",
            "儿童绘本",
            "青少年读物",
            "适读年龄",
            "for children",
            "for kids",
        )
    )
    adult_packaging = any(
        marker in candidate
        for marker in ("成人读物", "成年人", "职场人士", "for adults")
    )
    # A strongly age-packaged children's edition is not a safe generic
    # recommendation candidate.  The semantic layer must explicitly opt into
    # that audience; otherwise a catalog fallback can silently turn an adult or
    # audience-neutral request into a children's list.  This is validation of
    # structured candidate metadata, not a second interpretation of user text.
    if child_packaging and not adult_packaging and not target_is_child:
        return True
    return target_is_child and adult_packaging and not child_packaging


def _publication_year(book: Book) -> int | None:
    raw = book.raw_data if isinstance(book.raw_data, dict) else {}
    candidates = [
        raw.get("publication_year"),
        raw.get("pubdate"),
    ]
    for value in candidates:
        match = re.search(r"(?<!\d)((?:19|20|21)\d{2})(?!\d)", str(value or ""))
        if match:
            return int(match.group(1))
    snippet = str(raw.get("search_snippet") or "")
    contextual = re.search(
        r"(?:出版年|出版时间|出版日期|出版于|published)\s*[:：]?\s*"
        r"((?:19|20|21)\d{2})(?!\d)",
        snippet,
        flags=re.IGNORECASE,
    )
    if contextual:
        return int(contextual.group(1))
    return None


def _filters_applied(request: BookSearchInput) -> list[str]:
    filters: list[str] = []
    if request.language:
        filters.append("language_discovery")
    if request.genres:
        filters.append("genre_discovery")
    if request.authors:
        filters.append("author_discovery")
    if request.audience:
        filters.append("audience_discovery")
    if request.publication_year_from is not None:
        filters.append("publication_year_from_verified")
    if request.publication_year_to is not None:
        filters.append("publication_year_to_verified")
    if request.reference_titles:
        filters.append("reference_titles_excluded")
    if request.excluded_titles:
        filters.append("explicit_titles_excluded")
    return filters


def _limitations(
    *,
    request: BookSearchInput,
    candidate_count: int,
) -> list[str]:
    limitations: list[str] = []
    if request.language or request.genres or request.authors or request.audience:
        limitations.append(
            "语言、类型、作者与受众用于目录发现和排序；来源元数据不足时不冒充严格核验。"
        )
    if (
        request.publication_year_from is not None
        or request.publication_year_to is not None
    ) and candidate_count == 0:
        limitations.append("没有找到出版年份可核验且落在指定区间内的候选书。")
    return limitations


__all__ = ["RecommendationService"]
