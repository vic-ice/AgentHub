"""Live Chat E2E for the single RecommendationService owner."""

from __future__ import annotations

import argparse
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


def _http_flow(user_id: str, thread_id: str, model_uuid: str) -> dict:
    message = (
        "2026年有啥值得推荐看的书吗，比如《非暴力沟通》、"
        "《小狗钱钱》这种类型的？请推荐几本我书架之外的新书。"
    )
    with httpx.Client(timeout=240) as client:
        for title, status in (("非暴力沟通", "want_to_read"), ("小狗钱钱", "read")):
            seeded = client.post(
                f"{BASE}/books/shelf",
                json={"user_id": user_id, "title": title, "reading_status": status},
            )
            seeded.raise_for_status()

        request_id = str(uuid.uuid4())
        completed = False
        failure = ""
        request = {
            "content": message,
            "thread_id": thread_id,
            "user_id": user_id,
            "request_id": request_id,
            "thinking_mode": False,
        }
        if model_uuid:
            request["model_uuid"] = model_uuid
        with client.stream(
            "POST",
            f"{BASE}/chat/stream",
            json=request,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "answer.completed":
                    completed = True
                elif event.get("type") == "turn.failed":
                    failure = str(event.get("content") or "")
        assert completed, failure

        history = client.get(
            f"{BASE}/chat/history/{thread_id}", params={"user_id": user_id}
        )
        history.raise_for_status()
        answer = next(
            str(item.get("content") or "")
            for item in reversed(history.json().get("messages", []))
            if item.get("type") == "ai" and item.get("request_id") == request_id
        )
        assert "安全控制轮数上限" not in answer, answer
        assert "未继续执行" not in answer, answer
        assert len(answer.strip()) >= 20, answer

        trace = client.get(
            f"{BASE}/traces/{thread_id}/dag/{request_id}",
            params={"user_id": user_id},
        )
        trace.raise_for_status()
        operations = [
            str(step.get("tool_name") or "")
            for step in trace.json().get("steps", [])
            if step.get("message_type") == "tool"
        ]
        assert operations.count("book_search_v1") == 1, operations
        assert "web_search_v1" not in operations, operations
        return {
            "status": "passed",
            "request_id": request_id,
            "operations": operations,
            "answer_chars": len(answer),
        }


async def _main(model_uuid: str) -> int:
    from app.infra.database import (
        dispose_database,
        get_database,
        init_database_connection,
    )

    user_id, thread_id = uuid.uuid4(), uuid.uuid4()
    await init_database_connection()
    database = get_database()
    try:
        async with database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO public.users (id, display_name, is_mock_user) "
                    "VALUES (:user_id, 'Recommendation Chat E2E', true)"
                ),
                {"user_id": user_id},
            )
            await session.execute(
                text(
                    "INSERT INTO public.conversations (thread_id, user_id, title) "
                    "VALUES (:thread_id, :user_id, 'Recommendation Chat E2E')"
                ),
                {"thread_id": thread_id, "user_id": user_id},
            )
        result = await asyncio.to_thread(
            _http_flow, str(user_id), str(thread_id), model_uuid
        )
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


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-uuid", default="")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_arguments().model_uuid)))
