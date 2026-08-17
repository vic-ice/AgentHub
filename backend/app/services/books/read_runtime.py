from __future__ import annotations

from typing import Any

from app.infra.database import get_database
from app.services.agent_runtime.contracts import ExecutionContext
from app.services.books.read_contracts import (
    BookshelfReadReceipt,
    BookshelfReadRequest,
)
from app.services.books.reading_service import ReadingService


async def execute_bookshelf_read(
    arguments: dict[str, Any],
    *,
    context: ExecutionContext,
) -> dict[str, Any]:
    """Read the authoritative current Shelf through its sole domain Owner."""

    request = BookshelfReadRequest.model_validate(arguments)
    database = get_database()
    async with database.session() as session:
        service = ReadingService(session)
        await service.ensure_backfilled(context.user_id)
        items, total = await service.list_entries(
            user_id=context.user_id,
            statuses=list(request.statuses),
            evaluations=list(request.evaluations),
            q=request.query,
            limit=request.limit,
            offset=0,
        )
    return BookshelfReadReceipt(
        status="completed" if items else "empty",
        user_id=context.user_id,
        total=total,
        items=items,
    ).model_dump(mode="json")


__all__ = ["execute_bookshelf_read"]
