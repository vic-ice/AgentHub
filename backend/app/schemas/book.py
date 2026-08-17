from datetime import datetime
from decimal import Decimal
from uuid import UUID

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.book_search_contracts import BookSearchStatus
from app.services.recommendation_signals import RecommendationSignal


class BookBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    rating: Decimal | None = None
    rating_count: int | None = None
    cover_url: str | None = None
    source_name: str = "web"
    source_url: str | None = None
    external_id: str | None = None
    raw_data: dict = Field(default_factory=dict)


class BookInDB(BookBase):
    id: UUID
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BookSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=256)
    limit: int = Field(default=5, ge=1, le=10)
    persist: bool = True


class BookSearchResponse(BaseModel):
    query: str
    status: BookSearchStatus = "ok"
    books: list[BookInDB]
    result_count: int = 0
    source: str = "external_search"
    next_action_hint: str = ""
    error: str | None = None
    duration_ms: int = 0
    metadata: dict = Field(default_factory=dict)
    candidate_sources: dict = Field(default_factory=dict)


class BookInteractionCreate(BaseModel):
    user_id: UUID
    book_id: UUID | None = None
    book_title: str | None = Field(default=None, max_length=256)
    interaction_type: str = Field(..., min_length=1, max_length=32)
    note: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    raw_data: dict = Field(default_factory=dict)

    @field_validator("interaction_type")
    @classmethod
    def normalize_interaction_type(cls, value: str) -> str:
        return value.strip().lower().replace(" ", "_")


class BookInteractionInDB(BookInteractionCreate):
    id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RecommendationSignalInDB(RecommendationSignal):
    model_config = ConfigDict(from_attributes=True)


class UserPreferenceProfileBase(BaseModel):
    preferred_tags: list[str] = Field(default_factory=list)
    disliked_tags: list[str] = Field(default_factory=list)
    favorite_authors: list[str] = Field(default_factory=list)
    disliked_authors: list[str] = Field(default_factory=list)
    notes: str = ""
    profile_summary: str = ""


class UserPreferenceProfileUpdate(BaseModel):
    preferred_tags: list[str] | None = None
    disliked_tags: list[str] | None = None
    favorite_authors: list[str] | None = None
    disliked_authors: list[str] | None = None
    notes: str | None = None
    profile_summary: str | None = None
    merge: bool = True


class UserPreferenceProfileInDB(UserPreferenceProfileBase):
    user_id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ── Bookshelf (Reading Assets) — frozen v1 ─────────────────────────────────────
# 契约权威来源：docs/bookshelf-contract.md

ReadingStatus = Literal["want_to_read", "reading", "read", "dropped"]
BookEvaluation = Literal["liked", "neutral", "disliked", "not_interested"]


class ShelfBookBase(BaseModel):
    id: UUID
    user_id: UUID
    book_id: UUID | None = None
    title: str = Field(..., min_length=1, max_length=256)
    authors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    cover_url: str | None = None
    source_url: str | None = None
    reading_status: ReadingStatus
    evaluation: BookEvaluation | None = None
    note: str = ""
    rating: int | None = Field(default=None, ge=1, le=5)
    last_event_at: datetime | None = None


class ShelfBook(ShelfBookBase):
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ShelfBookUpsert(BaseModel):
    user_id: UUID
    book_id: UUID | None = None
    title: str = Field(default="", max_length=256)
    reading_status: ReadingStatus = "want_to_read"
    evaluation: BookEvaluation | None = None
    note: str = Field(default="", max_length=2000)
    rating: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def require_book_id_or_title(self) -> "ShelfBookUpsert":
        if self.book_id is None and not str(self.title or "").strip():
            raise ValueError("book_id or title is required")
        return self


class ShelfBookUpdate(BaseModel):
    reading_status: ReadingStatus | None = None
    evaluation: BookEvaluation | None = None
    note: str | None = Field(default=None, max_length=2000)
    rating: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def at_least_one_field(self) -> "ShelfBookUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one shelf field must be provided")
        return self


class BookshelfListResponse(BaseModel):
    user_id: UUID
    items: list[ShelfBook] = Field(default_factory=list)
    total: int = 0
    status: Literal["ok"] = "ok"