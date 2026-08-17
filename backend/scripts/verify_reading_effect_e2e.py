"""Live black-box acceptance for Chat -> Shelf + unified Memory effects.

Uses the real running HTTP service and configured Controller model. Every run
creates an isolated mock user/thread and deletes only that user afterwards.
No response text is fixture-matched: acceptance reads authoritative Shelf and
current Memory state after varied natural-language messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
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

CASES = (
    (
        "昨晚把《追忆似水年华》最后一页翻完了，读下来很喜欢。",
        {
            "追忆似水年华": ("read", "liked"),
        },
    ),
    (
        "《黑暗的左手》读完了，不太合我胃口；接下来打算看《莫失莫忘》。",
        {
            "黑暗的左手": ("read", "disliked"),
            "莫失莫忘": ("want_to_read", None),
        },
    ),
    (
        "《献给阿尔吉侬的花束》我今天已经开读；顺手把《长日将尽》加入待读。",
        {
            "献给阿尔吉侬的花束": ("reading", None),
            "长日将尽": ("want_to_read", None),
        },
    ),
)


def _normalize_title(value: Any) -> str:
    text_value = str(value or "").strip().casefold()
    text_value = re.sub(r"[《》「」『』“”‘’\"']", "", text_value)
    return re.sub(r"\s+", " ", text_value)


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


def _send_chat(
    client: httpx.Client,
    *,
    user_id: str,
    thread_id: str,
    model_uuid: str,
    message: str,
) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    payload = {
        "content": message,
        "thread_id": thread_id,
        "user_id": user_id,
        "request_id": request_id,
        "model_uuid": model_uuid,
        "thinking_mode": False,
    }
    completed: dict[str, Any] | None = None
    failure = ""
    with client.stream(
        "POST",
        f"{BASE}/chat/stream",
        json=payload,
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
                completed = event
            elif event_type == "turn.failed":
                failure = str(event.get("content") or "")
    if completed is None:
        raise AssertionError(
            f"chat did not complete for {message!r}; failure={failure!r}"
        )
    return {
        "request_id": request_id,
        "message": message,
    }


def _shelf(client: httpx.Client, user_id: str) -> dict[str, dict[str, Any]]:
    response = client.get(f"{BASE}/books/shelf/{user_id}")
    response.raise_for_status()
    return {
        _normalize_title(item.get("title")): item
        for item in response.json().get("items", [])
    }


def _memory(client: httpx.Client, user_id: str) -> dict[str, dict[str, Any]]:
    response = client.get(f"{BASE}/memory/{user_id}/current")
    response.raise_for_status()
    state: dict[str, dict[str, Any]] = {}
    for fact in response.json().get("facts", []):
        value = fact.get("value") if isinstance(fact.get("value"), dict) else {}
        title = _normalize_title(
            value.get("book_title") or value.get("entity")
        )
        if not title:
            continue
        dimensions = state.setdefault(title, {})
        predicate = str(fact.get("predicate") or value.get("predicate") or "")
        if predicate == "reading_status":
            dimensions["reading_status"] = value.get("reading_status")
        elif predicate == "evaluation":
            dimensions["evaluation"] = value.get("evaluation")
    return state


def _assert_effects(
    actual: dict[str, dict[str, Any]],
    expected: dict[str, tuple[str, str | None]],
    *,
    source: str,
) -> None:
    for title, (status, evaluation) in expected.items():
        key = _normalize_title(title)
        if key not in actual:
            raise AssertionError(f"{source}: missing book {title!r}: {actual}")
        item = actual[key]
        if item.get("reading_status") != status:
            raise AssertionError(
                f"{source}: {title!r} status={item.get('reading_status')!r}, "
                f"expected {status!r}"
            )
        if evaluation is not None and item.get("evaluation") != evaluation:
            raise AssertionError(
                f"{source}: {title!r} evaluation={item.get('evaluation')!r}, "
                f"expected {evaluation!r}"
            )


def _run_http(
    user_id: str,
    thread_id: str,
    selected_model_uuid: str = "",
) -> dict[str, Any]:
    case_results: list[dict[str, Any]] = []
    all_expected: dict[str, tuple[str, str | None]] = {}
    with httpx.Client(timeout=180) as client:
        model_uuid = selected_model_uuid or _default_model(client)
        for message, expected in CASES:
            case_results.append(
                _send_chat(
                    client,
                    user_id=user_id,
                    thread_id=thread_id,
                    model_uuid=model_uuid,
                    message=message,
                )
            )
            all_expected.update(expected)
            _assert_effects(
                _shelf(client, user_id),
                expected,
                source="Shelf",
            )

        direct_expected = {
            "禅与摩托车维修艺术": ("dropped", "disliked"),
        }
        direct_response = client.post(
            f"{BASE}/books/shelf",
            json={
                "user_id": user_id,
                "title": "禅与摩托车维修艺术",
                "reading_status": "want_to_read",
                "evaluation": "neutral",
                "note": "frontend-e2e",
            },
        )
        direct_response.raise_for_status()
        patch_response = client.patch(
            f"{BASE}/books/shelf/{direct_response.json()['id']}",
            json={
                "reading_status": "dropped",
                "evaluation": "disliked",
                "note": "frontend-patch-e2e",
            },
        )
        patch_response.raise_for_status()
        all_expected.update(direct_expected)
        case_results.append(
            {"kind": "bookshelf_api_post_patch", "title": "禅与摩托车维修艺术"}
        )

        shelf = _shelf(client, user_id)
        memory = _memory(client, user_id)
        _assert_effects(shelf, all_expected, source="Shelf")
        _assert_effects(memory, all_expected, source="Memory")
        return {
            "status": "passed",
            "model_uuid": model_uuid,
            "case_count": len(case_results),
            "cases": case_results,
            "shelf": shelf,
            "memory": memory,
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
    thread_id = str(uuid.uuid4())
    await init_database_connection()
    database = get_database()
    try:
        async with database.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO public.users (id, display_name, is_mock_user)
                    VALUES (:user_id, 'Reading Effect E2E', true)
                    """
                ),
                {"user_id": user_id},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO public.conversations (thread_id, user_id, title)
                    VALUES (:thread_id, :user_id, 'Reading Effect E2E')
                    """
                ),
                {"thread_id": thread_id, "user_id": user_id},
            )
        result = await asyncio.to_thread(
            _run_http,
            user_id,
            thread_id,
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
