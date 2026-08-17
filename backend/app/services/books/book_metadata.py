"""One-shot public book metadata lookup and application-owned cover cache.

This component performs external I/O only.  It never writes Book or Shelf rows;
ReadingService remains the sole owner of bookshelf persistence and decisions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from app.infra.config import BASE_DIR
from app.services.book_search import search_external_book_candidates
from app.services.recommendation_signals import normalize_recommendation_text


COVER_CACHE_DIR = BASE_DIR / "data" / "book_covers"
COVER_URL_PREFIX = "/api/v1/books/covers"
_DOUBAN_SUBJECT_RE = re.compile(r"^https://book\.douban\.com/subject/\d+/?$")
_IMAGE_HOST_SUFFIXES = (".doubanio.com", ".douban.com")
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class BookMetadata:
    title: str
    authors: list[str] = field(default_factory=list)
    source_url: str | None = None
    cover_url: str | None = None
    status: str = "not_found"
    error: str = ""


def _identity(value: str) -> str:
    value = normalize_recommendation_text(value).casefold()
    return re.sub(r"[\s《》〈〉「」『』:：·•\-—_()（）\[\]]+", "", value)


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip(" /\t\r\n")


def _canonical_subject_url(url: str) -> str | None:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "book.douban.com":
        return None
    match = re.search(r"/subject/(\d+)", parsed.path)
    return f"https://book.douban.com/subject/{match.group(1)}/" if match else None


def _valid_subject_url(url: str) -> bool:
    return _canonical_subject_url(url) == str(url or "").strip()


def _choose_candidate(title: str, candidates: list[dict]) -> dict | None:
    wanted = _identity(title)
    valid: list[dict] = []
    for item in candidates:
        canonical_url = _canonical_subject_url(str(item.get("source_url") or ""))
        if canonical_url:
            valid.append({**item, "source_url": canonical_url})
    exact = [item for item in valid if _identity(str(item.get("title") or "")) == wanted]
    if exact:
        return exact[0]
    contained = [
        item
        for item in valid
        if wanted and wanted in _identity(str(item.get("title") or ""))
    ]
    return contained[0] if contained else None


def _extract_meta(html: str, *, property_name: str = "", name: str = "") -> str:
    marker = property_name or name
    attribute = "property" if property_name else "name"
    patterns = (
        rf'<meta[^>]+{attribute}=["\']{re.escape(marker)}["\'][^>]+content=["\']([^"\']+)',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+{attribute}=["\']{re.escape(marker)}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return unescape(match.group(1)).strip()
    return ""


def _extract_authors(html: str) -> list[str]:
    # Current and older Douban subject pages both expose the bibliographic
    # author row inside #info.  Parsing is deliberately source-format-only;
    # it is not semantic interpretation of the user's message.
    row = re.search(
        r'<span[^>]*class=["\']pl["\'][^>]*>\s*作者\s*</span>\s*:?(.*?)(?:<br\s*/?>)',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    values: list[str] = []
    if row:
        links = re.findall(r"<a[^>]*>(.*?)</a>", row.group(1), flags=re.DOTALL | re.IGNORECASE)
        raw_values = links or re.split(r"[/,，]", _clean_text(row.group(1)))
        values.extend(_clean_text(item) for item in raw_values)

    if not values:
        # Some editions publish schema.org JSON-LD instead of the classic row.
        for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            try:
                data = json.loads(unescape(block))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            author_data = data.get("author") if isinstance(data, dict) else None
            author_items = author_data if isinstance(author_data, list) else [author_data]
            for item in author_items:
                if isinstance(item, dict):
                    values.append(str(item.get("name") or ""))
                elif item:
                    values.append(str(item))

    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _clean_text(value)
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            cleaned.append(item[:128])
    return cleaned[:10]


def parse_douban_subject(html: str, *, fallback_title: str, source_url: str) -> BookMetadata:
    cover_source = _extract_meta(html, property_name="og:image")
    title = _extract_meta(html, property_name="og:title") or fallback_title
    title = re.sub(r"\s*\(豆瓣\)\s*$", "", title, flags=re.IGNORECASE).strip()
    return BookMetadata(
        title=title or fallback_title,
        authors=_extract_authors(html),
        source_url=source_url,
        cover_url=cover_source or None,
        status="found",
    )


def cached_cover_path(filename: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{64}\.(?:jpg|png|webp|gif)", filename):
        return None
    path = (COVER_CACHE_DIR / filename).resolve()
    root = COVER_CACHE_DIR.resolve()
    return path if path.parent == root and path.is_file() else None


async def _download_cover(session: aiohttp.ClientSession, source_url: str) -> str | None:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host.endswith(suffix) for suffix in _IMAGE_HOST_SUFFIXES):
        return None
    async with session.get(
        source_url,
        headers={
            "Referer": "https://book.douban.com/",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
        },
    ) as response:
        if response.status != 200:
            return None
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        extension = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }.get(content_type)
        if extension is None:
            return None
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > _MAX_IMAGE_BYTES:
                return None
            chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        return None
    digest = hashlib.sha256(payload).hexdigest()
    filename = f"{digest}.{extension}"
    COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    destination = COVER_CACHE_DIR / filename
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(destination)
    return f"{COVER_URL_PREFIX}/{filename}"


async def resolve_book_metadata(title: str, *, source_url: str | None = None) -> BookMetadata:
    """Resolve one title and turn any remote cover into a durable local URL."""
    clean_title = normalize_recommendation_text(title)
    if not clean_title:
        return BookMetadata(title="", status="not_found")
    try:
        subject_url = source_url if _valid_subject_url(source_url or "") else None
        if subject_url is None:
            search = await asyncio.wait_for(
                search_external_book_candidates(clean_title, limit=10),
                timeout=10,
            )
            candidate = _choose_candidate(clean_title, search.candidates)
            if candidate is None:
                return BookMetadata(title=clean_title, status="not_found")
            subject_url = str(candidate["source_url"])

        timeout = aiohttp.ClientTimeout(total=10, connect=4)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AgentHubBookMetadata/1.0)",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(subject_url) as response:
                if response.status != 200:
                    return BookMetadata(
                        title=clean_title,
                        source_url=subject_url,
                        status="unavailable",
                        error=f"subject_http_{response.status}",
                    )
                html = await response.text(errors="replace")
            metadata = parse_douban_subject(
                html,
                fallback_title=clean_title,
                source_url=subject_url,
            )
            local_cover = (
                await _download_cover(session, metadata.cover_url)
                if metadata.cover_url
                else None
            )
            return BookMetadata(
                title=metadata.title,
                authors=metadata.authors,
                source_url=metadata.source_url,
                cover_url=local_cover,
                status="found" if metadata.authors or local_cover else "not_found",
            )
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError, ValueError) as exc:
        return BookMetadata(
            title=clean_title,
            source_url=source_url,
            status="unavailable",
            error=(str(exc) or exc.__class__.__name__)[:300],
        )
