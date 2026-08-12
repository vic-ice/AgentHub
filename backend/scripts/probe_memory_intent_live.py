"""Live probe for the memory intent state machine over the chat API.

The rule router bypasses the model for matched memory questions, so this
Runs against a randomly created isolated user/thread and deletes the user
afterwards, so the real user thread is never polluted. Run from backend/.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx


BASE = "http://127.0.0.1:8081/api/v1"

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
                    VALUES (:user_id, 'Memory Probe Isolated', true)
                    """
                ),
                {"user_id": user_id},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO public.conversations (thread_id, user_id, title)
                    VALUES (:thread_id, :user_id, 'Memory Probe Isolated')
                    """
                ),
                {"thread_id": thread_id, "user_id": user_id},
            )
        return await asyncio.to_thread(
            _run_questions,
            user_id,
            thread_id,
        )
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


def _run_questions(user_id: str, thread_id: str) -> int:
    with httpx.Client(timeout=20) as client:
        models = client.get(f"{BASE}/models").json()
        llm = next(
            (
                m
                for m in models.get("models", [])
                if m.get("model_type") == "llm" and m.get("is_active")
            ),
            None,
        )
        if llm is None:
            print("no active llm")
            return 1
        model_uuid = llm.get("model_uuid") or llm.get("id")

        for name in ("冰露", "黄仁"):
            response = client.post(
                f"{BASE}/memory/edit",
                json={
                    "user_id": user_id,
                    "predicate": "name",
                    "value": {"name": name},
                    "evidence_quote": f"我在记忆面板把名字改为{name}",
                },
            )
            if response.status_code not in (200, 201):
                print(f"seed memory {name} failed: HTTP {response.status_code}")
                return 1

        for question in ("我是谁", "我之前叫什么", "什么时候说的我叫冰露"):
            payload = {
                "content": question,
                "thread_id": thread_id,
                "user_id": user_id,
                "request_id": str(uuid.uuid4()),
                "model_uuid": model_uuid,
                "thinking_mode": False,
            }
            print(f"Q: {question}")
            try:
                with client.stream(
                    "POST",
                    f"{BASE}/chat/stream",
                    json=payload,
                    timeout=20,
                ) as response:
                    answer = ""
                    failed = ""
                    raw_lines: list[str] = []
                    for line in response.iter_lines():
                        if not line:
                            continue
                        raw_lines.append(line)
                        if not line.startswith("data:"):
                            continue
                        try:
                            data = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if data.get("type") == "answer.completed":
                            content = data.get("content", {})
                            if isinstance(content, dict):
                                answer_obj = content.get("answer") or {}
                                if isinstance(answer_obj, dict):
                                    answer = str(answer_obj.get("content", ""))
                                else:
                                    answer = str(answer_obj or "")
                            else:
                                answer = str(content or "")
                        if data.get("type") == "turn.failed":
                            failed = str(data.get("content", ""))
                    if answer:
                        print(f"A: {answer}")
                    elif failed:
                        print(f"FAILED: {failed}")
                    else:
                        print("NO_ANSWER")
                        for raw in raw_lines[:5]:
                            print("RAW:", raw[:200])
            except Exception as exc:
                print(f"ERR: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
