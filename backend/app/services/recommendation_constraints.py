from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.memory.contracts import MemoryEvent
from app.services.memory.read_gateway import MemoryReadGateway


CONSTRAINTS_CONTRACT_VERSION = "personalized-recommendation-constraints-v1"
_PROFILE_TYPES = {"preference", "correction"}
_LIKE_POLARITIES = {"like", "want"}
_DISLIKE_POLARITIES = {"dislike", "avoid"}
_TAG_SUBJECTS = {"tag", "theme", "style", "genre", "mood", "pacing", "content"}
_AUTHOR_SUBJECTS = {"author"}
_WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.IGNORECASE)
_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "book",
    "books",
    "for",
    "i",
    "me",
    "novel",
    "novels",
    "of",
    "or",
    "please",
    "recommend",
    "the",
    "to",
}


class PersonalizedRecommendationConstraints(BaseModel):
    """CurrentMemory-derived constraints for ordinary recommendations."""

    user_id: UUID
    original_query: str = ""
    effective_query: str = ""
    preferred_terms: list[str] = Field(default_factory=list)
    avoided_terms: list[str] = Field(default_factory=list)
    favorite_authors: list[str] = Field(default_factory=list)
    disliked_authors: list[str] = Field(default_factory=list)
    source_memory_ids: list[str] = Field(default_factory=list)
    search_terms_added: list[str] = Field(default_factory=list)
    shelf_reference_titles: list[str] = Field(default_factory=list)
    applied_to_search: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


async def build_personalized_recommendation_constraints(
    session: AsyncSession,
    *,
    user_id: UUID,
    query: str,
    max_search_terms: int = 3,
) -> PersonalizedRecommendationConstraints:
    memories = await MemoryReadGateway(session).profile_memories(
        user_id=user_id,
        memory_types=sorted(_PROFILE_TYPES),
        limit=100,
    )
    constraints = _constraints_from_memories(
        user_id=user_id,
        query=query,
        memories=memories,
    )
    _apply_search_query_constraints(
        constraints,
        max_search_terms=max_search_terms,
    )
    await _add_reading_anchor_terms(
        session,
        user_id=user_id,
        constraints=constraints,
        max_search_terms=max_search_terms,
    )
    await _add_shelf_reference_titles(
        session,
        user_id=user_id,
        constraints=constraints,
    )
    return constraints


def _constraints_from_memories(
    *,
    user_id: UUID,
    query: str,
    memories: list[MemoryEvent],
) -> PersonalizedRecommendationConstraints:
    constraints = PersonalizedRecommendationConstraints(
        user_id=user_id,
        original_query=query,
        effective_query=query,
        source_memory_ids=[str(memory.id) for memory in memories if memory.id],
        metadata={
            "contract_version": CONSTRAINTS_CONTRACT_VERSION,
            "constraint_source": "current_memory",
            "current_memory_count": len(memories),
        },
    )
    for memory in memories:
        if memory.type not in _PROFILE_TYPES:
            continue
        value = memory.value.strip()
        if not value:
            continue
        if memory.subject in _AUTHOR_SUBJECTS:
            if memory.polarity in _LIKE_POLARITIES:
                constraints.favorite_authors = _add_unique(
                    constraints.favorite_authors,
                    value,
                )
            elif memory.polarity in _DISLIKE_POLARITIES:
                constraints.disliked_authors = _add_unique(
                    constraints.disliked_authors,
                    value,
                )
        elif memory.subject in _TAG_SUBJECTS:
            if memory.polarity in _LIKE_POLARITIES:
                constraints.preferred_terms = _add_unique(
                    constraints.preferred_terms,
                    value,
                )
            elif memory.polarity in _DISLIKE_POLARITIES:
                constraints.avoided_terms = _add_unique(
                    constraints.avoided_terms,
                    value,
                )
    return constraints


def _apply_search_query_constraints(
    constraints: PersonalizedRecommendationConstraints,
    *,
    max_search_terms: int,
) -> None:
    additions: list[str] = []
    for value in [*constraints.favorite_authors, *constraints.preferred_terms]:
        if len(additions) >= max(0, max_search_terms):
            break
        if _is_redundant_query_term(value, constraints.original_query):
            continue
        additions.append(value)

    constraints.search_terms_added = additions
    constraints.applied_to_search = bool(additions)
    if additions:
        constraints.effective_query = " ".join(
            item for item in [constraints.original_query.strip(), *additions] if item
        )
    else:
        constraints.effective_query = constraints.original_query


def _is_redundant_query_term(value: str, query: str) -> bool:
    normalized_value = value.strip().lower()
    normalized_query = query.strip().lower()
    if not normalized_value:
        return True
    if normalized_value in normalized_query:
        return True
    value_terms = _query_terms(normalized_value)
    query_terms = set(_query_terms(normalized_query, max_terms=30))
    return bool(value_terms) and all(term in query_terms for term in value_terms)


def _query_terms(query: str, max_terms: int = 12) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in _WORD_RE.findall(query.lower()):
        if len(match) < 2 or match in _QUERY_STOPWORDS or match in seen:
            continue
        seen.add(match)
        terms.append(match)
        if len(terms) >= max_terms:
            break
    return terms


def _add_unique(values: list[str], value: str) -> list[str]:
    seen = {item.lower() for item in values}
    item = value.strip()
    if item and item.lower() not in seen:
        return [*values, item]
    return values


async def _add_reading_anchor_terms(
    session: AsyncSession,
    *,
    user_id: UUID,
    constraints: PersonalizedRecommendationConstraints,
    max_search_terms: int,
) -> None:
    """Fold top read-book style tags into the effective search query.

    Read books are style anchors: their tags help ordinary Search find
    similar books instead of re-recommending what the user has read.
    """
    try:
        from app.services.books.reading_service import ReadingService

        anchors = await ReadingService(session).reading_anchors(user_id)
    except Exception:
        return
    seen = {item.lower() for item in constraints.search_terms_added}
    additions: list[str] = []
    budget = max(0, max_search_terms)
    for anchor in anchors[:10]:
        for tag in anchor.get("tags") or []:
            token = str(tag).strip()
            if not token or token.lower() in seen:
                continue
            if _is_redundant_query_term(token, constraints.original_query):
                continue
            seen.add(token.lower())
            additions.append(token)
            if len([*constraints.search_terms_added, *additions]) >= budget:
                break
        if len([*constraints.search_terms_added, *additions]) >= budget:
            break
    if not additions:
        return
    constraints.search_terms_added = [
        *constraints.search_terms_added,
        *additions,
    ][:budget]
    constraints.applied_to_search = True
    constraints.effective_query = " ".join(
        item
        for item in [constraints.original_query.strip(), *constraints.search_terms_added]
        if item
    )
    constraints.metadata["reading_anchor_terms_added"] = additions


async def _add_shelf_reference_titles(
    session: AsyncSession,
    *,
    user_id: UUID,
    constraints: PersonalizedRecommendationConstraints,
    limit: int = 3,
) -> None:
    """Use positive Shelf entries as discovery anchors for Shelf-based requests.

    The Shelf remains authoritative for current reading assets.  Memory supplies
    broader preferences, while these titles give catalog discovery a concrete
    starting point when the user's request is deictic (for example, "根据我的书架
    推荐").  RecommendationProjector still performs the final full-Shelf
    exclusion, so an anchor can shape discovery but can never be returned again.
    """

    if not _asks_for_shelf_personalization(constraints.original_query):
        return
    try:
        from app.services.books.reading_service import ReadingService

        entries, total = await ReadingService(session).list_entries(
            user_id=user_id,
            limit=100,
            offset=0,
        )
    except Exception:
        constraints.metadata["shelf_context_status"] = "unavailable"
        return

    ranked = sorted(
        enumerate(entries),
        key=lambda pair: (_shelf_reference_score(pair[1]), -pair[0]),
        reverse=True,
    )
    titles = [
        str(entry.title).strip()
        for _, entry in ranked
        if _shelf_reference_score(entry) >= 0 and str(entry.title or "").strip()
    ]
    constraints.shelf_reference_titles = list(dict.fromkeys(titles))[
        : max(0, limit)
    ]
    constraints.metadata.update(
        {
            "shelf_context_status": "available",
            "shelf_entry_count": total,
            "shelf_reference_titles": list(constraints.shelf_reference_titles),
        }
    )


_SHELF_PERSONALIZATION_RE = re.compile(
    r"(?:我的|本人|我)?\s*(?:书架|藏书|阅读记录|读书记录|已读|在读).{0,24}"
    r"(?:推荐|找|选|新书)|(?:根据|结合|参考|按照).{0,16}"
    r"(?:书架|藏书|阅读记录|读书记录|读过的书)",
    re.IGNORECASE,
)


def _asks_for_shelf_personalization(query: str) -> bool:
    return _SHELF_PERSONALIZATION_RE.search(" ".join(str(query or "").split())) is not None


def _shelf_reference_score(entry: Any) -> int:
    status = str(getattr(entry, "reading_status", "") or "").strip()
    evaluation = str(getattr(entry, "evaluation", "") or "").strip()
    if status == "dropped" or evaluation in {"disliked", "not_interested"}:
        return -1
    score = {
        "read": 30,
        "reading": 25,
        "want_to_read": 10,
    }.get(status, 0)
    score += {
        "liked": 40,
        "neutral": 5,
    }.get(evaluation, 0)
    rating = getattr(entry, "rating", None)
    if isinstance(rating, int):
        score += max(0, min(rating, 5))
    return score
