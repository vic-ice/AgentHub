"""Behavioral acceptance set for the memory intent + research pipeline.

Usage:
  python scripts/verify_behavior_acceptance.py             # offline + live memory
  python scripts/verify_behavior_acceptance.py --research  # also research gateway smoke

The live memory section needs the backend on 8081; it runs against a
randomly created isolated user/thread and deletes that user afterwards, so
real user threads are never polluted. The --research section needs the
database and external network.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.memory.intent import MemoryIntentClassifier
from app.services.research.search_policy import build_research_search_request


BASE = "http://127.0.0.1:8081/api/v1"

INTENT_CASES: list[tuple[str, str]] = [
    ("我是谁", "current"),
    ("我叫什么", "current"),
    ("我之前叫什么", "previous"),
    ("之前我叫什么来着", "previous"),
    ("我最早的称呼呢", "earliest"),
    ("我改过几次名字", "timeline"),
    ("我什么时候改的名字", "timeline"),
    ("什么时候说的我叫冰露", "when"),
    ("我叫冰露", "write"),
    ("我现在不叫黄仁，我叫冰露", "correct"),
    ("我不叫黄仁", "correct"),
    ("我喜欢科幻小说", "write"),
    ("我有一只猫", "write"),
    ("我还有一只狗", "write"),
    ("忘了我叫冰露", "forget"),
    ("帮我查天气", None),
    ("你好", None),
]

ISOLATED_MEMORY_CASES: list[tuple[str, str]] = [
    ("我是谁", "黄仁"),
    ("我之前叫什么", "冰露"),
    ("什么时候说的我叫冰露", "冰露"),
]


def _classify_offline() -> int:
    classifier = MemoryIntentClassifier()
    topic = classifier.classify("我叫冰露")
    failures = 0
    for text, expected in INTENT_CASES:
        intent = classifier.classify(text, previous_topic=topic)
        actual = intent.kind if intent is not None else None
        if actual != expected:
            failures += 1
            print(f"[FAIL] intent {text} -> {actual} (expected {expected})")
    print(f"intent offline: {len(INTENT_CASES) - failures}/{len(INTENT_CASES)} ok")
    return failures


def _research_policy() -> int:
    request = build_research_search_request(
        "深度搜索类似《失控》《写给管理者的睡前故事》风格的书籍推荐"
    )
    ok = (
        "book" in request.requirements
        and "book.douban.com" in request.include_domains
        and "https://book.douban.com/subject/" in request.include_url_prefixes
    )
    print(f"research policy book->douban: {'ok' if ok else 'FAIL'}")
    return 0 if ok else 1


def _ask(
    client: httpx.Client,
    question: str,
    model_uuid: str,
    *,
    user_id: str,
    thread_id: str,
) -> str:
    payload = {
        "content": question,
        "thread_id": thread_id,
        "user_id": user_id,
        "request_id": str(uuid.uuid4()),
        "model_uuid": model_uuid,
        "thinking_mode": False,
    }
    with client.stream("POST", f"{BASE}/chat/stream", json=payload, timeout=20) as response:
        answer = ""
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "answer.completed":
                content = data.get("content", {})
                if isinstance(content, dict):
                    answer_obj = content.get("answer") or {}
                    if isinstance(answer_obj, dict):
                        answer = str(answer_obj.get("content", ""))
        return answer


def _isolated_live_memory() -> int:
    """Run live memory checks on a fresh, disposable user and clean up."""

    import asyncio
    import sys as _sys

    if _sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(_isolated_live_memory_async())


async def _isolated_live_memory_async() -> int:
    from sqlalchemy import text

    from app.infra.database import (
        dispose_database,
        get_database,
        init_database_connection,
    )

    user_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    await init_database_connection()
    failures = 0
    try:
        database = get_database()
        async with database.session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO public.users (id, display_name, is_mock_user)
                    VALUES (:user_id, 'Behavior Acceptance Isolated', true)
                    """
                ),
                {"user_id": user_id},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO public.conversations (thread_id, user_id, title)
                    VALUES (:thread_id, :user_id, 'Behavior Acceptance Isolated')
                    """
                ),
                {"thread_id": thread_id, "user_id": user_id},
            )
        try:
            with httpx.Client(timeout=25) as client:
                models = client.get(f"{BASE}/models").json()
                llm = next(
                    (
                        m
                        for m in models.get("models", [])
                        if m.get("model_type") == "llm"
                        and m.get("is_active")
                    ),
                    None,
                )
                if llm is None:
                    print("[FAIL] live memory: no active llm")
                    return 1
                model_uuid = llm.get("model_uuid") or llm.get("id")
                for name in ("冰露", "黄仁"):
                    response = client.post(
                        f"{BASE}/memory/edit",
                        json={
                            "user_id": user_id,
                            "predicate": "name",
                            "value": {"name": name},
                            "evidence_quote": (
                                f"我在记忆面板把名字改为{name}"
                            ),
                        },
                    )
                    if response.status_code not in (200, 201):
                        print(
                            f"[FAIL] seed memory {name}: "
                            f"HTTP {response.status_code} {response.text}"
                        )
                        return 1
                for question, expected in ISOLATED_MEMORY_CASES:
                    answer = _ask(
                        client,
                        question,
                        model_uuid,
                        user_id=user_id,
                        thread_id=thread_id,
                    )
                    ok = expected in answer
                    if not ok:
                        failures += 1
                        print(
                            f"[FAIL] live {question} -> {answer!r} "
                            f"(expected contains {expected!r})"
                        )
                    else:
                        print(f"[ok] live {question} -> {answer!r}")
        finally:
            async with database.session() as session:
                await session.execute(
                    text("DELETE FROM public.users WHERE id = :user_id"),
                    {"user_id": user_id},
                )
    except Exception as exc:
        print(f"[FAIL] isolated live memory unreachable: {exc}")
        return 1
    finally:
        await dispose_database()
    print(
        f"isolated live memory: "
        f"{len(ISOLATED_MEMORY_CASES) - failures}/"
        f"{len(ISOLATED_MEMORY_CASES)} ok"
    )
    return failures


def _research_gateway() -> int:
    import asyncio
    import sys as _sys

    if _sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    from app.infra.database import dispose_database, init_database
    from app.services.external_search import SearchRequest, get_search_gateway

    async def run() -> int:
        await init_database()
        try:
            result = await get_search_gateway().search(
                SearchRequest(
                    query="类似《失控》风格的书籍推荐",
                    max_results=5,
                    detail="deep",
                    include_domains=["book.douban.com", "douban.com"],
                    zone="cn",
                    language="zh",
                )
            )
            ok = result.outcome == "found" and any(
                "douban.com" in (h.url or "") for h in (result.hits or [])
            )
            print(f"research gateway douban: {'ok' if ok else 'FAIL'} ({result.outcome})")
            return 0 if ok else 1
        finally:
            await dispose_database()

    return asyncio.run(run())


def main() -> int:
    failures = _classify_offline()
    failures += _research_policy()
    failures += _isolated_live_memory()
    if "--research" in sys.argv:
        failures += _research_gateway()
    print(f"behavior acceptance failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
