from __future__ import annotations

from app.services.external_capabilities.contracts import (
    ExternalEvidenceSource,
    WebEvidence,
    WebSearchInput,
)
from app.services.external_search.contracts import SearchHit, SearchResult
from app.services.research.content_quality_gate import check_content_garbage
from app.services.research.dedup import dedup_by_content


class WebSearchReceiptSanitizer:
    """Project gateway output into bounded citation evidence."""

    def sanitize(
        self,
        request: WebSearchInput,
        result: SearchResult,
    ) -> WebEvidence:
        hits = _clean_hits(
            result.hits,
            limit=request.max_results,
            detail=request.detail,
        )
        sources = [
            ExternalEvidenceSource(
                title=_bounded(hit.title, 300),
                url=_bounded(hit.url, 2_000),
                snippet=_bounded(
                    _hit_evidence_text(hit, detail=request.detail),
                    1_000,
                ),
                published_date=_bounded(hit.published_date, 64),
            )
            for hit in hits
            if str(hit.url or "").strip()
        ]
        if result.outcome == "found" and sources:
            status = "ok"
            error = ""
        elif result.outcome == "empty" or (
            result.outcome == "found" and not sources
        ):
            status = "empty_result"
            error = ""
        else:
            status = "unavailable"
            error = "web_source_unavailable"
        return WebEvidence(
            status=status,
            query=request.query,
            sources=sources,
            error=error,
        )


def _clean_hits(
    hits: list[SearchHit],
    *,
    limit: int,
    detail: str = "standard",
) -> list[SearchHit]:
    """Drop junk/duplicate search hits before they reach the model.

    Chat Search shares the same conservative quality gate and content-
    fingerprint dedup as the Deep Research evidence pipeline.
    """

    seen_urls: set[str] = set()
    kept: list[SearchHit] = []
    for hit in hits:
        url = str(hit.url or "").strip()
        if not url or url.lower() in seen_urls:
            continue
        garbage, _reason = check_content_garbage(
            f"{hit.title} {_hit_evidence_text(hit, detail=detail)}",
            min_chars=None,
        )
        if garbage:
            continue
        seen_urls.add(url.lower())
        kept.append(hit)
        if len(kept) >= limit:
            break
    return dedup_by_content(
        kept,
        text_of=lambda hit: (
            f"{hit.title} {_hit_evidence_text(hit, detail=detail)}"
        ),
    )[:limit]


def _hit_evidence_text(hit: SearchHit, *, detail: str) -> str:
    snippet = " ".join(str(hit.snippet or "").split())
    content = " ".join(str(hit.content or "").split())
    if detail == "deep" and content:
        garbage, _reason = check_content_garbage(content, min_chars=None)
        if not garbage:
            if snippet and snippet.casefold() not in content.casefold():
                # Provider snippets are usually query-focused; keep that
                # passage first, then add bounded page context instead of
                # replacing the relevant excerpt with the page introduction.
                return f"{snippet} {content}"
            return content
    return snippet or content


def _bounded(value: str, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


__all__ = ["WebSearchReceiptSanitizer"]

