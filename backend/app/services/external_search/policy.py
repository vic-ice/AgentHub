from __future__ import annotations

from app.services.external_search.contracts import SearchRequest


_PROVIDERS = ("tavily", "ddgs", "anysearch")


def provider_order(
    request: SearchRequest,
    *,
    previously_used: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return a deterministic provider order; it performs no I/O."""

    # Quality-first order: Tavily (keyed, deep detail) leads, DuckDuckGo is a
    # keyless fallback, AnySearch (anonymous) is the last-resort for Chinese
    # coverage. Previously used providers move to the back so a fresh cycle
    # gets a different source before repeating.
    ordered = list(_PROVIDERS)

    used = {item.strip().lower() for item in previously_used}
    return tuple(
        [item for item in ordered if item not in used]
        + [item for item in ordered if item in used]
    )
