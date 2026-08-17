"""Authoritative ordinary book lookup and recommendation owner."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.book import Book
from app.services.book_search import search_and_cache_books_with_status
from app.services.external_capabilities.contracts import (
    BookEvidence,
    BookSearchInput,
    ExternalEvidenceSource,
)
from app.services.recommendation_constraints import (
    build_personalized_recommendation_constraints,
)
from app.services.recommendation_projection import RecommendationProjector


class RecommendationService:
    """Own discovery policy; Search only supplies public candidate evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        request: BookSearchInput,
        *,
        user_id: UUID,
    ) -> BookEvidence:
        effective_query = request.query
        constraints_payload: dict[str, Any] = {}
        if request.mode == "recommendation":
            constraints = await build_personalized_recommendation_constraints(
                self.session,
                user_id=user_id,
                query=request.query,
            )
            effective_query = constraints.effective_query or request.query
            constraints_payload = constraints.model_dump(mode="json")

        result = await search_and_cache_books_with_status(
            self.session,
            query=effective_query,
            limit=request.limit,
        )
        books = list(result.books)
        projection_payload: dict[str, Any] = {}
        if request.mode == "recommendation" and books:
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

        sources = [_book_source(book) for book in books[: request.limit]]
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
        return BookEvidence(
            status=status,
            query=request.query,
            sources=sources,
            error=error,
            metadata={
                "owner": "RecommendationService",
                "mode": request.mode,
                "effective_query": effective_query,
                "search_status": result.status,
                "personalization": {
                    "enabled": request.mode == "recommendation",
                    "constraints": constraints_payload,
                    "projection": projection_payload,
                    "suppressed_books": projection_payload.get(
                        "suppressed_candidates",
                        [],
                    ),
                },
            },
        )


def _book_source(book: Book) -> ExternalEvidenceSource:
    raw = book.raw_data if isinstance(book.raw_data, dict) else {}
    url = str(book.source_url or raw.get("url") or "").strip()
    authors = "、".join(str(item) for item in (book.authors or []) if item)
    summary = " ".join(str(book.summary or "").split())
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
        published_date=str(raw.get("published_date") or "")[:64],
    )


__all__ = ["RecommendationService"]
