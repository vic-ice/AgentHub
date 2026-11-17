from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.infra.database.factory import get_embedding_space_runtime


logger = logging.getLogger(__name__)

DEFAULT_MEMORY_VECTOR_RECALL_LIMIT = 16
MIN_MEMORY_VECTOR_RECALL_SCORE = 0.60


async def recall_memory_keys(
    *,
    user_id: UUID,
    query: str,
    limit: int = DEFAULT_MEMORY_VECTOR_RECALL_LIMIT,
    min_score: float = MIN_MEMORY_VECTOR_RECALL_SCORE,
) -> tuple[str, ...]:
    """Return semantic recall hints for active facts in the user's collection.

    The returned keys are only ranking hints.  Callers still load canonical
    versioned memory records from memory_events before exposing any fact.
    """

    normalized_query = " ".join(str(query or "").split())
    if not normalized_query:
        return ()

    try:
        store = get_embedding_space_runtime().get_active_store("memory")
        rows = await store.search(
            collection_name=f"memory:{user_id}",
            query_text=normalized_query,
            limit=max(1, int(limit)),
        )
    except Exception:
        logger.debug("Memory vector recall is unavailable", exc_info=True)
        return ()

    keys: list[str] = []
    seen: set[str] = set()
    expected_user = str(user_id)
    for row in rows:
        if _row_score(row) < min_score:
            continue
        payload = _row_payload(row)
        payload_user_id = str(payload.get("user_id") or "")
        if payload_user_id and payload_user_id != expected_user:
            continue
        memory_key = str(
            payload.get("memory_key")
            or (row.get("memory_key") if isinstance(row, dict) else "")
            or ""
        ).strip()
        if not memory_key or memory_key in seen:
            continue
        seen.add(memory_key)
        keys.append(memory_key)
    return tuple(keys)


def _row_payload(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        payload = row.get("payload")
        return payload if isinstance(payload, dict) else row
    return {}


def _row_score(row: Any) -> float:
    if not isinstance(row, dict):
        return 0.0
    try:
        return float(row.get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["recall_memory_keys"]
