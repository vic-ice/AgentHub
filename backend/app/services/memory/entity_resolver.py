"""EntityResolver — resolve one fact's entity expression to a stable entity_id.

Every entity fact binds a stable id from memory_entities; referents are never
stored as raw text ("马 / 这本 / 它"). Rules (single source of identity):

- same user + entity_type + canonical_name -> existing entity (reuse id)
- alias match -> existing entity
- multiple candidates -> ambiguous (Clarification Gate asks; zero write)
- no candidate -> create a new entity row (id stable from now on)

Books additionally link the books cache (external_ref.book_id) so ReadingService
can sync the frontend bookshelf.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.book import find_book_by_title
from app.models.memory import MemoryEntityRecord
from app.services.memory.classification import derive_domain_from_entity_type

_STRIP_RE = re.compile(r"[《》「」『』“”‘’\"'，。！？、\s]+")


def canonical_entity_name(value: Any) -> str:
    text = str(value or "").strip()
    text = _STRIP_RE.sub("", text)
    return text.lower()


@dataclass
class EntityResolution:
    status: str  # existing | created | ambiguous
    entity_id: UUID | None = None
    entity_type: str = ""
    canonical_name: str = ""
    question: str = ""
    matched_by: str = ""


class EntityResolver:
    """Owns memory_entities identity rules for one user."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def resolve(
        self,
        *,
        user_id: UUID,
        entity_type: str,
        name: str,
        aliases: list[str] | None = None,
        external_ref: dict[str, Any] | None = None,
    ) -> EntityResolution:
        canonical = canonical_entity_name(name)

        if not str(entity_type or "").strip():
            return await self._resolve_by_name_only(
                user_id=user_id,
                name=name,
                canonical=canonical,
            )

        if not canonical or not entity_type:
            return EntityResolution(
                status="ambiguous",
                question=(
                    f"你说的“{name}”具体指哪个对象？请给出名字或类型（书/人/宠物/物品/地点/账号/项目）。"
                ),
            )
        entity_type = entity_type.strip().lower()
        domain = derive_domain_from_entity_type(entity_type)

        # exact canonical match
        exact = (
            await self.session.execute(
                select(MemoryEntityRecord).where(
                    MemoryEntityRecord.user_id == user_id,
                    MemoryEntityRecord.entity_type == entity_type,
                    MemoryEntityRecord.canonical_name == canonical,
                )
            )
        ).scalar_one_or_none()
        if exact is not None:
            return EntityResolution(
                status="existing",
                entity_id=exact.id,
                entity_type=entity_type,
                canonical_name=exact.canonical_name,
                matched_by="canonical_name",
            )

        # alias match
        alias_hits = (
            await self.session.execute(
                select(MemoryEntityRecord).where(
                    MemoryEntityRecord.user_id == user_id,
                    MemoryEntityRecord.entity_type == entity_type,
                )
            )
        ).scalars().all()
        alias_matches = [
            row
            for row in alias_hits
            if canonical in {canonical_entity_name(item) for item in (row.aliases or [])}
        ]
        if len(alias_matches) == 1:
            row = alias_matches[0]
            return EntityResolution(
                status="existing",
                entity_id=row.id,
                entity_type=entity_type,
                canonical_name=row.canonical_name,
                matched_by="alias",
            )
            _upsert_alias(row, name)
        if len(alias_matches) > 1:
            return EntityResolution(
                status="ambiguous",
                entity_type=entity_type,
                canonical_name=canonical,
                question=(
                    f"“{name}”匹配到多个同类对象，请说明具体是哪一个。"
                ),
            )

        # create new entity
        ref = dict(external_ref or {})
        if entity_type == "book":
            book = await find_book_by_title(self.session, str(name or "").strip())
            if book is not None:
                ref["book_id"] = str(book.id)
                if not ref.get("source_url"):
                    ref["source_url"] = book.source_url
        entity = MemoryEntityRecord(
            user_id=user_id,
            entity_type=entity_type,
            canonical_name=canonical,
            aliases=_merge_aliases(aliases, name),
            domain=domain,
            external_ref=ref,
        )
        self.session.add(entity)
        await self.session.flush()
        await self.session.refresh(entity)
        return EntityResolution(
            status="created",
            entity_id=entity.id,
            entity_type=entity_type,
            canonical_name=canonical,
            matched_by="created",
        )


    async def _resolve_by_name_only(
        self,
        *,
        user_id: UUID,
        name: str,
        canonical: str,
    ) -> EntityResolution:
        """Resolve an untyped entity without asking another semantic model."""
        if not canonical:
            return EntityResolution(
                status="ambiguous",
                question=f"“{name}”具体指哪个对象？请补充对象类型。",
            )
        rows = (
            await self.session.execute(
                select(MemoryEntityRecord).where(
                    MemoryEntityRecord.user_id == user_id,
                )
            )
        ).scalars().all()
        hits = [
            row
            for row in rows
            if canonical == row.canonical_name
            or canonical in {
                canonical_entity_name(item) for item in (row.aliases or [])
            }
        ]
        if len(hits) == 1:
            row = hits[0]
            _upsert_alias(row, name)
            return EntityResolution(
                status="existing",
                entity_id=row.id,
                entity_type=row.entity_type,
                canonical_name=row.canonical_name,
                matched_by="name_only",
            )
        if len(hits) > 1:
            return EntityResolution(
                status="ambiguous",
                canonical_name=canonical,
                question=f"“{name}”匹配到多个同名对象，请说明具体是哪一个。",
            )
        return EntityResolution(
            status="ambiguous",
            canonical_name=canonical,
            question=f"“{name}”是新对象吗？请补充对象类型。",
        )


def _merge_aliases(aliases: list[str] | None, name: Any) -> list[str]:
    result: list[str] = []
    for item in [*(aliases or []), name]:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _upsert_alias(row: MemoryEntityRecord, name: Any) -> None:
    aliases = list(row.aliases or [])
    text = str(name or "").strip()
    if text and text not in aliases:
        aliases.append(text)
        row.aliases = aliases




__all__ = ["EntityResolution", "EntityResolver", "canonical_entity_name"]
