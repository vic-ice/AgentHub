from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from app.services.external_search.contracts import (
    SearchAttempt,
    SearchHit,
    SearchRequest,
    SearchResult,
)
from app.services.external_search.runtime_config import resolve_search_provider


_REGION_BY_ZONE = {
    "cn": "cn-zh",
    "intl": "us-en",
}
_TIMELIMIT_BY_RANGE = {
    "day": "d",
    "week": "w",
    "month": "m",
    "year": "y",
}
_DEFAULT_REGION = "us-en"
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


class DDGSProvider:
    """Keyless DuckDuckGo text search adapter over the shared SearchProvider port.

    ``ddgs`` is a synchronous library, so every call is executed in a worker
    thread and bounded by a timeout. Domain filters are applied here so the
    research book policy (``book.douban.com``) is honored at the provider
    boundary, not only at the gateway.
    """

    name = "ddgs"

    async def search(self, request: SearchRequest) -> SearchResult:
        start = time.perf_counter()
        config = await resolve_search_provider(
            self.name,
            env_setting="DDGS_REGION",
            default_api_base_url="",
            allow_anonymous=True,
        )
        if not config.enabled:
            return _unavailable(
                request,
                start,
                "not_configured",
                config.error or "DuckDuckGo is disabled",
            )

        zone = request.zone or (
            "cn" if _CJK_RE.search(request.query) else "intl"
        )
        region = _clean_region(
            config.settings.get("region")
            or _REGION_BY_ZONE.get(zone, "")
        )
        safesearch = str(
            config.settings.get("safesearch") or "moderate"
        ).strip().lower()
        if safesearch not in {"on", "moderate", "off"}:
            safesearch = "moderate"
        timeout_seconds = _bounded_int(
            config.settings.get("timeout_seconds"),
            20,
            minimum=3,
            maximum=60,
        )
        timelimit = _TIMELIMIT_BY_RANGE.get(request.time_range or "")
        max_results = max(1, min(int(request.max_results or 5), 10))
        effective_query = " ".join(request.query.split()).strip()

        try:
            raw_results = await asyncio.wait_for(
                asyncio.to_thread(
                    _run_ddgs_text,
                    query=effective_query,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                    max_results=max_results,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            return _unavailable(
                request,
                start,
                "timeout",
                f"DuckDuckGo search timed out after {timeout_seconds}s",
                effective_query=effective_query,
            )
        except Exception as exc:
            return _unavailable(
                request,
                start,
                _error_type(str(exc) or exc.__class__.__name__),
                str(exc) or exc.__class__.__name__,
                effective_query=effective_query,
            )

        hits = _project_hits(raw_results, request)
        duration_ms = int((time.perf_counter() - start) * 1000)
        outcome = "found" if hits else "empty"
        return SearchResult(
            outcome=outcome,
            provider=self.name,
            query=request.query,
            effective_query=effective_query,
            hits=hits,
            attempts=[
                SearchAttempt(
                    provider=self.name,
                    outcome=outcome,
                    duration_ms=duration_ms,
                )
            ],
            metadata={"region": region, "backend": "auto"},
        )


def _run_ddgs_text(
    *,
    query: str,
    region: str,
    safesearch: str,
    timelimit: str | None,
    max_results: int,
) -> list[dict[str, Any]]:
    from ddgs import DDGS

    return DDGS().text(
        query,
        region=region,
        safesearch=safesearch,
        timelimit=timelimit,
        max_results=max_results,
        backend="auto",
    )


def _project_hits(
    raw_results: list[dict[str, Any]],
    request: SearchRequest,
) -> list[SearchHit]:
    include = {
        _clean_domain(item) for item in request.include_domains
    }
    exclude = {
        _clean_domain(item) for item in request.exclude_domains
    }
    prefixes = tuple(
        str(item or "").strip()
        for item in request.include_url_prefixes
        if str(item or "").strip()
    )
    hits: list[SearchHit] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("href") or item.get("url") or "").strip()
        if not url:
            continue
        domain = _clean_domain(url)
        if include and domain not in include:
            continue
        if exclude and domain in exclude:
            continue
        if prefixes and not url.startswith(prefixes):
            continue
        hits.append(
            SearchHit(
                title=str(item.get("title") or ""),
                url=url,
                snippet=str(item.get("body") or item.get("description") or ""),
                content=str(
                    item.get("body")
                    or item.get("description")
                    or item.get("content")
                    or ""
                ),
                published_date=str(item.get("date") or ""),
                provider="ddgs",
                metadata={},
            )
        )
    return hits


def _clean_domain(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .removeprefix("https://")
        .removeprefix("http://")
        .split("/", 1)[0]
    )


def _clean_region(value: Any) -> str:
    token = str(value or "").strip().lower()
    return token if token else _DEFAULT_REGION


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _error_type(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(token in lowered for token in ("rate", "429", "blocked", "captcha")):
        return "rate_limited"
    if any(token in lowered for token in ("connect", "ssl", "dns", "socket")):
        return "network"
    return "adapter_error"


def _unavailable(
    request: SearchRequest,
    start: float,
    error_type: str,
    error: str,
    *,
    effective_query: str = "",
) -> SearchResult:
    return SearchResult(
        outcome="unavailable",
        provider="ddgs",
        query=request.query,
        effective_query=effective_query or request.query,
        attempts=[
            SearchAttempt(
                provider="ddgs",
                outcome="unavailable",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_type=error_type,
                error=error[:400],
            )
        ],
        error=error[:400],
    )


__all__ = ["DDGSProvider"]
