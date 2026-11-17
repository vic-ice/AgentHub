from __future__ import annotations

import html as _html
import re


_TAG_RE = re.compile(r"<[^>]+>")
_NBSP_RE = re.compile(r"&nbsp;?|&#160;?", re.IGNORECASE)
_MULTISPACE_RE = re.compile(r"[ \t\u3000]+")
_MULTINEWLINE_RE = re.compile(r"\n{3,}")
_NOISE_TOKEN_RE = re.compile(
    r"\b(?:javascript|css|nbsp|amp|quot|undefined|null|true|false)\b",
    re.IGNORECASE,
)

_BOILERPLATE_LINE_RES = (
    re.compile(r"^\s*(?:copyright|\u00a9|\u7248\u6743\u6240\u6709|\u5907\u6848\u53f7|icp|\u6d59icp|\u4eacicp|\u6caaicp|\u6e56\u5357icp|\u516c\u5b89\u5907\u6848|\u6caa\u516c\u7f51\u5b89\u5907)", re.IGNORECASE),
    re.compile(r"^\s*(?:\u767b\u5f55|\u6ce8\u518c|\u514d\u8d39\u6ce8\u518c|\u7acb\u5373\u767b\u5f55|\u626b\u7801\u5173\u6ce8|\u70b9\u51fb\u5173\u6ce8|\u5173\u6ce8\u6211\u4eec|\u8ba2\u9605\u6211\u4eec|\u5173\u6ce8\u516c\u4f17\u53f7)"),
    re.compile(r"^\s*(?:\u4e0a\u4e00\u7bc7|\u4e0b\u4e00\u7bc7|\u76f8\u5173\u9605\u8bfb|\u76f8\u5173\u6587\u7ae0|\u5ef6\u4f38\u9605\u8bfb|\u63a8\u8350\u9605\u8bfb|\u70ed\u95e8\u6587\u7ae0|\u6700\u65b0\u6587\u7ae0|\u66f4\u591a\u5185\u5bb9|\u66f4\u591a\u6587\u7ae0|\u731c\u4f60\u559c\u6b22)"),
    re.compile(r"^\s*(?:\u590d\u5236\u94fe\u63a5|\u5206\u4eab|\u4e3e\u62a5|\u6536\u85cf|\u6253\u5370|\u8fd4\u56de\u9876\u90e8|\u56de\u5230\u9876\u90e8|\u9605\u8bfb\u539f\u6587|\u67e5\u770b\u539f\u6587|\u70b9\u51fb\u67e5\u770b|\u67e5\u770b\u8be6\u60c5)"),
    re.compile(r"^\s*(?:\u672c\u6587(?:\u7531|\u6765\u6e90)|\u8d23\u4efb\u7f16\u8f91|\u7f16\u8f91[:\uff1a]|\u4f5c\u8005[:\uff1a]|\u6765\u6e90[:\uff1a]|\u65f6\u95f4[:\uff1a]|\u53d1\u5e03\u65f6\u95f4[:\uff1a]|\u6d4f\u89c8[:\uff1a]|\u70b9\u51fb[:\uff1a])"),
    re.compile(r"^\s*[\-\u2013\u2014\u2022\u00b7|]\s*$"),
    re.compile(r"^\s*[0-9]+\s*$"),
    re.compile(r"^(?:function|var |window\.|document\.|if\s*\()", re.IGNORECASE),
)

_TITLE_SEP_RES = (
    re.compile(r"\s*[|\uff5c]\s*[^|\uff5c]{1,40}$"),
    re.compile(r"\s*[\-\u2013\u2014]\s*\u95ee\u7b54\s*[\-\u2013\u2014].*$", re.IGNORECASE),
    re.compile(
        r"\s*[\-\u2013\u2014]\s*(?:\u7535\u5b50\u5de5\u7a0b\u4e16\u754c|\u8bba\u575b|\u95ee\u7b54|\u535a\u5ba2|\u5b98\u7f51|\u9996\u9875)$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:\u3010|\uff3b|\(|\uff08)[^\uff09\uff3d\u3011]{1,20}(?:\u3011|\uff3d|\)|\uff09)\s*"),
)


def clean_text_for_context(text, *, max_chars: int = 4_000) -> str:
    """Strip HTML, entities, boilerplate and noise before model context.

    Applied at the retrieval boundary so neither the reviewer nor the
    report writer ever receives raw page chrome.
    """

    cleaned = _html.unescape(str(text or ""))
    cleaned = _NBSP_RE.sub(" ", cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    lines: list[str] = []
    for raw in cleaned.splitlines():
        line = _MULTISPACE_RE.sub(" ", raw).strip()
        if not line or _is_boilerplate_line(line):
            continue
        lines.append(line)
    joined = "\n".join(lines)
    joined = _MULTINEWLINE_RE.sub("\n\n", joined).strip()
    joined = _NOISE_TOKEN_RE.sub(" ", joined)
    joined = _MULTISPACE_RE.sub(" ", joined)
    return joined[:max_chars]


def clean_source_title(title) -> str:
    """Strip site suffixes and separator noise from source titles."""

    text = _MULTISPACE_RE.sub(
        " ",
        _html.unescape(str(title or "")),
    ).strip()
    for pattern in _TITLE_SEP_RES:
        text = pattern.sub("", text)
    text = text.strip(" |\uff5c\u00b7\u2022-\u2013\u2014:\uff1a")
    return _MULTISPACE_RE.sub(" ", text).strip()[:300]


def _is_boilerplate_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in _BOILERPLATE_LINE_RES)


__all__ = ["clean_source_title", "clean_text_for_context"]
