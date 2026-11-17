"""Pure normalization helpers for book and underlying-work identity."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def normalize_book_title(value: Any) -> str:
    """Normalize typography without interpreting the title's meaning."""

    text = str(value or "").strip()
    text = re.sub(r"[《》「」『』“”‘’\"']", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def normalize_book_work_title(value: Any) -> str:
    """Map explicit edition labels to the same underlying work identity."""

    text = normalize_book_title(value)
    # Catalog result titles can contain provider-page chrome.  This is source
    # normalization, not semantic title guessing: only known, exact suffixes
    # are removed so Shelf/reference exclusions compare the underlying work.
    text = re.sub(
        r"\s*(?:[-—|_]\s*)?(?:豆瓣)?读书\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*[（(【\[]\s*(?:修订|新版|增订|纪念|典藏|第\s*\d+\s*版|"
        r"revised|new\s+edition|\d+(?:st|nd|rd|th)\s+edition)[^）)】\]]*"
        r"[）)】\]]\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s+(?:修订版|新版|增订版|纪念版|典藏版|第\s*\d+\s*版)\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def canonical_book_source_url(value: Any) -> str:
    """Canonicalize a source URL for identity comparison only."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/") or "/"
    if (
        parsed.netloc.casefold().endswith("book.douban.com")
        and re.fullmatch(r"/subject/\d+", path)
    ):
        path += "/"
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), path, parsed.query, "")
    )
