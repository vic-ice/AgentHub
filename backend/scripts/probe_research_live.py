"""Live deep-research probe for the exact book-recommendation phrasing.

Runs against a randomly created isolated user/thread and deletes the user
afterwards, so the real user thread is never polluted.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx


BASE = "http://127.0.0.1:8081/api/v1"
QUESTION = "深度搜索下类似《失控》《写给管理者的睡前故事》这种风格书籍推荐给我"

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(_probe_async())


async def _probe_async() -> int:
    from sqlalchemy import text

    from app.infra.database import (
        dispose_database,
        get_database,
        init_database_connection,
    )

    user_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    await init_database_connection()
    try:
        database = get_database()
        async with database.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO public.users (id, display_name, is_mock_user)
                    VALUES (:user_id, 'Research Probe Isolated', true)
                    """
                ),
                {"user_id": user_id},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO public.conversations (thread_id, user_id, title)
                    VALUES (:thread_id, :user_id, 'Research Probe Isolated')
                    """
                ),
                {"thread_id": thread_id, "user_id": user_id},
            )
        print("THREAD", thread_id)
        result = await asyncio.to_thread(_stream_answer, user_id, thread_id)
        answer, failed = result
        if answer:
            target = Path(
                r"F:\book_data\test01\local-dev\research_probe_answer.txt"
            )
            target.write_text(answer, encoding="utf-8")
            print(f"ANSWER_SAVED len={len(answer)} -> {target}")
            return 0
        if failed:
            print("FAILED:", failed)
            return 1
        print("NO_ANSWER")
        return 1
    finally:
        try:
            database = get_database()
            async with database.session() as session:
                await session.execute(
                    text("DELETE FROM public.users WHERE id = :user_id"),
                    {"user_id": user_id},
                )
        finally:
            await dispose_database()


def _stream_answer(user_id: str, thread_id: str) -> tuple[str, str]:
    with httpx.Client(timeout=180) as client:
        models = client.get(f"{BASE}/models").json()
        default_id = models.get("default_llm")
        llm = next(
            (
                m
                for m in models.get("models", [])
                if (m.get("id") == default_id or m.get("model_uuid") == default_id)
                and m.get("model_type") == "llm"
                and m.get("is_active")
            ),
            None,
        ) or next(
            (
                m
                for m in models.get("models", [])
                if m.get("model_type") == "llm" and m.get("is_active")
            ),
            None,
        )
        if llm is None:
            return "", "no active llm"
        model_uuid = llm.get("model_uuid") or llm.get("id")
        payload = {
            "content": QUESTION,
            "thread_id": thread_id,
            "user_id": user_id,
            "request_id": str(uuid.uuid4()),
            "model_uuid": model_uuid,
            "thinking_mode": True,
        }
        answer = ""
        failed = ""
        with client.stream(
            "POST",
            f"{BASE}/chat/stream",
            json=payload,
            timeout=180,
        ) as response:
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload_text = line[5:].strip()
                if not payload_text or payload_text == "[DONE]":
                    continue
                try:
                    data = json.loads(payload_text)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "answer.completed":
                    content = data.get("content", {})
                    if isinstance(content, dict):
                        answer_obj = content.get("answer") or {}
                    if isinstance(answer_obj, dict):
                        answer = str(answer_obj.get("content", ""))
                if data.get("type") == "turn.failed":
                    failed = str(data.get("content", ""))
        return answer, failed


if __name__ == "__main__":
    sys.exit(main())
