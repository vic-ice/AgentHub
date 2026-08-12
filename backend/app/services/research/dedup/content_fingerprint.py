from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar


T = TypeVar("T")


def normalize_fingerprint_text(value: Any) -> str:
    """Lowercase and collapse whitespace before shingling."""

    return " ".join(str(value or "").lower().split())


def shingles(text: str, n: int = 3) -> set[str]:
    """Character-level n-gram shingles robust to light rewriting."""

    normalized = normalize_fingerprint_text(text)
    size = max(1, int(n))
    if len(normalized) < size:
        return {normalized} if normalized else set()
    return {
        normalized[index : index + size]
        for index in range(len(normalized) - size + 1)
    }


def jaccard_similarity(left: set[str], right: set[str]) -> float:
    """Jaccard similarity over shingle sets."""

    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


class ShingleJaccardDetector:
    """Pure-hash near-duplicate detector with no vector model.

    Compares a new text against already-kept candidates using character
    n-gram Jaccard similarity. An interface-compatible vector backend may
    replace this implementation later without changing callers.
    """

    def __init__(
        self,
        *,
        threshold: float = 0.8,
        shingle_size: int = 3,
    ) -> None:
        self._threshold = float(threshold)
        self._shingle_size = max(1, int(shingle_size))

    def is_near_duplicate(
        self,
        text: str,
        candidates: Iterable[str],
    ) -> tuple[bool, str | None]:
        """Return (is_duplicate, matched_candidate_text)."""

        text_shingles = shingles(text, self._shingle_size)
        for candidate in candidates:
            if jaccard_similarity(
                text_shingles,
                shingles(candidate, self._shingle_size),
            ) >= self._threshold:
                return True, candidate
        return False, None


def dedup_by_content(
    items: list[T],
    *,
    text_of: Callable[[T], str],
    detector: ShingleJaccardDetector | None = None,
) -> list[T]:
    """Keep the first item of every near-duplicate content cluster.

    Items whose text is a near-duplicate of an earlier item are dropped;
    empty text is always kept. Stable order is preserved.
    """

    near_duplicate = detector or ShingleJaccardDetector()
    kept: list[T] = []
    seen: list[str] = []
    for item in items:
        text = normalize_fingerprint_text(text_of(item))
        if not text:
            kept.append(item)
            continue
        is_dup, _matched = near_duplicate.is_near_duplicate(text, seen)
        if is_dup:
            continue
        seen.append(text)
        kept.append(item)
    return kept


__all__ = [
    "ShingleJaccardDetector",
    "dedup_by_content",
    "jaccard_similarity",
    "normalize_fingerprint_text",
    "shingles",
]
