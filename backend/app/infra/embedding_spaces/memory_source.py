"""Memory-specific source documents for semantic embedding generations."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.infra.database.database import PostgresDatabase
from app.infra.embedding_spaces.contracts import VectorSourceDocument


_ACTIVE_CANONICAL_MEMORY_FILTER = """
    memory_key IS NOT NULL
    AND operation IS NOT NULL
    AND operation != 'forget'
    AND superseded_by IS NULL
    AND is_deleted = FALSE
"""


class MemoryVectorSourceReader:
    """Expose active canonical memory facts as embedding source documents."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def count(self, table_name: str | None = None) -> int:
        del table_name
        result = await self._database.execute_query(
            text(
                f"""
                SELECT COUNT(1)
                FROM public.memory_events
                WHERE {_ACTIVE_CANONICAL_MEMORY_FILTER}
                """
            )
        )
        return int(result.scalar_one())

    async def read_batch(
        self,
        table_name: str | None = None,
        *,
        offset: int,
        limit: int,
    ) -> list[VectorSourceDocument]:
        del table_name
        result = await self._database.execute_query(
            text(
                f"""
                SELECT
                    id::text,
                    user_id::text,
                    thread_id::text,
                    schema_key,
                    memory_key,
                    version_no,
                    operation,
                    value,
                    metadata,
                    valid_from,
                    updated_at
                FROM public.memory_events
                WHERE {_ACTIVE_CANONICAL_MEMORY_FILTER}
                ORDER BY user_id::text, memory_key, version_no DESC
                OFFSET :offset
                LIMIT :limit
                """
            ),
            {"offset": offset, "limit": limit},
        )
        return [
            memory_row_to_source_document(row)
            for row in result.mappings().all()
        ]

    async def read_updated_batch(
        self,
        table_name: str | None = None,
        *,
        updated_since: datetime,
        offset: int,
        limit: int,
    ) -> list[VectorSourceDocument]:
        del table_name
        result = await self._database.execute_query(
            text(
                f"""
                SELECT
                    id::text,
                    user_id::text,
                    thread_id::text,
                    schema_key,
                    memory_key,
                    version_no,
                    operation,
                    value,
                    metadata,
                    valid_from,
                    updated_at
                FROM public.memory_events
                WHERE {_ACTIVE_CANONICAL_MEMORY_FILTER}
                  AND updated_at >= :updated_since
                ORDER BY updated_at, user_id::text, memory_key
                OFFSET :offset
                LIMIT :limit
                """
            ),
            {
                "updated_since": updated_since,
                "offset": offset,
                "limit": limit,
            },
        )
        return [
            memory_row_to_source_document(row)
            for row in result.mappings().all()
        ]

    async def watermark(self) -> datetime:
        result = await self._database.execute_query(
            text("SELECT clock_timestamp()")
        )
        return result.scalar_one()


def memory_row_to_source_document(row: Any) -> VectorSourceDocument:
    metadata = _row_get(row, "metadata") or {}
    memory_v2 = metadata.get("memory_v2") if isinstance(metadata, dict) else None
    memory_v2 = memory_v2 if isinstance(memory_v2, dict) else {}
    value = memory_v2.get("value") if memory_v2 else None
    qualifiers = memory_v2.get("qualifiers") if memory_v2 else None
    evidence = str(memory_v2.get("evidence_quote") or "").strip()
    if value is None:
        value = _decode_value(_row_get(row, "value"))
    if not evidence:
        evidence = str(_row_get(row, "value") or "").strip()

    user_id = str(_row_get(row, "user_id") or "")
    schema_key = str(_row_get(row, "schema_key") or "")
    memory_key = str(_row_get(row, "memory_key") or "")
    content = "\n".join(
        item
        for item in (
            f"schema: {schema_key}",
            f"predicate: {memory_v2.get('predicate') or schema_key}",
            f"value: {_stable_json(value)}",
            f"qualifiers: {_stable_json(qualifiers or {})}",
            f"evidence: {evidence}",
        )
        if item.strip()
    )
    return VectorSourceDocument(
        id=f"memory:{_row_get(row, 'id')}",
        collection_name=f"memory:{user_id}",
        content=content,
        metadata={
            "source": "memory_events",
            "user_id": user_id,
            "thread_id": str(_row_get(row, "thread_id") or ""),
            "schema_key": schema_key,
            "memory_key": memory_key,
            "version_no": int(_row_get(row, "version_no") or 0),
            "operation": str(_row_get(row, "operation") or ""),
            "valid_from": str(_row_get(row, "valid_from") or ""),
            "updated_at": str(_row_get(row, "updated_at") or ""),
        },
    )


def _row_get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, TypeError):
        return getattr(row, key, None)


def _decode_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


__all__ = ["MemoryVectorSourceReader", "memory_row_to_source_document"]
