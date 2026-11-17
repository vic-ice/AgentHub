from __future__ import annotations

from typing import Any

from app.services.research.dedup import (
    jaccard_similarity,
    normalize_fingerprint_text,
    shingles,
)


class QueryLoopDetector:
    """Detect repeated search directions without any vector model.

    Queries are compared by character bigram Jaccard after stripping the
    shared objective/base prefix, so legitimate gap changes are not flagged
    while near-identical search directions are. Hints are differentiated by
    the previous result quality so the planner can change strategy instead of
    micro-tuning keywords.
    """

    def __init__(
        self,
        *,
        window: int = 5,
        threshold: float = 0.88,
        shingle_size: int = 2,
        history: list[str] | None = None,
        exclude_prefix: str = "",
    ) -> None:
        self._window = max(1, int(window))
        self._threshold = float(threshold)
        self._shingle_size = max(1, int(shingle_size))
        self._exclude_prefix = normalize_fingerprint_text(exclude_prefix)
        self._history: list[dict[str, str]] = [
            {"query": item, "quality": "unknown"}
            for item in (history or [])
            if normalize_fingerprint_text(item)
        ][-self._window :]

    def check(self, query: str) -> dict[str, Any]:
        """Compare one candidate query against recent history."""

        candidate = self._variant(query)
        if not candidate:
            return {
                "is_loop": False,
                "hint": "",
                "matched_query": "",
                "similarity": 0.0,
            }
        candidate_shingles = shingles(candidate, self._shingle_size)
        best: tuple[float, str] = (0.0, "")
        for entry in self._history:
            historical = self._variant(entry["query"])
            if not historical:
                continue
            similarity = jaccard_similarity(
                candidate_shingles,
                shingles(historical, self._shingle_size),
            )
            if similarity > best[0]:
                best = (similarity, entry["query"])
        similarity, matched_query = best
        if matched_query and similarity >= self._threshold:
            quality = self._quality_of(matched_query)
            return {
                "is_loop": True,
                "hint": _loop_hint(matched_query, quality),
                "matched_query": matched_query,
                "similarity": round(similarity, 4),
            }
        self._record(query)
        return {
            "is_loop": False,
            "hint": "",
            "matched_query": "",
            "similarity": round(similarity, 4),
        }

    def mark_result_quality(self, quality: str) -> None:
        """Record the result quality of the most recent query."""

        if not self._history:
            return
        self._history[-1]["quality"] = str(quality or "").strip().lower()

    def _variant(self, query: str) -> str:
        """Return the query minus the shared base prefix."""

        normalized = normalize_fingerprint_text(query)
        if not normalized:
            return ""
        if (
            self._exclude_prefix
            and normalized.startswith(self._exclude_prefix)
        ):
            return normalized[len(self._exclude_prefix) :].strip()
        return normalized

    def _record(self, query: str) -> None:
        self._history.append({"query": query, "quality": "unknown"})
        if len(self._history) > self._window:
            self._history.pop(0)

    def _quality_of(self, query: str) -> str:
        for entry in self._history:
            if entry["query"] == query:
                return entry["quality"]
        return "unknown"


def _loop_hint(matched_query: str, past_quality: str) -> str:
    if past_quality in {"empty", "empty_result", "no_extractable_claims"}:
        return (
            f"检测到重复搜索：'{matched_query}' 方向之前未找到有效结果。"
            "请从完全不同的角度切入，例如对立面、上位概念或具体案例来源。"
        )
    if past_quality.startswith("garbage_") or past_quality == "garbage":
        return (
            f"检测到重复搜索：'{matched_query}' 方向返回的是无效内容"
            "（登录墙/广告页）。建议直接访问可信来源的具体 URL，"
            "而不是继续关键词搜索。"
        )
    return (
        f"检测到重复搜索：'{matched_query}' 与当前查询高度相似。"
        "请重新梳理信息缺口，从子问题出发设计搜索词，"
        "而不是重复大范围搜索。"
    )


__all__ = ["QueryLoopDetector"]
