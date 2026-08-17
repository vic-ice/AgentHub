from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.book import BookEvaluation, ReadingStatus, ShelfBook


class BookshelfReadRequest(BaseModel):
    """Typed current-Shelf query proposed by the Controller."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["current"] = Field(
        default="current",
        description="Read the authoritative current Bookshelf state.",
    )
    statuses: list[ReadingStatus] = Field(
        default_factory=list,
        description=(
            "Canonical reading-status subset requested by the user. Include "
            "every explicitly requested status; use an empty list only when "
            "the user asks for the whole Shelf without a status restriction."
        ),
    )
    evaluations: list[BookEvaluation] = Field(
        default_factory=list,
        description=(
            "Canonical evaluation subset requested by the user. Include every "
            "explicitly requested evaluation; use an empty list only when no "
            "evaluation restriction was requested."
        ),
    )
    query: str = Field(
        default="",
        max_length=256,
        description="Optional title or author filter; empty returns the whole Shelf.",
    )
    limit: int = Field(default=5000, ge=1, le=5000)


class BookshelfReadReceipt(BaseModel):
    """One aggregate row per authoritative Shelf entry."""

    model_config = ConfigDict(extra="forbid")

    result_mode: Literal["bookshelf_read_receipt"] = "bookshelf_read_receipt"
    status: Literal["completed", "empty"]
    user_id: UUID
    total: int = Field(ge=0)
    items: list[ShelfBook] = Field(default_factory=list)


__all__ = ["BookshelfReadReceipt", "BookshelfReadRequest"]
