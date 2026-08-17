"""Live black-box acceptance for authoritative Bookshelf reads through Chat.

The implementation is intentionally not involved in the assertions: every case
uses the real HTTP Chat stream, configured Controller model, Runtime, Reading
Service and PostgreSQL.  Each natural-language expression runs in a fresh
conversation so earlier wording cannot teach or bias later cases.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text


BASE = "http://127.0.0.1:8080/api/v1"
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

EXPRESSIONS = (
    "我现在书架有哪些书呢？",
    "一共有哪几本书，然后我对每本书评价是什么呢？",
    "我的书架现在有哪些书？",
    "书架里都放了什么？",
    "列一下我收藏的书和当前状态。",
    "我一共有几本书？分别叫什么？",
    "把每本书的阅读进度和评价给我。",
    "哪些书在我的书架上？",
    "说说我的在读、想读和已读清单。",
    "我目前保存了哪些书？",
    "帮我盘点一下书架。",
    "书架总数是多少，分别是什么？",
    "我对书架里的每本书怎么评价的？",
    "哪些书我已经读过，哪些还想读？",
    "把我的阅读清单完整列出来。",
    "当前我的阅读资产有哪些？",
    "我名下的书单是什么？",
    "看看我自己的书架，不要推荐新书。",
    "只汇总我已经加入书架的书。",
    "列出书名、阅读状态和我的评价。",
    "我都把哪些书放进书架了？",
    "给我一个当前书架概览。",
    "翻翻我的个人书架，告诉我里面有什么。",
    "现有藏书请按状态和评价一起说明。",
)

EXPECTED_LINES = (
    "《财富自由之路》：想读；评价：喜欢",
    "《失控》：在读；评价：一般",
    "《小狗钱钱》：已读；评价：不喜欢",
)

FILTERED_EXPECTATIONS = {
    "哪些书我已经读过，哪些还想读？": (
        "《财富自由之路》：想读；评价：喜欢",
        "《小狗钱钱》：已读；评价：不喜欢",
    ),
}


def _default_model(client: httpx.Client) -> str:
    response = client.get(f"{BASE}/models")
    response.raise_for_status()
    payload = response.json()
    default_id = str(payload.get("default_llm") or "")
    if default_id:
        return default_id
    for model in payload.get("models", []):
        if model.get("model_type") == "llm" and model.get("is_active"):
            return str(model.get("model_uuid") or model.get("id") or "")
    raise AssertionError("no active LLM is configured")


def _seed_shelf(client: httpx.Client, user_id: str) -> None:
    entries = (
        ("财富自由之路", "want_to_read", "liked"),
        ("失控", "reading", "neutral"),
        ("小狗钱钱", "read", "disliked"),
    )
    for title, status, evaluation in entries:
        response = client.post(
            f"{BASE}/books/shelf",
            json={
                "user_id": user_id,
                "title": title,
                "reading_status": status,
                "evaluation": evaluation,
                "note": "bookshelf-read-e2e",
            },
        )
        response.raise_for_status()


def _send_chat(
    client: httpx.Client,
    *,
    user_id: str,
    thread_id: str,
    model_uuid: str,
    message: str,
) -> str:
    request_id = str(uuid.uuid4())
    failure = ""
    completed = False
    with client.stream(
        "POST",
        f"{BASE}/chat/stream",
        json={
            "content": message,
            "thread_id": thread_id,
            "user_id": user_id,
            "request_id": request_id,
            "model_uuid": model_uuid,
            "thinking_mode": False,
        },
        timeout=180,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            event_type = str(event.get("type") or "")
            if event_type == "answer.completed":
                completed = True
            elif event_type == "turn.failed":
                failure = str(event.get("content") or "")
    if not completed:
        raise AssertionError(
            f"chat did not complete for {message!r}; failure={failure!r}"
        )
    return request_id


def _answer(
    client: httpx.Client,
    *,
    user_id: str,
    thread_id: str,
    request_id: str,
) -> str:
    response = client.get(
        f"{BASE}/chat/history/{thread_id}",
        params={"user_id": user_id},
    )
    response.raise_for_status()
    for message in reversed(response.json().get("messages", [])):
        if (
            message.get("type") == "ai"
            and str(message.get("request_id") or "") == request_id
        ):
            return str(message.get("content") or "")
    raise AssertionError(f"persisted answer missing for request {request_id}")


def _operations(
    client: httpx.Client,
    *,
    user_id: str,
    thread_id: str,
    request_id: str,
) -> list[str]:
    response = client.get(
        f"{BASE}/traces/{thread_id}/dag/{request_id}",
        params={"user_id": user_id},
    )
    response.raise_for_status()
    return [
        str(step.get("tool_name"))
        for step in response.json().get("steps", [])
        if step.get("message_type") == "tool"
    ]


def _assert_answer(
    answer: str,
    expression: str,
    expected_lines: tuple[str, ...],
) -> None:
    expected_total = len(expected_lines)
    if f"你的书架目前有 {expected_total} 本书" not in answer:
        raise AssertionError(f"{expression!r}: incorrect total: {answer!r}")
    for line in expected_lines:
        if line not in answer:
            raise AssertionError(f"{expression!r}: missing {line!r}: {answer!r}")
    for title, line in zip(
        ("财富自由之路", "失控", "小狗钱钱"),
        EXPECTED_LINES,
        strict=True,
    ):
        expected_count = 1 if line in expected_lines else 0
        if answer.count(f"《{title}》") != expected_count:
            raise AssertionError(
                f"{expression!r}: title {title!r} is duplicated or missing: {answer!r}"
            )
    if "长期记忆" in answer:
        raise AssertionError(f"{expression!r}: leaked Memory fallback: {answer!r}")


def _run_http(
    user_id: str,
    thread_ids: list[str],
    selected_model_uuid: str = "",
) -> dict[str, Any]:
    results: list[dict[str, str]] = []
    with httpx.Client(timeout=180) as client:
        model_uuid = selected_model_uuid or _default_model(client)
        _seed_shelf(client, user_id)
        for index, (thread_id, expression) in enumerate(
            zip(thread_ids, EXPRESSIONS, strict=True),
            start=1,
        ):
            request_id = _send_chat(
                client,
                user_id=user_id,
                thread_id=thread_id,
                model_uuid=model_uuid,
                message=expression,
            )
            answer = _answer(
                client,
                user_id=user_id,
                thread_id=thread_id,
                request_id=request_id,
            )
            operations = _operations(
                client,
                user_id=user_id,
                thread_id=thread_id,
                request_id=request_id,
            )
            _assert_answer(
                answer,
                expression,
                FILTERED_EXPECTATIONS.get(expression, EXPECTED_LINES),
            )
            if operations != ["bookshelf_read_v1"]:
                raise AssertionError(
                    f"{expression!r}: operations={operations!r}, expected one "
                    "authoritative bookshelf_read_v1"
                )
            results.append(
                {
                    "case": str(index),
                    "expression": expression,
                    "request_id": request_id,
                }
            )
            print(
                json.dumps(
                    {"case": index, "status": "passed", "expression": expression},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return {
        "status": "passed",
        "model_uuid": model_uuid,
        "case_count": len(results),
        "cases": results,
    }


async def _main(model_uuid: str = "") -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from app.infra.database import (
        dispose_database,
        get_database,
        init_database_connection,
    )

    user_id = str(uuid.uuid4())
    thread_ids = [str(uuid.uuid4()) for _ in EXPRESSIONS]
    await init_database_connection()
    database = get_database()
    try:
        async with database.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO public.users (id, display_name, is_mock_user)
                    VALUES (:user_id, 'Bookshelf Read E2E', true)
                    """
                ),
                {"user_id": user_id},
            )
            for index, thread_id in enumerate(thread_ids, start=1):
                await session.execute(
                    text(
                        """
                        INSERT INTO public.conversations (thread_id, user_id, title)
                        VALUES (:thread_id, :user_id, :title)
                        """
                    ),
                    {
                        "thread_id": thread_id,
                        "user_id": user_id,
                        "title": f"Bookshelf Read E2E {index}",
                    },
                )
        result = await asyncio.to_thread(
            _run_http,
            user_id,
            thread_ids,
            model_uuid,
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
