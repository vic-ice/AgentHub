"""Book and reading preference endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_db
from app.schemas.book import (
    BookInDB,
    BookInteractionCreate,
    BookInteractionInDB,
    BookSearchResponse,
    UserPreferenceProfileInDB,
    UserPreferenceProfileUpdate,
)
from app.services.books.recommendation_service import RecommendationService
from app.services.external_capabilities.contracts import BookSearchInput
from app.services.recommendation_signals import (
    RecommendationSignal,
    RecommendationSignalCreate,
    recommendation_signal_from_record,
)

from app.infra.database import get_database
from app.schemas.book import (
    BookshelfListResponse,
    ShelfBook,
    ShelfBookUpdate,
    ShelfBookUpsert,
)
from app.services.books.reading_service import (
    ReadingService,
    write_reading_memory_best_effort,
)
from app.services.books.book_metadata import cached_cover_path


api_router = APIRouter(prefix="/books", tags=["Books"])


@api_router.get("", response_model=list[BookInDB])
async def get_books(
    query: str | None = Query(default=None, description="Search local cached books"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[BookInDB]:
    books = await RecommendationService(db).list_catalog(
        query=query,
        limit=limit,
        offset=offset,
    )
    return [BookInDB.model_validate(book) for book in books]


@api_router.get("/search", response_model=BookSearchResponse)
async def search_books(
    query: str = Query(..., min_length=1, max_length=256),
    limit: int = Query(default=5, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
) -> BookSearchResponse:
    result = await RecommendationService(db).search_catalog(
        BookSearchInput(query=query, mode="lookup", limit=limit)
    )
    return BookSearchResponse(
        query=query,
        status=result.status,
        books=[BookInDB.model_validate(book) for book in result.books],
        result_count=result.result_count,
        source=result.source,
        next_action_hint=result.next_action_hint,
        error=result.error,
        duration_ms=result.duration_ms,
        metadata=result.metadata,
        candidate_sources=result.candidate_sources,
    )


@api_router.post("/interactions", response_model=BookInteractionInDB)
async def save_book_interaction(
    interaction: BookInteractionCreate,
    db: AsyncSession = Depends(get_db),
) -> BookInteractionInDB:
    saved = await RecommendationService(db).record_interaction(interaction)
    return BookInteractionInDB.model_validate(saved)


@api_router.post("/recommendation-events", response_model=RecommendationSignal)
async def save_recommendation_event(
    signal: RecommendationSignalCreate,
    db: AsyncSession = Depends(get_db),
) -> RecommendationSignal:
    saved = await RecommendationService(db).record_signal(signal)
    return recommendation_signal_from_record(saved)


@api_router.get("/recommendation-events/{user_id}", response_model=list[RecommendationSignal])
async def get_recommendation_events(
    user_id: UUID,
    book_title: str = Query(default=""),
    event_type: list[str] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[RecommendationSignal]:
    events = await RecommendationService(db).list_signals(
        user_id=user_id,
        book_title=book_title,
        event_types=event_type,
        limit=limit,
        offset=offset,
    )
    return [recommendation_signal_from_record(event) for event in events]


@api_router.get("/preferences/{user_id}", response_model=UserPreferenceProfileInDB)
async def get_reading_preferences(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> UserPreferenceProfileInDB:
    profile = await RecommendationService(db).get_preference_profile(user_id)
    return UserPreferenceProfileInDB.model_validate(profile)


@api_router.patch("/preferences/{user_id}", response_model=UserPreferenceProfileInDB)
async def update_reading_preferences(
    user_id: UUID,
    update: UserPreferenceProfileUpdate,
    db: AsyncSession = Depends(get_db),
) -> UserPreferenceProfileInDB:
    profile = await RecommendationService(db).update_preference_profile(
        user_id,
        update,
    )
    return UserPreferenceProfileInDB.model_validate(profile)



def _split_query_values(raw: str | None) -> list[str]:
    """Split comma-separated query values into a cleaned list."""
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@api_router.get("/shelf/{user_id}", response_model=BookshelfListResponse)
async def get_bookshelf(
    user_id: UUID,
    status: str | None = Query(default=None, description="Comma-separated ReadingStatus values"),
    evaluation: str | None = Query(default=None, description="BookEvaluation value"),
    q: str | None = Query(default=None, max_length=256, description="Title/author fuzzy match"),
    limit: int = Query(default=5000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
) -> BookshelfListResponse:
    db = get_database()
    async with db.session() as session:
        service = ReadingService(session)
        await service.ensure_backfilled(user_id)
        await service.enrich_missing_metadata(user_id)
        try:
            items, total = await service.list_entries(
                user_id=user_id,
                statuses=_split_query_values(status),
                evaluations=_split_query_values(evaluation),
                q=q or "",
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BookshelfListResponse(user_id=user_id, items=items, total=total)


@api_router.get("/covers/{filename}", response_class=FileResponse)
async def read_cached_book_cover(filename: str) -> FileResponse:
    path = cached_cover_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="book cover not found")
    return FileResponse(
        path,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@api_router.post("/shelf", response_model=ShelfBook)
async def upsert_bookshelf(payload: ShelfBookUpsert) -> ShelfBook:
    db = get_database()
    async with db.session() as session:
        service = ReadingService(session)
        await service.ensure_backfilled(payload.user_id)
        result = await service.upsert(
            user_id=payload.user_id,
            book_id=payload.book_id,
            title=payload.title,
            reading_status=payload.reading_status,
            evaluation=payload.evaluation,
            note=payload.note,
            rating=payload.rating,
            source="api",
        )
    await write_reading_memory_best_effort(
        user_id=payload.user_id,
        book_title=result.shelf.title,
        reading_status=result.shelf.reading_status,
        evaluation=result.shelf.evaluation,
        note=result.shelf.note,
        source_kind="bookshelf_api",
        source_event_id=(
            result.events[-1].id
            if result.events else result.interaction_id
        ),
    )
    return result.shelf


@api_router.patch("/shelf/{entry_id}", response_model=ShelfBook)
async def update_bookshelf_entry(entry_id: UUID, update: ShelfBookUpdate) -> ShelfBook:
    db = get_database()
    async with db.session() as session:
        service = ReadingService(session)
        try:
            result = await service.update_entry(entry_id, update, source="api")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="shelf entry not found") from exc
    await write_reading_memory_best_effort(
        user_id=result.shelf.user_id,
        book_title=result.shelf.title,
        reading_status=result.shelf.reading_status,
        evaluation=result.shelf.evaluation,
        note=result.shelf.note,
        source_kind="bookshelf_api",
        source_event_id=(
            result.events[-1].id
            if result.events else result.interaction_id
        ),
    )
    return result.shelf


@api_router.delete("/shelf/{entry_id}", status_code=204)
async def remove_bookshelf_entry(entry_id: UUID) -> None:
    db = get_database()
    async with db.session() as session:
        service = ReadingService(session)
        removed = await service.remove_entry(entry_id)
    if not removed:
        raise HTTPException(status_code=404, detail="shelf entry not found")
    return None

@api_router.get("/{book_id}", response_model=BookInDB | None)
async def read_book(
    book_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> BookInDB | None:
    book = await RecommendationService(db).get_catalog_book(book_id)
    return BookInDB.model_validate(book) if book else None
