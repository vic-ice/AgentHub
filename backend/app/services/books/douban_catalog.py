"""Read-only Douban catalog autocomplete provider.

This adapter executes a typed catalog query and normalizes source fields.  It
does not interpret user language, select recommendation strategy, rank books,
or write Shelf/Memory state.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import aiohttp

from app.services.book_search_contracts import (
    BookCandidateSearchResult,
    get_book_search_hint,
)


_ENDPOINT = "https://book.douban.com/j/subject_suggest"
_SUBJECT_URL_RE = re.compile(r"^https://book\.douban\.com/subject/(\d+)/?$")


class DoubanCatalogProvider:
    """Fetch exact book records from Douban's public suggestion endpoint."""

    name = "douban_catalog"

    async def search(self, query: str, *, limit: int) -> BookCandidateSearchResult:
        started_at = time.perf_counter()
        clean_query = " ".join(str(query or "").split()).strip()[:120]
        if not clean_query:
            return BookCandidateSearchResult(
                query="",
                provider_query="",
                status="empty_result",
                source=self.name,
                next_action_hint=get_book_search_hint("empty_result"),
            )
        timeout = aiohttp.ClientTimeout(total=8, connect=4)
        selected_query = clean_query
        candidates: list[dict] = []
        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; AgentHubCatalog/1.0)",
                    "Referer": "https://book.douban.com/",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                },
            ) as session:
                seen_subjects: set[str] = set()
                selected_queries: list[str] = []
                for query_group in _catalog_query_groups(clean_query):
                    for provider_query in query_group:
                        async with session.get(
                            _ENDPOINT,
                            params={"q": provider_query},
                        ) as response:
                            if response.status != 200:
                                if candidates:
                                    break
                                return _failed(
                                    query=clean_query,
                                    status="hard_error",
                                    error=f"douban_catalog_http_{response.status}",
                                    started_at=started_at,
                                )
                            payload = await response.json(content_type=None)
                        group_candidates = normalize_douban_suggestions(
                            payload,
                            limit=limit,
                        )
                        if group_candidates:
                            selected_queries.append(provider_query)
                        selected_query = provider_query
                        for candidate in group_candidates:
                            subject_id = _subject_id(candidate.get("url"))
                            if not subject_id or subject_id in seen_subjects:
                                continue
                            seen_subjects.add(subject_id)
                            candidates.append(candidate)
                            if len(candidates) >= max(1, min(int(limit), 10)):
                                break
                        if len(candidates) >= max(1, min(int(limit), 10)):
                            break
                    if len(candidates) >= max(1, min(int(limit), 10)):
                        break
                if selected_queries:
                    selected_query = " | ".join(selected_queries)[:300]
        except asyncio.TimeoutError:
            if not candidates:
                return _failed(
                    query=clean_query,
                    status="timeout",
                    error="douban_catalog_timeout",
                    started_at=started_at,
                )
        except (aiohttp.ClientError, OSError, ValueError, TypeError) as exc:
            if not candidates:
                return _failed(
                    query=clean_query,
                    status="hard_error",
                    error=(str(exc) or exc.__class__.__name__)[:300],
                    started_at=started_at,
                )

        status = "ok" if candidates else "empty_result"
        return BookCandidateSearchResult(
            query=clean_query,
            provider_query=selected_query,
            status=status,
            candidates=candidates,
            source=self.name,
            next_action_hint=get_book_search_hint(status),
            duration_ms=int((time.perf_counter() - started_at) * 1000),
        )


def catalog_query_backoffs(query: str) -> list[str]:
    """Bounded prefix backoff for a prefix-oriented catalog endpoint.

    This is lexical provider adaptation only. It has no vocabulary of book
    topics and never examines the original user message.
    """

    return list(
        dict.fromkeys(
            value
            for group in _catalog_query_groups(query)
            for value in group
        )
    )


def _catalog_query_groups(query: str) -> list[list[str]]:
    """Return bounded lexical alternatives without understanding the topic."""

    clean = " ".join(str(query or "").split()).strip()
    if not clean:
        return []
    components = [
        item.strip()
        for item in re.split(r"[\s,，、;；/|]+", clean)
        if len(item.strip()) >= 2
    ][:3]
    components = sorted(
        dict.fromkeys(components),
        key=lambda value: (len(value), components.index(value)),
    )
    groups = [[clean]]
    if len(components) > 1:
        groups.extend(_prefix_backoffs(component) for component in components)
    elif components and components[0] == clean:
        groups = [_prefix_backoffs(clean)]
    return groups


def _prefix_backoffs(value: str) -> list[str]:
    if not re.fullmatch(r"[\u3400-\u9fff]+", value):
        return [value]
    length = len(value)
    lengths = [length]
    audience_stripped = ""
    for marker in ("成年人", "小学生", "青少年", "成人", "职场", "幼儿", "少儿"):
        if value.startswith(marker) and len(value) - len(marker) >= 2:
            audience_stripped = value[len(marker):]
            break
    if length > 2:
        lengths.extend([length - 1, length - 2, 2])
    return list(
        dict.fromkeys(
            item
            for item in (
                [value, audience_stripped]
                + [value[:size] for size in lengths[1:] if size >= 2]
            )
            if item
        )
    )


def _subject_id(value: Any) -> str:
    match = _SUBJECT_URL_RE.fullmatch(str(value or "").strip())
    return match.group(1) if match is not None else ""


def normalize_douban_suggestions(payload: Any, *, limit: int) -> list[dict]:
    """Allowlist bibliographic fields and exact subject URLs only."""

    rows = payload if isinstance(payload, list) else []
    candidates: list[dict] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict) or str(item.get("type") or "b") != "b":
            continue
        url = str(item.get("url") or "").strip()
        match = _SUBJECT_URL_RE.fullmatch(url)
        title = " ".join(str(item.get("title") or "").split()).strip()
        if match is None or not title or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        author = " ".join(str(item.get("author_name") or "").split()).strip()
        candidates.append(
            {
                "title": title[:300],
                "url": f"https://book.douban.com/subject/{match.group(1)}/",
                "author_name": author[:300],
                "pic": str(item.get("pic") or "").strip()[:1000],
                "published_date": "",
                "book_published_date": str(item.get("year") or "").strip()[:64],
                "snippet": f"作者：{author}" if author else "",
            }
        )
        if len(candidates) >= max(1, min(int(limit), 10)):
            break
    return candidates


def _failed(
    *,
    query: str,
    status: str,
    error: str,
    started_at: float,
) -> BookCandidateSearchResult:
    return BookCandidateSearchResult(
        query=query,
        provider_query=query,
        status=status,
        source="douban_catalog",
        next_action_hint=get_book_search_hint(status),
        error=error,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
    )


__all__ = [
    "DoubanCatalogProvider",
    "catalog_query_backoffs",
    "normalize_douban_suggestions",
]
