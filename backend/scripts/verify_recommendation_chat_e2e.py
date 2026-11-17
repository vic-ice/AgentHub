"""Live Chat E2E for the single RecommendationService owner."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
BASE = "http://127.0.0.1:8080/api/v1"
_CANONICAL_BOOK_URL_RE = re.compile(
    r"https://book\.douban\.com/subject/\d+/?"
)


def _markdown_table_rows(answer: str) -> tuple[list[str], list[list[str]]]:
    lines = [line.strip() for line in str(answer or "").splitlines()]
    for index in range(len(lines) - 1):
        if not lines[index].startswith("|"):
            continue
        if not re.fullmatch(
            r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?",
            lines[index + 1],
        ):
            continue
        header = [cell.strip() for cell in lines[index].strip("|").split("|")]
        rows: list[list[str]] = []
        for line in lines[index + 2 :]:
            if not line.startswith("|"):
                break
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) == len(header):
                rows.append(cells)
        return header, rows
    return [], []


def _column_index(header: list[str], *aliases: str) -> int:
    normalized = [
        re.sub(r"[\s_*`]+", "", cell).casefold()
        for cell in header
    ]
    for alias in aliases:
        key = re.sub(r"[\s_*`]+", "", alias).casefold()
        if key in normalized:
            return normalized.index(key)
    return -1


def _http_flow(user_id: str, thread_id: str, model_uuid: str) -> dict:
    message = (
        "2026年有啥值得推荐看的书吗，比如《非暴力沟通》、"
        "《小狗钱钱》这种类型的？请推荐几本我书架之外的新书。"
        "请用Markdown表格，列为书名、作者、适合人群、推荐理由、局限。"
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
        answer_message = next(
            item
            for item in reversed(history.json().get("messages", []))
            if item.get("type") == "ai" and item.get("request_id") == request_id
        )
        answer = str(answer_message.get("content") or "")
        answer_custom_data = dict(answer_message.get("custom_data") or {})
        publication_mode = str(
            answer_custom_data.get("publication_mode") or ""
        )
        assert "安全控制轮数上限" not in answer, answer
        assert "未继续执行" not in answer, answer
        assert len(answer.strip()) >= 20, answer
        from app.services.publication_safety import (
            contains_internal_reasoning,
            table_contract_satisfied,
        )

        assert table_contract_satisfied(answer, request=message), answer
        assert not contains_internal_reasoning(answer), answer
        header, rows = _markdown_table_rows(answer)
        title_index = _column_index(header, "书名", "图书", "书籍")
        author_index = _column_index(header, "作者")
        audience_index = _column_index(header, "适合人群", "适合读者")
        reason_index = _column_index(
            header,
            "推荐理由",
            "适配理由",
            "为什么推荐",
        )
        limitation_index = _column_index(
            header,
            "局限",
            "主要局限",
            "注意事项",
            "取舍",
        )
        assert min(
            title_index,
            author_index,
            audience_index,
            reason_index,
            limitation_index,
        ) >= 0, header
        assert len(rows) >= 3, rows
        canonical_urls = _CANONICAL_BOOK_URL_RE.findall(answer)
        fallback_without_evidence = not canonical_urls
        if fallback_without_evidence:
            assert not re.search(r"https?://", answer, re.IGNORECASE), answer
            assert any(
                marker in answer
                for marker in ("核验", "联网", "外部", "实时")
            ), answer
        else:
            assert len(set(canonical_urls)) >= 3, (canonical_urls, answer)
        for row in rows:
            assert all(cell.strip() for cell in row), row
            assert not any(
                marker in row[reason_index]
                for marker in ("作者简介", "出生于", "毕业于", "任职于")
            ), row
            assert "非暴力沟通" not in row[title_index], row
            assert "小狗钱钱" not in row[title_index], row

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
        book_step = next(
            step
            for step in trace.json().get("steps", [])
            if step.get("message_type") == "tool"
            and step.get("tool_name") == "book_search_v1"
        )
        raw_book_output = str(book_step.get("tool_output") or "")
        try:
            book_output = json.loads(raw_book_output)
        except json.JSONDecodeError:
            book_output = {}
        summary_count_match = re.search(
            r"已筛选\s*(\d+)\s*本候选",
            raw_book_output,
        )
        candidate_count = int(
            book_output.get("candidate_count")
            or (summary_count_match.group(1) if summary_count_match else 0)
        )
        if fallback_without_evidence:
            assert candidate_count == 0, raw_book_output
        else:
            assert candidate_count >= 3, raw_book_output
        book_search_args = book_step.get("tool_args") or {}
        assert (book_search_args.get("themes") or []) == [], book_search_args
        assert "Markdown" not in str(book_search_args.get("query") or "")
        assert book_search_args.get("reference_titles") == [
            "非暴力沟通",
            "小狗钱钱",
        ], book_search_args
        item_titles = [
            str(item.get("title") or "")
            for item in book_output.get("items", [])
            if isinstance(item, dict)
        ]
        return {
            "status": "passed",
            "request_id": request_id,
            "publication_mode": (
                publication_mode
                or (
                    "model_knowledge_fallback"
                    if fallback_without_evidence
                    else "model_synthesis"
                )
            ),
            "operations": operations,
            "answer_chars": len(answer),
            "answer_preview": answer[:500],
            "book_search_args": book_search_args,
            "candidate_count": candidate_count,
            "table_row_count": len(rows),
            "canonical_book_urls": list(dict.fromkeys(canonical_urls)),
            "item_titles": item_titles,
            "book_metadata": book_output.get("metadata") or {},
            "book_output_keys": sorted(book_output),
            "raw_book_output_preview": raw_book_output[:1200],
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
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
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
