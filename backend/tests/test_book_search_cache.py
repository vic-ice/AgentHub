from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.book import Book
from app.services.book_search import search_cached_books


def _book(
    title: str,
    source_url: str,
    *,
    summary: str = "沟通方法",
    age_minutes: int = 0,
) -> Book:
    seen_at = datetime(2026, 8, 26, tzinfo=timezone.utc) - timedelta(
        minutes=age_minutes
    )
    return Book(
        title=title,
        source_name="douban",
        source_url=source_url,
        summary=summary,
        raw_data={},
        last_seen_at=seen_at,
        updated_at=seen_at,
    )


class SearchCachedBooksTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_dirty_urls_and_deduplicates_before_limit(self) -> None:
        books = [
            _book("沟通方法书评", "https://book.douban.com/review/1001"),
            _book(
                "沟通方法笔记",
                "https://book.douban.com/people/reader/annotation/1002/",
                age_minutes=1,
            ),
            _book(
                "沟通的艺术（新版）",
                "https://book.douban.com/subject/2002/",
                age_minutes=2,
            ),
            _book(
                "沟通的艺术",
                "https://book.douban.com/subject/2001/",
                age_minutes=3,
            ),
            _book(
                "关键对话",
                "https://book.douban.com/subject/3001/",
                age_minutes=4,
            ),
        ]
        scalar_result = SimpleNamespace(all=lambda: books)
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(scalars=lambda: scalar_result)
            )
        )

        results = await search_cached_books(db, query="沟通", limit=2)

        self.assertEqual(
            [book.title for book in results],
            ["沟通的艺术（新版）", "关键对话"],
        )
        self.assertTrue(all("/review/" not in book.source_url for book in results))
        self.assertTrue(
            all("/annotation/" not in book.source_url for book in results)
        )


if __name__ == "__main__":
    unittest.main()
