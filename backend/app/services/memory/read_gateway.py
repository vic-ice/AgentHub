"""MemoryReadGateway — unified query dispatch.

Queries enter semantically and are routed to the right read model:
- reading-history intent -> Shelf / ReadingEvents
- entity-bearing query      -> Entity Facts (entity-first, current facts)
- otherwise                 -> Memory search / Profile read model
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

_READING_HISTORY_RE = re.compile(
    r"读了哪些|读过哪些|看过哪些|读了什么书|读过什么书|看过什么书|读过的书|看过的书|reading history",
    re.IGNORECASE,
)


@dataclass
class ReadResult:
    kind: str  # shelf | entity_facts | memory | profile | empty
    payload: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryReadGateway:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def current_versions(self, *, user_id: UUID, limit: int = 100):
        """Authoritative current version heads for API/controller consumers."""
        from app.services.memory.current_projection import (
            collapse_semantic_current,
        )
        from app.services.memory.version_store import MemoryVersionStore

        return collapse_semantic_current(
            await MemoryVersionStore(self.session).list_current(
                user_id=user_id,
                limit=limit,
            )
        )

    async def history_versions(self, *, user_id: UUID, limit: int = 100):
        """Authoritative version history; filtering belongs to the caller view."""
        from app.services.memory.version_store import MemoryVersionStore

        return await MemoryVersionStore(self.session).list_history(
            user_id=user_id,
            limit=limit,
        )

    async def history_versions_by_key(
        self,
        *,
        user_id: UUID,
        memory_key: str,
    ):
        """Read one complete version chain without a user-wide scan limit."""
        from app.services.memory.version_store import MemoryVersionStore

        return await MemoryVersionStore(self.session).history(
            user_id=user_id,
            memory_key=memory_key,
        )

    async def current_versions_by_keys(
        self,
        *,
        user_id: UUID,
        memory_keys: list[str] | tuple[str, ...],
    ):
        from app.services.memory.current_projection import (
            collapse_semantic_current,
        )
        from app.services.memory.version_store import MemoryVersionStore

        return collapse_semantic_current(
            await MemoryVersionStore(self.session).list_current_by_memory_keys(
                user_id=user_id,
                memory_keys=memory_keys,
            )
        )

    async def profile_memories(
        self,
        *,
        user_id: UUID,
        memory_types: list[str],
        limit: int = 100,
    ):
        """Current profile events for recommendation/profile projections."""
        from app.services.memory.providers.postgres import PostgresMemoryProvider

        result = await PostgresMemoryProvider(self.session).list_current(
            user_id=user_id,
            query="",
            memory_types=memory_types,
            limit=limit,
        )
        return result.memories

    async def search(self, *, user_id: UUID, query: str, limit: int = 10) -> ReadResult:
        q = str(query or "").strip()
        if _READING_HISTORY_RE.search(q):
            return await self._shelf_events(user_id=user_id, limit=limit)
        entity = await self._entity_facts(user_id=user_id, query=q, limit=limit)
        if entity is not None:
            return entity
        return await self._memory_search(user_id=user_id, query=q, limit=limit)

    async def _shelf_events(self, *, user_id: UUID, limit: int) -> ReadResult:
        from app.services.books.reading_service import ReadingService

        items, total = await ReadingService(self.session).list_entries(
            user_id=user_id, limit=limit, offset=0
        )
        return ReadResult(
            kind="shelf",
            payload={"items": [i.model_dump(mode="json") for i in items], "total": total},
            metadata={"route": "reading_history"},
        )

    async def _entity_facts(
        self, *, user_id: UUID, query: str, limit: int
    ) -> ReadResult | None:
        from app.services.memory.providers.postgres import PostgresMemoryProvider

        provider = PostgresMemoryProvider(self.session)
        rows = await provider._search_entity_facts(
            user_id=user_id, query=query, limit=limit
        )
        if rows is None or not rows:
            return None
        return ReadResult(
            kind="entity_facts",
            payload=[provider._event_from_record(r).model_dump(mode="json") for r in rows],
            metadata={"route": "entity_facts"},
        )

    async def _memory_search(self, *, user_id: UUID, query: str, limit: int) -> ReadResult:
        from app.services.memory.providers.postgres import PostgresMemoryProvider

        result = await PostgresMemoryProvider(self.session).search(
            user_id=user_id, query=query, limit=limit
        )
        return ReadResult(
            kind="memory",
            payload={
                "events": [e.model_dump(mode="json") for e in result.relevant_events],
                "profile_summary": result.profile_summary,
            },
            metadata={"route": "memory"},
        )


__all__ = ["MemoryReadGateway", "ReadResult"]
