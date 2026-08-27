from __future__ import annotations

import asyncio
from collections.abc import Mapping
import time
from urllib.parse import urlsplit, urlunsplit

from app.services.external_search.contracts import (
    SearchAttempt,
    SearchProvider,
    SearchRequest,
    SearchResult,
)
from app.services.external_search.policy import provider_order


FEDERATED_SEARCH_DEADLINE_SECONDS = 20.0
FEDERATED_DIVERSITY_GRACE_SECONDS = 1.25
FEDERATED_PARTIAL_RECALL_GRACE_SECONDS = 4.0
FEDERATED_MIN_USEFUL_HITS = 3


class SearchGateway:
    """Own provider execution policy for one typed search request.

    ``failover`` preserves the low-latency first-success behavior used by quick
    lookups. ``federated`` queries the bounded provider portfolio concurrently,
    then normalizes, deduplicates and round-robin merges its hits.  Both remain
    one SearchGateway invocation and never create a second business owner.
    """

    def __init__(
        self,
        providers: Mapping[str, SearchProvider] | None = None,
    ) -> None:
        self._providers = dict(providers) if providers is not None else None

    async def search(
        self,
        request: SearchRequest | dict,
        *,
        previously_used: tuple[str, ...] = (),
    ) -> SearchResult:
        search_request = SearchRequest.model_validate(request)
        providers = (
            self._providers
            if self._providers is not None
            else _runtime_providers()
        )
        if search_request.strategy == "federated":
            return await self._search_federated(
                search_request,
                providers=providers,
                previously_used=previously_used,
            )
        attempts: list[SearchAttempt] = []
        empty_result: SearchResult | None = None

        for provider_name in provider_order(
            search_request,
            previously_used=previously_used,
        ):
            provider = providers.get(provider_name)
            if provider is None:
                attempts.append(
                    SearchAttempt(
                        provider=provider_name,
                        outcome="unavailable",
                        error_type="not_configured",
                        error=f"{provider_name} is not configured",
                    )
                )
                continue

            started_at = time.perf_counter()
            try:
                result = await provider.search(search_request)
            except Exception as exc:
                attempts.append(
                    SearchAttempt(
                        provider=provider_name,
                        outcome="unavailable",
                        duration_ms=int(
                            (time.perf_counter() - started_at) * 1000
                        ),
                        error_type="adapter_error",
                        error=str(exc) or exc.__class__.__name__,
                    )
                )
                continue
            result = _apply_business_filters(result, search_request)
            attempts.extend(result.attempts or [
                SearchAttempt(provider=provider_name, outcome=result.outcome)
            ])
            if result.outcome == "found":
                return result.model_copy(update={"attempts": attempts})
            if result.outcome == "empty" and empty_result is None:
                empty_result = result

        if empty_result is not None:
            return empty_result.model_copy(update={"attempts": attempts})
        return SearchResult(
            outcome="unavailable",
            query=search_request.query,
            effective_query=search_request.query,
            attempts=attempts,
            error=_combined_error(attempts),
            metadata={"search_strategy": "failover"},
        )

    async def _search_federated(
        self,
        request: SearchRequest,
        *,
        providers: Mapping[str, SearchProvider],
        previously_used: tuple[str, ...],
    ) -> SearchResult:
        ordered = provider_order(request, previously_used=previously_used)[
            : request.provider_budget
        ]
        executions = await _execute_federated_portfolio(
            ordered,
            providers=providers,
            request=request,
        )
        attempts: list[SearchAttempt] = []
        found: list[SearchResult] = []
        empty: list[SearchResult] = []
        for result in executions:
            attempts.extend(result.attempts)
            if result.outcome == "found":
                found.append(result)
            elif result.outcome == "empty":
                empty.append(result)

        if found:
            hits = _merge_provider_hits(found, limit=request.max_results)
            successful = [result.provider for result in found if result.provider]
            provider_warnings = [
                attempt.error
                for attempt in attempts
                if attempt.error and attempt.outcome == "unavailable"
            ]
            return SearchResult(
                outcome="found" if hits else "empty",
                provider=",".join(dict.fromkeys(successful)),
                query=request.query,
                effective_query=" | ".join(
                    dict.fromkeys(
                        result.effective_query or result.query
                        for result in found
                    )
                ),
                hits=hits,
                attempts=attempts,
                # A federated request succeeded as soon as another provider
                # supplied admitted hits. Keep partial-provider failures as
                # diagnostics; exposing them as the result error makes a
                # successful research round look failed to users.
                error="",
                metadata={
                    "search_strategy": "federated",
                    "provider_budget": request.provider_budget,
                    "attempted_providers": list(ordered),
                    "successful_providers": list(dict.fromkeys(successful)),
                    "provider_hit_counts": {
                        result.provider: len(result.hits) for result in found
                    },
                    "provider_warning_count": len(provider_warnings),
                    "source_domains": list(
                        dict.fromkeys(
                            domain
                            for hit in hits
                            if (domain := urlsplit(hit.url).netloc.casefold())
                        )
                    ),
                },
            )
        if empty:
            first = empty[0]
            return first.model_copy(
                update={
                    "attempts": attempts,
                    "metadata": {
                        **first.metadata,
                        "search_strategy": "federated",
                        "provider_budget": request.provider_budget,
                        "attempted_providers": list(ordered),
                        "successful_providers": [],
                    },
                }
            )
        return SearchResult(
            outcome="unavailable",
            query=request.query,
            effective_query=request.query,
            attempts=attempts,
            error=_combined_error(attempts),
            metadata={
                "search_strategy": "federated",
                "provider_budget": request.provider_budget,
                "attempted_providers": list(ordered),
                "successful_providers": [],
            },
        )


async def _execute_provider(
    provider_name: str,
    provider: SearchProvider | None,
    request: SearchRequest,
) -> SearchResult:
    if provider is None:
        return SearchResult(
            outcome="unavailable",
            provider=provider_name,
            query=request.query,
            effective_query=request.query,
            attempts=[
                SearchAttempt(
                    provider=provider_name,
                    outcome="unavailable",
                    error_type="not_configured",
                    error=f"{provider_name} is not configured",
                )
            ],
        )
    started_at = time.perf_counter()
    try:
        result = await provider.search(request)
    except Exception as exc:
        return SearchResult(
            outcome="unavailable",
            provider=provider_name,
            query=request.query,
            effective_query=request.query,
            attempts=[
                SearchAttempt(
                    provider=provider_name,
                    outcome="unavailable",
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    error_type="adapter_error",
                    error=str(exc) or exc.__class__.__name__,
                )
            ],
        )
    filtered = _apply_business_filters(result, request)
    if filtered.attempts:
        return filtered
    return filtered.model_copy(
        update={
            "attempts": [
                SearchAttempt(
                    provider=provider_name,
                    outcome=filtered.outcome,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                )
            ]
        }
    )


async def _execute_federated_portfolio(
    ordered: tuple[str, ...] | list[str],
    *,
    providers: Mapping[str, SearchProvider],
    request: SearchRequest,
) -> list[SearchResult]:
    """Keep fast usable results instead of waiting for every lagging provider."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + FEDERATED_SEARCH_DEADLINE_SECONDS
    tasks = {
        name: asyncio.create_task(
            _execute_provider(name, providers.get(name), request)
        )
        for name in ordered
    }
    results: dict[str, SearchResult] = {}
    first_found_at: float | None = None
    try:
        while tasks:
            now = loop.time()
            remaining = deadline - now
            merged_hit_count = len(
                _merge_provider_hits(
                    list(results.values()),
                    limit=request.max_results,
                )
            )
            if first_found_at is not None:
                minimum_useful = min(
                    request.max_results,
                    FEDERATED_MIN_USEFUL_HITS,
                )
                grace = (
                    FEDERATED_DIVERSITY_GRACE_SECONDS
                    if merged_hit_count >= minimum_useful
                    else FEDERATED_PARTIAL_RECALL_GRACE_SECONDS
                )
                remaining = min(
                    remaining,
                    first_found_at + grace - now,
                )
            if remaining <= 0:
                break
            done, _pending = await asyncio.wait(
                tuple(tasks.values()),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break
            for task in done:
                name = next(
                    provider_name
                    for provider_name, candidate in tasks.items()
                    if candidate is task
                )
                tasks.pop(name)
                result = task.result()
                results[name] = result
                if result.outcome == "found" and first_found_at is None:
                    first_found_at = loop.time()
            merged_hit_count = len(
                _merge_provider_hits(
                    list(results.values()),
                    limit=request.max_results,
                )
            )
            if merged_hit_count >= request.max_results:
                break
    finally:
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    laggard_reason = (
        "cancelled after another provider returned usable results"
        if any(result.outcome == "found" for result in results.values())
        else "federated search deadline exceeded"
    )
    for name, task in tasks.items():
        results[name] = SearchResult(
            outcome="unavailable",
            provider=name,
            query=request.query,
            effective_query=request.query,
            attempts=[
                SearchAttempt(
                    provider=name,
                    outcome="unavailable",
                    error_type=(
                        "laggard_cancelled"
                        if first_found_at is not None
                        else "deadline_exceeded"
                    ),
                    error=laggard_reason,
                )
            ],
            error=laggard_reason,
        )
    return [results[name] for name in ordered]


def _merge_provider_hits(
    results: list[SearchResult],
    *,
    limit: int,
) -> list:
    """Round-robin providers so one engine cannot monopolize the answer."""

    merged = []
    seen: set[str] = set()
    depth = max((len(result.hits) for result in results), default=0)
    for index in range(depth):
        for result in results:
            if index >= len(result.hits):
                continue
            hit = result.hits[index]
            key = _canonical_url(hit.url)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(hit)
            if len(merged) >= limit:
                return merged
    return merged


def _canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", "")
    )


def _runtime_providers() -> dict[str, SearchProvider]:
    from app.services.external_search.providers.anysearch import AnySearchProvider
    from app.services.external_search.providers.ddgs import DDGSProvider
    from app.services.external_search.providers.tavily import TavilyProvider

    return {
        "tavily": TavilyProvider(),
        "ddgs": DDGSProvider(),
        "anysearch": AnySearchProvider(),
    }


def _combined_error(attempts: list[SearchAttempt]) -> str:
    errors = [attempt.error for attempt in attempts if attempt.error]
    return "; ".join(dict.fromkeys(errors))[:1000]


def _apply_business_filters(
    result: SearchResult,
    request: SearchRequest,
) -> SearchResult:
    if result.outcome != "found":
        return result
    hits = result.hits
    if request.include_domains:
        hits = [
            hit
            for hit in hits
            if _host_matches_any(hit.url, request.include_domains)
        ]
    if request.exclude_domains:
        hits = [
            hit
            for hit in hits
            if not _host_matches_any(hit.url, request.exclude_domains)
        ]
    if request.include_url_prefixes:
        hits = [
            hit
            for hit in hits
            if any(
                hit.url.startswith(prefix)
                for prefix in request.include_url_prefixes
            )
        ]
    hits = hits[: request.max_results]
    if hits:
        return result.model_copy(
            update={
                "hits": hits,
                "metadata": {
                    **result.metadata,
                    "post_filter_count": len(hits),
                },
            }
        )
    normalized_attempts = [
        attempt.model_copy(
            update={
                "outcome": "empty",
                "error_type": "filtered_empty",
                "error": "provider results did not satisfy URL filters",
            }
        )
        for attempt in result.attempts
    ]
    return result.model_copy(
        update={
            "outcome": "empty",
            "hits": [],
            "attempts": normalized_attempts,
            "error": "",
            "metadata": {
                **result.metadata,
                "post_filter_count": 0,
            },
        }
    )


def _host_matches_any(url: str, domains: list[str]) -> bool:
    try:
        host = str(urlsplit(str(url or "")).hostname or "").casefold()
    except ValueError:
        return False
    return any(
        host == domain.casefold()
        or host.endswith("." + domain.casefold())
        for domain in domains
        if domain
    )


def get_search_gateway() -> SearchGateway:
    return SearchGateway()
