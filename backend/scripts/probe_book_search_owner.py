"""Isolated live probe for the ordinary RecommendationService owner."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import text


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


async def _main() -> int:
    from app.infra.database import (
        dispose_database,
        get_database,
        init_database_connection,
    )
    from app.services.books.recommendation_service import RecommendationService
    from app.services.external_capabilities.contracts import BookSearchInput

    user_id = uuid.uuid4()
    await init_database_connection()
    database = get_database()
    try:
        async with database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO public.users (id, display_name, is_mock_user) "
                    "VALUES (:user_id, 'Book Search Owner Probe', true)"
                ),
                {"user_id": user_id},
            )
        async with database.session() as session:
            result = await RecommendationService(
                session,
                enable_external_enrichment=True,
            ).search(
                BookSearchInput(
                    query="类似《非暴力沟通》、《小狗钱钱》的书 推荐",
                    mode="recommendation",
                    reference_titles=["非暴力沟通", "小狗钱钱"],
                    excluded_titles=["非暴力沟通", "小狗钱钱"],
                    limit=5,
                ),
                user_id=user_id,
            )
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0
    finally:
        async with database.session() as session:
            await session.execute(
                text(
                    "DELETE FROM public.users WHERE id = :user_id "
                    "AND is_mock_user = true"
                ),
                {"user_id": user_id},
            )
        await dispose_database()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
