from __future__ import annotations

from typing import Any, Protocol

from app.infra.database import get_database
from app.services.books.recommendation_service import RecommendationService
from app.services.agent_runtime.contracts import (
    ActionReceipt,
    ExecutionContext,
)
from app.services.external_capabilities.contracts import BookEvidence, BookSearchInput

class RecommendationGateway(Protocol):
    async def search(
        self, request: BookSearchInput, *, user_id
    ) -> BookEvidence:
        ...


class BookSearchRuntimeAdapter:
    """Production adapter into the single ordinary recommendation owner."""

    operation = "book_search_v1"

    def __init__(
        self,
        *,
        recommendation_service: RecommendationGateway | None = None,
    ) -> None:
        self._recommendation_service = recommendation_service

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ExecutionContext,
        previous: list[ActionReceipt],
    ) -> dict[str, Any]:
        del previous
        request = BookSearchInput.model_validate(arguments)
        if self._recommendation_service is not None:
            result = await self._recommendation_service.search(
                request,
                user_id=context.user_id,
            )
            return result.model_dump(mode="json")
        database = get_database()
        async with database.session() as session:
            result = await RecommendationService(
                session,
                enable_external_enrichment=True,
            ).search(
                request, user_id=context.user_id
            )
            return result.model_dump(mode="json")


__all__ = ["BookSearchRuntimeAdapter", "RecommendationGateway"]
