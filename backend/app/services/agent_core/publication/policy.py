from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit, urlunsplit

from app.services.publication_safety import contains_internal_reasoning
from app.services.agent_core.publication.contracts import (
    ReceiptEvidenceBundle,
)


_SIDE_EFFECT_SUCCESS_RE = re.compile(
    r"(?:已|已经|成功)(?:记录|保存|更新|修改|删除|忘记|创建|取消)"
    r"|(?:saved|updated|deleted|created|cancelled|canceled)\s+"
    r"(?:your|the|this)",
    re.IGNORECASE,
)
_TECHNICAL_LEAK_RE = re.compile(
    r"dependency did not complete"
    r"|controller-action-"
    r"|(?:^|\W)action-[0-9a-f-]{8,}"
    r"|<\s*tool_call\b"
    r"|<\s*function="
    r"|</?\s*think\s*>"
    r"|trusted_receipts"
    r"|(?:通过|根据).{0,12}回执(?:校验|验证)?"
    r"|(?:错误码|请求\s*ID)\s*[:：]"
    r"|(?:^|\n)\s*Traceback \(most recent call last\)",
    re.IGNORECASE,
)
_FUNCTION_CALL_ONLY_RE = re.compile(
    r"^\s*(?:[-*]\s*)?[A-Za-z_][A-Za-z0-9_.-]*\s*\(\s*"
    r"(?:[^()\n]|\([^()\n]*\))*\s*\)\s*[.;；。]?\s*$",
    re.DOTALL,
)
_JSON_TOOL_CALL_ONLY_RE = re.compile(
    r'^\s*\{\s*"(?:name|tool|function)"\s*:\s*"[^"]+"\s*,'
    r'.*"(?:arguments|args|parameters)"\s*:\s*(?:\{|\[).*(?:\}|\])\s*\}\s*$',
    re.IGNORECASE | re.DOTALL,
)
_URL_RE = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)
_MARKDOWN_URL_RE = re.compile(
    r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
    re.IGNORECASE,
)


def validate_direct_text(text: str) -> str:
    cleaned = _clean(text)
    _reject_technical_text(cleaned)
    if _SIDE_EFFECT_SUCCESS_RE.search(cleaned):
        raise ValueError(
            "side-effect success claims require a deterministic receipt"
        )
    return cleaned


def validate_synthesis(
    text: str,
    *,
    evidence: Sequence[ReceiptEvidenceBundle],
) -> str:
    cleaned = validate_direct_text(text)
    if not evidence:
        raise ValueError("model synthesis requires receipt evidence")
    # Side-effect receipts (e.g. a memory write) may legitimately precede a
    # plain summary in multi-round turns; only URL fabrication and unsafe text
    # are rejected here, not the presence of a write receipt.
    allowed_urls = _evidence_urls(evidence)
    cleaned = _sanitize_synthesis_urls(cleaned, allowed_urls=allowed_urls)
    if (
        _has_admitted_web_search_evidence(evidence)
        and allowed_urls
        and not _contains_admitted_url(cleaned, allowed_urls=allowed_urls)
    ):
        raise ValueError(
            "web synthesis must include at least one admitted source citation"
        )
    admitted_book_titles = _evidence_book_titles(evidence)
    if admitted_book_titles and not any(
        alias in _normalized_title(cleaned)
        for title in admitted_book_titles
        for alias in _normalized_title_aliases(title)
    ):
        raise ValueError(
            "book synthesis must cite at least one admitted candidate title"
        )
    # A plain chat answer may legitimately omit source links even when the
    # model searched (e.g. "26 degrees and sunny"): only reject fabricated
    # URLs, not the absence of a citation.
    return cleaned


def validate_model_knowledge_text(text: str) -> str:
    """Validate an answer produced without usable external evidence.

    Model knowledge remains useful when retrieval is unavailable, but it must
    not manufacture the appearance of live verification. Preserve Markdown
    prose while removing every unadmitted URL.
    """

    cleaned = validate_direct_text(text)
    cleaned = _sanitize_synthesis_urls(cleaned, allowed_urls=set())
    return validate_direct_text(cleaned)


def _has_admitted_web_search_evidence(
    evidence: Sequence[ReceiptEvidenceBundle],
) -> bool:
    for bundle in evidence:
        for action in bundle.receipt.actions:
            if action.operation != "web_search_v2" or action.status != "completed":
                continue
            output = action.output if isinstance(action.output, dict) else {}
            if any(
                isinstance(item, dict) and str(item.get("url") or "").strip()
                for item in (output.get("sources") or [])
            ):
                return True
    return False


def _contains_admitted_url(text: str, *, allowed_urls: set[str]) -> bool:
    allowed = {
        identity
        for url in allowed_urls
        if (identity := _public_url_identity(url))
    }
    emitted = {
        identity
        for match in _URL_RE.finditer(str(text or ""))
        if (identity := _public_url_identity(match.group(0)))
    }
    return bool(allowed.intersection(emitted))


def _sanitize_synthesis_urls(text: str, *, allowed_urls: set[str]) -> str:
    """Keep admitted citations without sacrificing a safe model answer.

    Models commonly normalize a trailing slash or remove tracking parameters.
    Treat those spellings as the same admitted public page.  A genuinely new
    Markdown link is reduced to its readable label and a new bare URL is
    removed.  This preserves the evidence boundary without replacing the whole
    response with a terse fallback for a citation-formatting mistake.
    """

    allowed_by_identity = {
        identity: url
        for url in allowed_urls
        if (identity := _public_url_identity(url))
    }

    def replace_markdown(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        emitted = match.group(2).strip()
        admitted = allowed_by_identity.get(_public_url_identity(emitted))
        return f"[{label}]({admitted})" if admitted else label

    cleaned = _MARKDOWN_URL_RE.sub(replace_markdown, text)

    def replace_bare(match: re.Match[str]) -> str:
        emitted = match.group(0).strip()
        return allowed_by_identity.get(_public_url_identity(emitted), "")

    return _URL_RE.sub(replace_bare, cleaned)


def _public_url_identity(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/") or "/",
            "",
            "",
        )
    )


def _evidence_urls(
    evidence: Sequence[ReceiptEvidenceBundle],
) -> set[str]:
    urls: set[str] = set()
    for bundle in evidence:
        for action in bundle.receipt.actions:
            output = action.output if isinstance(action.output, dict) else {}
            sources = list(output.get("sources") or [])
            for item in output.get("items") or []:
                if isinstance(item, dict):
                    sources.extend(item.get("evidence_sources") or [])
            for source in sources:
                if not isinstance(source, dict):
                    continue
                url = str(source.get("url") or "").strip()
                if url.lower().startswith(("https://", "http://")):
                    urls.add(url)
    return urls


def _evidence_book_titles(
    evidence: Sequence[ReceiptEvidenceBundle],
) -> set[str]:
    titles: set[str] = set()
    for bundle in evidence:
        for action in bundle.receipt.actions:
            if action.operation != "book_search_v1" or action.status != "completed":
                continue
            output = action.output if isinstance(action.output, dict) else {}
            items = output.get("items")
            if isinstance(items, list) and items:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    if title:
                        titles.add(title)
                continue
            sources = output.get("sources")
            if not isinstance(sources, list):
                continue
            for source in sources:
                if not isinstance(source, dict):
                    continue
                title = str(source.get("title") or "").strip()
                if title:
                    titles.add(title)
    return titles


def _normalized_title(value: str) -> str:
    return re.sub(
        r"[^\w\u4e00-\u9fff]+",
        "",
        str(value or "").casefold(),
    )


def _normalized_title_aliases(value: str) -> list[str]:
    raw = str(value or "").strip()
    values = [raw]
    base = re.split(r"[（(【\[]", raw, maxsplit=1)[0].strip()
    if len(_normalized_title(base)) >= 4:
        values.append(base)
    return list(
        dict.fromkeys(
            item
            for candidate in values
            if (item := _normalized_title(candidate))
        )
    )


def _reject_technical_text(text: str) -> None:
    if (
        _TECHNICAL_LEAK_RE.search(text)
        or contains_internal_reasoning(text)
        or _FUNCTION_CALL_ONLY_RE.fullmatch(text)
        or _JSON_TOOL_CALL_ONLY_RE.fullmatch(text)
    ):
        raise ValueError(
            "technical runtime details cannot enter a published answer"
        )


def _clean(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("published answer cannot be empty")
    if len(cleaned) > 32_000:
        raise ValueError("published answer exceeds its contract")
    return cleaned


__all__ = [
    "validate_direct_text",
    "validate_model_knowledge_text",
    "validate_synthesis",
]
