"""Live HTTP verification for admin Memory edit/read/forget provenance."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

BASE = "http://127.0.0.1:8080/api/v1"


def _http_flow(user_id: str) -> dict:
    evidence = "Admin E2E: my name is Gateway Audit"
    with httpx.Client(timeout=60) as client:
        edit = client.post(
            f"{BASE}/memory/edit",
            json={
                "user_id": user_id,
                "predicate": "name",
                "value": {"name": "Gateway Audit"},
                "evidence_quote": evidence,
            },
        )
        edit.raise_for_status()
        edit_receipt = edit.json()
        assert edit_receipt["mutations"][0]["status"] == "created", edit_receipt

        current = client.get(f"{BASE}/memory/{user_id}/current")
        current.raise_for_status()
        facts = current.json()["facts"]
        assert len(facts) == 1, facts
        assert facts[0]["value"]["name"] == "Gateway Audit", facts
        assert facts[0]["domain"] == "personal", facts
        memory_key = facts[0]["memory_key"]

        forget = client.post(
            f"{BASE}/memory/forget",
            json={
                "user_id": user_id,
                "predicate": "name",
                "identity": {},
                "evidence_quote": "Admin E2E: forget my name",
            },
        )
        forget.raise_for_status()
        forget_receipt = forget.json()
        assert forget_receipt["mutations"][0]["status"] == "forgotten", forget_receipt

        after = client.get(f"{BASE}/memory/{user_id}/current")
        after.raise_for_status()
        assert after.json()["facts"] == [], after.json()

        history = client.get(
            f"{BASE}/memory/{user_id}/history",
            params={"memory_key": memory_key},
        )
        history.raise_for_status()
        versions = history.json()["versions"]
        assert [item["version_no"] for item in versions] == [1, 2], versions
        assert versions[-1]["is_tombstone"] is True, versions
        return {
            "status": "passed",
            "source_kind": "admin_action",
            "memory_key": memory_key,
            "versions": len(versions),
        }


async def _main() -> int:
    from app.infra.database import (
        dispose_database,
        get_database,
        init_database_connection,
    )

    user_id = uuid.uuid4()
    await init_database_connection()
    database = get_database()
    try:
        async with database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO public.users (id, display_name, is_mock_user) "
                    "VALUES (:user_id, 'Memory Admin HTTP E2E', true)"
                ),
                {"user_id": user_id},
            )
        result = await asyncio.to_thread(_http_flow, str(user_id))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        try:
            async with database.session() as session:
                await session.execute(
                    text(
                        "DELETE FROM public.users WHERE id = :user_id "
                        "AND is_mock_user = true"
                    ),
                    {"user_id": user_id},
                )
        finally:
            await dispose_database()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
