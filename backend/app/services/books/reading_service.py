"""ReadingService — the single business write entry for reading state.

Contract: docs/bookshelf-contract.md (frozen v1)

Responsibilities:
- Current Shelf (user_book_shelf) is the authoritative current reading state.
- Every business write (chat feedback, recommendation buttons, Shelf API) goes
  through this service: Shelf row + RecommendationEvent audit (+ legacy
  BookInteraction compat row) are flushed in the same DB transaction.
- Reading status/evaluation memory is normally derived after an upsert. Shelf
  removal retires those derived current memories in the same transaction so
  the two authoritative user views cannot contradict each other.
- All mappings (reading status <-> event type <-> memory polarity <->
  legacy interaction type) live here and nowhere else.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import String, cast, func, or_, select, text
# (JSONB imported from sqlalchemy.dialects.postgresql when needed)
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.book import (
    create_book_interaction,
    create_recommendation_event,
    find_book_by_title,
)
from app.models.base import utc_now
from app.models.book import (
    Book,
    BookInteraction,
    RecommendationEvent,
    UserBookShelf,
    UserBookshelfState,
)
from app.schemas.book import (
    BookInteractionCreate,
    ShelfBook,
    ShelfBookUpdate,
)
from app.services.recommendation_signals import (
    RecommendationSignal,
    RecommendationSignalCreate,
    normalize_recommendation_text,
    normalize_recommendation_token,
    recommendation_signal_from_record,
)
from app.services.books.book_metadata import BookMetadata, resolve_book_metadata
from app.services.books.book_identity import (
    normalize_book_title,
    normalize_book_work_title,
)


logger = logging.getLogger(__name__)

READING_STATUSES = frozenset({"want_to_read", "reading", "read", "dropped"})
BOOK_EVALUATIONS = frozenset({"liked", "neutral", "disliked", "not_interested"})
STATUS_OR_EVALUATION_EVENT_TYPES = frozenset(
    {"want_to_read", "reading", "read", "dropped", "liked", "disliked", "not_interested"}
)

# reading status -> recommendation event type
STATUS_EVENT_TYPES = {
    "want_to_read": "want_to_read",
    "reading": "reading",
    "read": "read",
    "dropped": "dropped",
}
# evaluation -> recommendation event type
EVALUATION_EVENT_TYPES = {
    "liked": "liked",
    "disliked": "disliked",
    "not_interested": "not_interested",
}
# legacy book_interactions.interaction_type for status / evaluation
STATUS_INTERACTION_TYPES = {
    "want_to_read": "want_to_read",
    "reading": "reading",
    "read": "read",
    "dropped": "dropped",
}
EVALUATION_INTERACTION_TYPES = {
    "neutral": "neutral",
    "liked": "like",
    "disliked": "dislike",
    "not_interested": "not_interested",
}
# memory polarities that the existing memory schema understands (derived write).
STATUS_MEMORY_POLARITY = {"want_to_read": "want", "read": "read"}
EVALUATION_MEMORY_POLARITY = {
    "liked": "like",
    "disliked": "dislike",
    "not_interested": "avoid",
}
# event type -> (reading_status | None, evaluation | None)
EVENT_TO_STATE_CHANGE: dict[str, tuple[str | None, str | None]] = {
    "want_to_read": ("want_to_read", None),
    "reading": ("reading", None),
    "read": ("read", None),
    "dropped": ("dropped", None),
    "liked": (None, "liked"),
    "disliked": (None, "disliked"),
    "not_interested": (None, "not_interested"),
}
# legacy interaction_type -> (reading_status | None, evaluation | None)
INTERACTION_TO_STATE_CHANGE: dict[str, tuple[str | None, str | None]] = {
    "want_to_read": ("want_to_read", None),
    "want": ("want_to_read", None),
    "to_read": ("want_to_read", None),
    "reading": ("reading", None),
    "in_progress": ("reading", None),
    "currently_reading": ("reading", None),
    "read": ("read", None),
    "finished": ("read", None),
    "already_read": ("read", None),
    "dropped": ("dropped", None),
    "quit": ("dropped", None),
    "abandoned": ("dropped", None),
    "like": (None, "liked"),
    "liked": (None, "liked"),
    "dislike": (None, "disliked"),
    "disliked": (None, "disliked"),
    "not_interested": (None, "not_interested"),
    "avoid": (None, "not_interested"),
}

_UNSET = object()


@dataclass
class ReadingServiceResult:
    """Result of one ReadingService write: current shelf + audit artifacts."""

    shelf: ShelfBook
    interaction_id: UUID | None = None
    events: list[RecommendationSignal] = field(default_factory=list)


def _validate_status(value: Any) -> str:
    token = normalize_recommendation_token(value)
    if token not in READING_STATUSES:
        raise ValueError(f"reading_status must be one of: {sorted(READING_STATUSES)}")
    return token


def _validate_evaluation(value: Any) -> str | None:
    if value is None:
        return None
    token = normalize_recommendation_token(value)
    if token not in BOOK_EVALUATIONS:
        raise ValueError(f"evaluation must be one of: {sorted(BOOK_EVALUATIONS)}")
    return token


def _clean_note(value: Any) -> str:
    return str(value or "").strip()[:2000]
def _is_application_cover(value: str | None) -> bool:
    return str(value or "").startswith("/api/v1/books/covers/")


def _metadata_lookup_due(book: Book | None) -> bool:
    if book is None:
        return True
    if book.authors and _is_application_cover(book.cover_url):
        return False
    state = dict(book.raw_data or {}).get("douban_metadata") or {}
    if int(state.get("resolver_version") or 0) < 2:
        return True
    attempted_at = str(state.get("attempted_at") or "")
    if not attempted_at:
        return True
    try:
        age = utc_now() - datetime.fromisoformat(attempted_at)
    except ValueError:
        return True
    return age.total_seconds() >= 6 * 60 * 60

class ReadingService:
    """Owns the user_book_shelf table: reads, writes, backfill, mapping."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── Backfill ────────────────────────────────────────────────────────────

    async def ensure_backfilled(self, user_id: UUID) -> bool:
        """Backfill the shelf from historical audit once per user (idempotent)."""
        await self.session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:lock_key, 0))"
            ),
            {"lock_key": f"bookshelf-init:{user_id}"},
        )
        state = await self.session.get(UserBookshelfState, user_id)
        if state is not None:
            return False
        await self._backfill_from_history(user_id)
        self.session.add(UserBookshelfState(user_id=user_id, initialized_at=utc_now()))
        await self.session.flush()
        return True

    async def _backfill_from_history(self, user_id: UUID) -> None:
        events = (
            await self.session.execute(
                select(RecommendationEvent)
                .where(
                    RecommendationEvent.user_id == user_id,
                    RecommendationEvent.event_type.in_(list(EVENT_TO_STATE_CHANGE)),
                )
                .order_by(RecommendationEvent.created_at.asc())
            )
        ).scalars().all()
        interactions = (
            await self.session.execute(
                select(BookInteraction)
                .where(BookInteraction.user_id == user_id)
                .order_by(BookInteraction.created_at.asc())
            )
        ).scalars().all()

        states: dict[str, dict[str, Any]] = {}

        def apply(
            key: str,
            *,
            at: datetime,
            status: str | None,
            evaluation: str | None,
            book_id: UUID | None,
            title: str,
        ) -> None:
            state = states.setdefault(
                key,
                {
                    "book_id": book_id,
                    "title": title,
                    "reading_status": None,
                    "evaluation": None,
                    "last_event_at": at,
                },
            )
            if status is not None:
                state["reading_status"] = status
            if evaluation is not None:
                state["evaluation"] = evaluation
            if state["last_event_at"] is None or at > state["last_event_at"]:
                state["last_event_at"] = at

        for event in events:
            status, evaluation = EVENT_TO_STATE_CHANGE[event.event_type]
            key = (
                f"book:{event.book_id}"
                if event.book_id is not None
                else f"title:{normalize_book_title(event.book_title)}"
            )
            if key.endswith("title:") and not str(event.book_title or "").strip():
                continue
            apply(
                key,
                at=event.created_at,
                status=status,
                evaluation=evaluation,
                book_id=event.book_id,
                title=normalize_recommendation_text(event.book_title),
            )
        for interaction in interactions:
            status, evaluation = INTERACTION_TO_STATE_CHANGE.get(
                normalize_recommendation_token(interaction.interaction_type),
                (None, None),
            )
            if status is None and evaluation is None:
                continue
            key = (
                f"book:{interaction.book_id}"
                if interaction.book_id is not None
                else f"title:{normalize_book_title(interaction.book_title)}"
            )
            if key.endswith("title:") and not str(interaction.book_title or "").strip():
                continue
            apply(
                key,
                at=interaction.created_at,
                status=status,
                evaluation=evaluation,
                book_id=interaction.book_id,
                title=normalize_recommendation_text(interaction.book_title),
            )

        if not states:
            return
        book_ids = {
            state["book_id"]
            for state in states.values()
            if state["book_id"] is not None
        }
        books_by_id: dict[UUID, Book] = {}
        if book_ids:
            rows = (
                await self.session.execute(select(Book).where(Book.id.in_(book_ids)))
            ).scalars().all()
            books_by_id = {book.id: book for book in rows}

        now = utc_now()
        for state in states.values():
            book = books_by_id.get(state["book_id"]) if state["book_id"] else None
            self.session.add(
                UserBookShelf(
                    user_id=user_id,
                    book_id=state["book_id"],
                    title=(
                        book.title
                        if book is not None
                        else str(state["title"] or "").strip()[:256]
                    ),
                    authors=list(book.authors or []) if book is not None else [],
                    tags=list(book.tags or []) if book is not None else [],
                    cover_url=book.cover_url if book is not None else None,
                    source_url=book.source_url if book is not None else None,
                    reading_status=state["reading_status"] or "want_to_read",
                    evaluation=state["evaluation"],
                    last_event_at=state["last_event_at"],
                    created_at=now,
                    updated_at=now,
                )
            )

    # ── Writes (single business entry) ─────────────────────────────────────

    async def upsert_from_feedback(
        self,
        *,
        user_id: UUID,
        interaction_type: str,
        book_title: str = "",
        book_id: UUID | None = None,
        note: str = "",
        rating: int | None = None,
        thread_id: UUID | None = None,
        request_id: str = "",
        source: str = "book_feedback",
    ) -> ReadingServiceResult:
        status, evaluation = INTERACTION_TO_STATE_CHANGE.get(
            normalize_recommendation_token(interaction_type),
            (None, None),
        )
        if status is None and evaluation is None:
            raise ValueError(
                f"interaction_type {interaction_type!r} does not map to a reading state"
            )
        return await self.upsert(
            user_id=user_id,
            book_id=book_id,
            title=book_title,
            reading_status=status,
            evaluation=evaluation,
            note=note,
            rating=rating,
            thread_id=thread_id,
            request_id=request_id,
            source=source,
        )

    async def apply_signal_event(
        self,
        *,
        user_id: UUID,
        event_type: str,
        book_title: str = "",
        thread_id: UUID | None = None,
        request_id: str = "",
        source: str = "agent_tool",
        note: str = "",
    ) -> ReadingServiceResult:
        status, evaluation = EVENT_TO_STATE_CHANGE.get(
            normalize_recommendation_token(event_type),
            (None, None),
        )
        if status is None and evaluation is None:
            raise ValueError(
                f"event_type {event_type!r} is not a reading-state/evaluation signal"
            )
        return await self.upsert(
            user_id=user_id,
            title=book_title,
            reading_status=status,
            evaluation=evaluation,
            note=note,
            thread_id=thread_id,
            request_id=request_id,
            source=source,
        )

    async def upsert(
        self,
        *,
        user_id: UUID,
        book_id: UUID | None = None,
        title: str = "",
        reading_status: str | None = "want_to_read",
        evaluation: Any = _UNSET,
        note: Any = _UNSET,
        rating: Any = _UNSET,
        thread_id: UUID | None = None,
        request_id: str = "",
        source: str = "api",
    ) -> ReadingServiceResult:
        """Upsert the current shelf state and append audit in one transaction.

        ``reading_status=None`` keeps the existing status on update (create
        defaults to want_to_read). ``evaluation=_UNSET`` leaves evaluation
        untouched; ``evaluation=None`` clears it.
        """
        status = _validate_status(reading_status) if reading_status is not None else None
        evaluation_value = _validate_evaluation(evaluation) if evaluation is not _UNSET else _UNSET
        note_value = _clean_note(note) if note is not _UNSET else _UNSET
        rating_value = rating if rating is not _UNSET else _UNSET

        # A write can be the user's first Shelf access. Initialize/backfill
        # before appending new events, otherwise the following GET would replay
        # those same events and attempt to insert a duplicate Shelf row.
        await self.ensure_backfilled(user_id)

        book, resolved_title = await self._resolve_book(
            book_id=book_id,
            title=title,
        )
        if not resolved_title:
            raise ValueError("book_id or title is required")

        entry = await self._find_existing(
            user_id=user_id,
            book_id=book.id if book else None,
            title=resolved_title,
        )

        now = utc_now()
        events: list[RecommendationSignal] = []
        interaction_type: str | None = None

        if entry is None:
            entry = UserBookShelf(
                user_id=user_id,
                book_id=book.id if book else None,
                title=resolved_title,
                authors=list(book.authors or []) if book else [],
                tags=list(book.tags or []) if book else [],
                cover_url=book.cover_url if book else None,
                source_url=book.source_url if book else None,
                reading_status=status or "want_to_read",
                evaluation=(
                    evaluation_value if evaluation_value is not _UNSET else None
                ),
                note=note_value if note_value is not _UNSET else "",
                rating=rating_value if rating_value is not _UNSET else None,
                last_event_at=now,
            )
            self.session.add(entry)
            events.extend(
                await self._record_status_event(
                    user_id=user_id,
                    entry=entry,
                    thread_id=thread_id,
                    request_id=request_id,
                    source=source,
                )
            )
            if entry.evaluation is not None:
                events.extend(
                    await self._record_evaluation_event(
                        user_id=user_id,
                        entry=entry,
                        thread_id=thread_id,
                        request_id=request_id,
                        source=source,
                    )
                )
            interaction_type = STATUS_INTERACTION_TYPES.get(entry.reading_status)
        else:
            if status is not None and status != entry.reading_status:
                entry.reading_status = status
                entry.last_event_at = now
                events.extend(
                    await self._record_status_event(
                        user_id=user_id,
                        entry=entry,
                        thread_id=thread_id,
                        request_id=request_id,
                        source=source,
                    )
                )
            if evaluation_value is not _UNSET and evaluation_value != entry.evaluation:
                entry.evaluation = evaluation_value
                entry.last_event_at = now
                if evaluation_value is not None:
                    events.extend(
                        await self._record_evaluation_event(
                            user_id=user_id,
                            entry=entry,
                            thread_id=thread_id,
                            request_id=request_id,
                            source=source,
                        )
                    )
            if note_value is not _UNSET and note_value != entry.note:
                entry.note = note_value
            if rating_value is not _UNSET and rating_value != entry.rating:
                entry.rating = rating_value
            interaction_type = _interaction_type_for_change(
                entry.reading_status,
                entry.evaluation,
                status,
                evaluation_value if evaluation_value is not _UNSET else None,
            )

        if interaction_type:
            interaction = await create_book_interaction(
                self.session,
                BookInteractionCreate(
                    user_id=user_id,
                    book_id=entry.book_id,
                    book_title=entry.title,
                    interaction_type=interaction_type,
                    note=_clean_note(note_value) if note_value is not _UNSET else None,
                    rating=rating_value if rating_value is not _UNSET else None,
                ),
            )
            interaction_id: UUID | None = interaction.id
        else:
            interaction_id = None

        await self.session.flush()
        await self.session.refresh(entry)
        return ReadingServiceResult(
            shelf=_to_shelf_book(entry),
            interaction_id=interaction_id,
            events=events,
        )
    async def update_entry(
        self,
        entry_id: UUID,
        update: ShelfBookUpdate,
        *,
        thread_id: UUID | None = None,
        request_id: str = "",
        source: str = "api",
    ) -> ReadingServiceResult:
        entry = await self.session.get(UserBookShelf, entry_id)
        if entry is None:
            raise KeyError("shelf entry not found")
        now = utc_now()
        changed_status = False
        events: list[RecommendationSignal] = []
        interaction_id: UUID | None = None
        if (
            "reading_status" in update.model_fields_set
            and update.reading_status is not None
        ):
            status = _validate_status(update.reading_status)
            if status != entry.reading_status:
                entry.reading_status = status
                entry.last_event_at = now
                events.extend(
                    await self._record_status_event(
                        user_id=entry.user_id,
                        entry=entry,
                        thread_id=thread_id,
                        request_id=request_id,
                        source=source,
                    )
                )
                changed_status = True
        if "evaluation" in update.model_fields_set:
            evaluation_value = _validate_evaluation(update.evaluation)
            if evaluation_value != entry.evaluation:
                entry.evaluation = evaluation_value
                entry.last_event_at = now
                if evaluation_value is not None:
                    events.extend(
                        await self._record_evaluation_event(
                            user_id=entry.user_id,
                            entry=entry,
                            thread_id=thread_id,
                            request_id=request_id,
                            source=source,
                        )
                    )
        if "note" in update.model_fields_set:
            entry.note = _clean_note(update.note)
        if "rating" in update.model_fields_set:
            entry.rating = update.rating
        if changed_status:
            interaction = await create_book_interaction(
                self.session,
                BookInteractionCreate(
                    user_id=entry.user_id,
                    book_id=entry.book_id,
                    book_title=entry.title,
                    interaction_type=STATUS_INTERACTION_TYPES.get(
                        entry.reading_status, "read"
                    ),
                    note=entry.note or None,
                    rating=entry.rating,
                ),
            )
            interaction_id = interaction.id
        await self.session.flush()
        await self.session.refresh(entry)
        return ReadingServiceResult(
            shelf=_to_shelf_book(entry),
            interaction_id=interaction_id,
            events=events,
        )

    async def remove_entry(
        self,
        entry_id: UUID,
        *,
        thread_id: UUID | None = None,
        request_id: str = "",
    ) -> bool:
        entry = await self.session.get(UserBookShelf, entry_id)
        if entry is None:
            return False
        from app.services.memory.write_gateway import MemoryWriteGateway

        receipt_id = (
            request_id.strip()[:128]
            or f"shelf-remove-{uuid.uuid4()}"[:128]
        )
        await MemoryWriteGateway(self.session).forget_reading_memory(
            user_id=entry.user_id,
            thread_id=thread_id,
            book_title=entry.title,
            source_event_id=uuid.uuid4(),
            receipt_id=receipt_id,
            evidence_quote=f"移出书架：《{entry.title}》",
        )
        await self.session.delete(entry)
        await self.session.flush()
        return True

    # ── Reads ───────────────────────────────────────────────────────────────

    async def list_entries(
        self,
        *,
        user_id: UUID,
        statuses: list[str] | None = None,
        evaluations: list[str] | None = None,
        q: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ShelfBook], int]:
        stmt = select(UserBookShelf).where(UserBookShelf.user_id == user_id)
        if statuses:
            normalized_statuses = [_validate_status(item) for item in statuses]
            stmt = stmt.where(UserBookShelf.reading_status.in_(normalized_statuses))
        if evaluations:
            normalized_evaluations = [
                _validate_evaluation(item) for item in evaluations if item
            ]
            if normalized_evaluations:
                stmt = stmt.where(UserBookShelf.evaluation.in_(normalized_evaluations))
        query = str(q or "").strip()
        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(
                or_(
                    UserBookShelf.title.ilike(pattern),
                    cast(UserBookShelf.authors, String).ilike(pattern),
                )
            )
        total = await self.session.scalar(
            select(func.count()).select_from(stmt.subquery())
        )
        rows = (
            await self.session.execute(
                stmt.order_by(
                    UserBookShelf.last_event_at.desc().nullslast(),
                    UserBookShelf.updated_at.desc(),
                )
                .offset(max(0, offset))
                .limit(max(1, min(limit, 5000)))
            )
        ).scalars().all()
        return [_to_shelf_book(row) for row in rows], int(total or 0)

    async def enrich_missing_metadata(
        self,
        user_id: UUID,
        *,
        limit: int = 12,
    ) -> int:
        """Fill missing Shelf metadata through one bounded, concurrent lookup.

        ReadingService keeps the write authority. The metadata component only
        returns external facts and an application-owned cached cover URL.
        Lookup failure is fail-open and never changes reading state.
        """
        rows = (
            await self.session.execute(
                select(UserBookShelf)
                .where(UserBookShelf.user_id == user_id)
                .order_by(UserBookShelf.updated_at.desc())
                .limit(max(1, min(limit * 4, 100)))
            )
        ).scalars().all()
        targets = [
            row
            for row in rows
            if not row.authors or not _is_application_cover(row.cover_url)
        ][:limit]
        if not targets:
            return 0

        lookup_targets: list[tuple[UserBookShelf, Book | None]] = []
        for row in targets:
            book = await self.session.get(Book, row.book_id) if row.book_id else None
            if book is None:
                book = await find_book_by_title(self.session, row.title)
            if _metadata_lookup_due(book):
                lookup_targets.append((row, book))

        if not lookup_targets:
            return 0
        results = await asyncio.gather(
            *(
                resolve_book_metadata(
                    row.title,
                    source_url=(book.source_url if book is not None else row.source_url),
                )
                for row, book in lookup_targets
            )
        )

        changed = 0
        for (row, book), metadata in zip(lookup_targets, results, strict=True):
            book = await self._persist_book_metadata(
                book=book,
                fallback_title=row.title,
                metadata=metadata,
            )
            row.book_id = book.id
            if metadata.authors and not row.authors:
                row.authors = list(metadata.authors)
                changed += 1
            if metadata.cover_url and not _is_application_cover(row.cover_url):
                row.cover_url = metadata.cover_url
                changed += 1
            if metadata.source_url and not row.source_url:
                row.source_url = metadata.source_url
        await self.session.flush()
        return changed

    async def _persist_book_metadata(
        self,
        *,
        book: Book | None,
        fallback_title: str,
        metadata: BookMetadata,
    ) -> Book:
        if book is None and metadata.source_url:
            book = (
                await self.session.execute(
                    select(Book).where(Book.source_url == metadata.source_url).limit(1)
                )
            ).scalar_one_or_none()
        if book is None:
            book = Book(
                title=metadata.title or fallback_title,
                authors=[],
                tags=[],
                source_name="douban" if metadata.source_url else "metadata_lookup",
                source_url=metadata.source_url,
                raw_data={},
            )
            self.session.add(book)
            await self.session.flush()
        if metadata.authors:
            book.authors = list(metadata.authors)
        if metadata.cover_url:
            book.cover_url = metadata.cover_url
        if metadata.source_url and not book.source_url:
            book.source_url = metadata.source_url
            book.source_name = "douban"
        raw_data = dict(book.raw_data or {})
        raw_data["douban_metadata"] = {
            "resolver_version": 2,
            "status": metadata.status,
            "attempted_at": utc_now().isoformat(),
            "cover_cached": bool(metadata.cover_url),
            "error": metadata.error,
        }
        book.raw_data = raw_data
        return book

    async def current_snapshot(
        self,
        user_id: UUID,
    ) -> dict[str, tuple[str | None, str | None]]:
        """Shelf state keyed by ``book:<id>`` and ``title:<normalized>``.

        RecommendationProjector consumes this as the authoritative current
        status; audit events are used only for decay scoring.
        """
        rows = (
            await self.session.execute(
                select(UserBookShelf).where(UserBookShelf.user_id == user_id)
            )
        ).scalars().all()
        snapshot: dict[str, tuple[str | None, str | None]] = {}
        for row in rows:
            state = (row.reading_status or None, row.evaluation or None)
            if row.book_id is not None:
                snapshot[f"book:{row.book_id}"] = state
            snapshot[f"title:{normalize_book_title(row.title)}"] = state
        return snapshot

    async def reading_anchors(
        self,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """Read-book style anchors (tags/authors + feedback) for similarity.

        Only books with reading_status == read are anchors: their style is the
        basis for recommending similar books, weighted by per-book feedback.
        """
        rows = (
            await self.session.execute(
                select(UserBookShelf).where(
                    UserBookShelf.user_id == user_id,
                    UserBookShelf.reading_status == "read",
                )
            )
        ).scalars().all()
        return [
            {
                "title": row.title,
                "tags": list(row.tags or []),
                "authors": list(row.authors or []),
                "evaluation": row.evaluation,
            }
            for row in rows
        ]


    # ── Internals ───────────────────────────────────────────────────────────

    async def _resolve_book(
        self,
        *,
        book_id: UUID | None,
        title: str,
    ) -> tuple[Book | None, str]:
        book: Book | None = None
        if book_id is not None:
            book = await self.session.get(Book, book_id)
        resolved_title = ""
        if book is not None:
            resolved_title = normalize_recommendation_text(book.title)
        elif str(title or "").strip():
            book = await find_book_by_title(self.session, str(title).strip())
            resolved_title = (
                normalize_recommendation_text(book.title)
                if book is not None
                else normalize_recommendation_text(title)
            )
        return book, resolved_title

    async def _find_existing(
        self,
        *,
        user_id: UUID,
        book_id: UUID | None,
        title: str,
    ) -> UserBookShelf | None:
        if book_id is not None:
            return (
                await self.session.execute(
                    select(UserBookShelf).where(
                        UserBookShelf.user_id == user_id,
                        UserBookShelf.book_id == book_id,
                    )
                )
            ).scalar_one_or_none()
        normalized = normalize_book_title(title)
        if not normalized:
            return None
        rows = (
            await self.session.execute(
                select(UserBookShelf).where(UserBookShelf.user_id == user_id)
            )
        ).scalars().all()
        for row in rows:
            if normalize_book_title(row.title) == normalized:
                return row
        return None

    async def _record_status_event(
        self,
        *,
        user_id: UUID,
        entry: UserBookShelf,
        thread_id: UUID | None,
        request_id: str,
        source: str,
    ) -> list[RecommendationSignal]:
        event_type = STATUS_EVENT_TYPES.get(entry.reading_status)
        if event_type is None:
            return []
        strength = {
            "want_to_read": 0.75,
            "reading": 0.7,
            "read": 1.0,
            "dropped": 0.5,
        }.get(event_type, 0.5)
        return [
            await self._create_event(
                user_id=user_id,
                entry=entry,
                event_type=event_type,
                polarity="neutral",
                strength=strength,
                thread_id=thread_id,
                request_id=request_id,
                source=source,
                metadata={"shelf_dimension": "reading_status"},
            )
        ]

    async def _record_evaluation_event(
        self,
        *,
        user_id: UUID,
        entry: UserBookShelf,
        thread_id: UUID | None,
        request_id: str,
        source: str,
    ) -> list[RecommendationSignal]:
        event_type = EVALUATION_EVENT_TYPES.get(entry.evaluation or "")
        if event_type is None:
            return []
        polarity = (
            "positive"
            if event_type == "liked"
            else "negative"
            if event_type in {"disliked", "not_interested"}
            else "neutral"
        )
        strength = {"liked": 0.85, "disliked": 0.9, "not_interested": 0.8}.get(
            event_type, 0.5
        )
        return [
            await self._create_event(
                user_id=user_id,
                entry=entry,
                event_type=event_type,
                polarity=polarity,
                strength=strength,
                thread_id=thread_id,
                request_id=request_id,
                source=source,
                metadata={"shelf_dimension": "evaluation"},
            )
        ]

    async def _create_event(
        self,
        *,
        user_id: UUID,
        entry: UserBookShelf,
        event_type: str,
        polarity: str,
        strength: float,
        thread_id: UUID | None,
        request_id: str,
        source: str,
        metadata: dict[str, Any],
    ) -> RecommendationSignal:
        saved = await create_recommendation_event(
            self.session,
            RecommendationSignalCreate(
                user_id=user_id,
                thread_id=thread_id,
                book_id=entry.book_id,
                book_title=entry.title,
                event_type=event_type,
                signal_polarity=polarity,
                signal_strength=strength,
                request_id=request_id,
                source=source,
                metadata=metadata,
            ),
        )
        return recommendation_signal_from_record(saved)


def _interaction_type_for_change(
    current_status: str,
    current_evaluation: str | None,
    status_delta: str | None,
    evaluation_delta: str | None,
) -> str | None:
    if status_delta is not None and STATUS_INTERACTION_TYPES.get(status_delta):
        return STATUS_INTERACTION_TYPES[status_delta]
    if evaluation_delta is not None and EVALUATION_INTERACTION_TYPES.get(evaluation_delta):
        return EVALUATION_INTERACTION_TYPES[evaluation_delta]
    if current_evaluation and EVALUATION_INTERACTION_TYPES.get(current_evaluation):
        return EVALUATION_INTERACTION_TYPES[current_evaluation]
    return STATUS_INTERACTION_TYPES.get(current_status)


def _to_shelf_book(row: UserBookShelf) -> ShelfBook:
    return ShelfBook(
        id=row.id,
        user_id=row.user_id,
        book_id=row.book_id,
        title=row.title,
        authors=list(row.authors or []),
        tags=list(row.tags or []),
        cover_url=row.cover_url,
        source_url=row.source_url,
        reading_status=row.reading_status,
        evaluation=row.evaluation,
        note=row.note or "",
        rating=row.rating,
        last_event_at=row.last_event_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def write_reading_memory_best_effort(
    *,
    user_id: UUID,
    book_title: str,
    reading_status: str | None = None,
    evaluation: str | None = None,
    thread_id: UUID | None = None,
    note: str = "",
    source_kind: str = "book_feedback",
    entity_id: UUID | None = None,
    source_event_id: UUID | None = None,
) -> dict[str, Any]:
    """ReadingService-derived memory MUST enter MemoryWriteGateway.

    This is the only derived-write adapter: it opens its own session and commits
    through MemoryWriteGateway with provenance source_kind=reading_event.
    """
    if not book_title:
        return {
            "saved_memory_ids": [],
            "admissions": [],
            "conflicts": [],
            "skipped": True,
            "reason": "missing_entity_or_title",
        }
    from app.infra.database import get_database
    from app.services.memory.entity_resolver import EntityResolver
    from app.services.memory.write_gateway import MemoryWriteGateway

    db = get_database()
    async with db.session() as session:
        if entity_id is None:
            resolved = await EntityResolver(session).resolve(
                user_id=user_id,
                entity_type="book",
                name=book_title,
            )
            if resolved.status == "ambiguous" or resolved.entity_id is None:
                return {
                    "saved_memory_ids": [],
                    "skipped": True,
                    "reason": "ambiguous_book_entity",
                    "clarification_question": resolved.question,
                }
            entity_id = resolved.entity_id
        gateway = MemoryWriteGateway(session)
        outcome = await gateway.record_reading_memory(
            user_id=user_id,
            thread_id=thread_id,
            book_title=book_title,
            reading_status=reading_status,
            evaluation=evaluation,
            entity_id=entity_id,
            receipt_id=str(uuid.uuid4()),
            source_event_id=source_event_id or uuid.uuid4(),
            evidence=note or book_title,
        )
    return {
        "saved_memory_ids": [],
        "admissions": [],
        "conflicts": [],
        "skipped": outcome.get("status") != "committed",
        "gateway_status": outcome.get("status"),
    }

__all__ = [
    "BOOK_EVALUATIONS",
    "READING_STATUSES",
    "ReadingService",
    "ReadingServiceResult",
    "normalize_book_title",
    "write_reading_memory_best_effort",
]
