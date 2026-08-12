from __future__ import annotations

import json
import re

from app.services.memory.guardrails import guard_memory_read_query
from app.services.memory.version_contracts import (
    MemorySearchReceipt,
    MemoryVersionRecord,
    SearchMemoryRequest,
)
from app.services.memory.versioned_schema_registry import (
    VersionedMemorySchemaRegistry,
)


MAX_HISTORY_RECORDS = 40
_TOKEN_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "do",
    "does",
    "i",
    "is",
    "me",
    "my",
    "the",
    "what",
    "who",
}
_SCHEMA_QUERY_HINTS = {
    "identity.self_reported_name": "名字 姓名 称呼 叫什么 name who am i call me",
    "identity.preferred_address": "称呼 叫我 怎么称呼 address call me",
    "identity.alias": "别名 昵称 alias nickname also known as",
    "entity.name": "名字 名称 昵称 英文名 叫什么 entity name object name name of",
    "preference.entity": "喜欢 不喜欢 讨厌 想要 避免 偏好 喜好 preference like dislike want avoid",
    "relationship.entity": "关系 认识 朋友 同事 家人 亲戚 related knows relationship",
    "possession.entity": "有 拥有 养 宠物 物品 东西 possess own have pet object",
    "instruction.behavior": "规则 指令 希望 不要 始终 记住 instruction always behavior",
    "feedback.outcome": "反馈 结果 效果 outcome feedback",
    "temporary.state": "状态 当前 暂时 临时 state status current temporary",
}
_HISTORY_SCOPE_QUERY_RE = re.compile(
    r"(?:之前|以前|原来|先前|最早|一开始|最开始|后来|改过|变过|"
    r"历史|变更|时间线|\bprevious\b|\bearliest\b|\bhistory\b|\btimeline\b)",
    re.IGNORECASE,
)


class VersionedMemorySearch:
    """Filter, scope and rank canonical facts across their version chains."""

    def __init__(
        self,
        registry: VersionedMemorySchemaRegistry | None = None,
    ) -> None:
        self._registry = registry or VersionedMemorySchemaRegistry()

    def search(
        self,
        records: list[MemoryVersionRecord],
        request: SearchMemoryRequest,
        *,
        semantic_memory_keys: tuple[str, ...] | list[str] | None = None,
    ) -> MemorySearchReceipt:
        schema_key = ""
        if request.predicate:
            schema = self._registry.resolve_predicate(request.predicate)
            if schema is None:
                return MemorySearchReceipt(
                    status="empty",
                    scope=request.scope,
                )
            schema_key = schema.schema_key

        scoped = records
        if schema_key:
            scoped = [
                record for record in scoped if record.schema_key == schema_key
            ]
        scoped = self._select_scope(scoped, request)

        query = request.query.casefold()
        if query and guard_memory_read_query(query).blocked:
            return MemorySearchReceipt(
                status="empty",
                scope=request.scope,
            )
        semantic_rank = _semantic_rank(semantic_memory_keys or ()) if query else {}
        ranked: list[tuple[int, int, MemoryVersionRecord]] = []
        for index, record in enumerate(scoped):
            if request.scope == "current" and _HISTORY_SCOPE_QUERY_RE.search(query):
                continue
            searchable = _searchable_text(record)
            lexical_score = _query_score(query, record, searchable)
            semantic_score = _semantic_score(record.memory_key, semantic_rank)
            if query and lexical_score <= 0:
                semantic_only_allowed = bool(schema_key and semantic_score > 0)
                if (
                    not semantic_only_allowed
                    and (request.scope == "current" or not schema_key)
                ):
                    continue
            score = lexical_score + semantic_score
            ranked.append((score, -index, record))
        ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
        truncated = len(ranked) > MAX_HISTORY_RECORDS
        memories = [item[2] for item in ranked[:MAX_HISTORY_RECORDS]]
        return MemorySearchReceipt(
            status="completed" if memories else "empty",
            scope=request.scope,
            memories=memories,
            truncated=truncated,
        )

    def _select_scope(
        self,
        records: list[MemoryVersionRecord],
        request: SearchMemoryRequest,
    ) -> list[MemoryVersionRecord]:
        if request.scope == "current":
            return [
                record
                for record in records
                if record.superseded_by is None and not record.is_tombstone
            ]

        chains: dict[str, list[MemoryVersionRecord]] = {}
        for record in records:
            if not record.memory_key:
                continue
            chains.setdefault(record.memory_key, []).append(record)
        ordered = {
            key: sorted(items, key=lambda item: item.version_no)
            for key, items in chains.items()
        }

        if request.scope == "earliest":
            return [
                items[0]
                for items in ordered.values()
                if items and items[0].previous_version_id is None
            ]
        if request.scope == "previous":
            selected: list[MemoryVersionRecord] = []
            for items in ordered.values():
                if len(items) < 2:
                    continue
                depth = max(1, min(int(request.depth), len(items) - 1))
                selected.extend(reversed(items[-1 - depth : -1]))
            return selected

        selected = []
        for items in ordered.values():
            for record in items:
                if _overlaps_window(
                    record,
                    since=request.since,
                    until=request.until,
                ):
                    selected.append(record)
        return selected


def _overlaps_window(
    record: MemoryVersionRecord,
    *,
    since,
    until,
) -> bool:
    if since is not None and record.valid_to is not None:
        if record.valid_to < since:
            return False
    if until is not None and record.valid_from is not None:
        if record.valid_from > until:
            return False
    return True


def _searchable_text(record: MemoryVersionRecord) -> str:
    return " ".join(
        [
            record.schema_key,
            record.subject,
            _SCHEMA_QUERY_HINTS.get(record.schema_key, ""),
            record.evidence_quote,
            json.dumps(record.value, ensure_ascii=False),
            json.dumps(record.qualifiers, ensure_ascii=False),
        ]
    ).casefold()


def _query_score(
    query: str,
    record: MemoryVersionRecord,
    searchable: str,
) -> int:
    if not query:
        return 1
    evidence = record.evidence_quote.casefold()
    if query in evidence:
        return 6
    if query in searchable:
        return 5

    query_terms = _terms(query)
    if not query_terms:
        return 0
    searchable_terms = _terms(searchable)
    overlap = query_terms & searchable_terms
    if not overlap:
        return 0

    evidence_overlap = query_terms & _terms(evidence)
    schema_overlap = query_terms & _terms(_SCHEMA_QUERY_HINTS.get(record.schema_key, ""))
    coverage = len(overlap) / max(1, len(query_terms))
    return (
        1
        + len(overlap)
        + len(evidence_overlap)
        + len(schema_overlap)
        + int(coverage * 3)
    )


def _terms(text: str) -> set[str]:
    normalized = str(text or "").casefold()
    ascii_terms = {
        term
        for term in re.findall(r"[a-z0-9]+", normalized)
        if len(term) > 1 and term not in _TOKEN_STOPWORDS
    }
    cjk_fragments = re.findall(r"[\u3400-\u9fff]+", normalized)
    cjk_terms: set[str] = set()
    for fragment in cjk_fragments:
        if len(fragment) == 1:
            cjk_terms.add(fragment)
            continue
        cjk_terms.add(fragment)
        cjk_terms.update(
            fragment[index : index + 2]
            for index in range(len(fragment) - 1)
        )
    return ascii_terms | cjk_terms


def _semantic_rank(
    memory_keys: tuple[str, ...] | list[str],
) -> dict[str, int]:
    ranked: dict[str, int] = {}
    for index, key in enumerate(memory_keys):
        normalized = str(key or "").strip()
        if normalized and normalized not in ranked:
            ranked[normalized] = index
    return ranked


def _semantic_score(memory_key: str, semantic_rank: dict[str, int]) -> int:
    rank = semantic_rank.get(memory_key)
    if rank is None:
        return 0
    return 3 + max(0, 2 - min(rank, 2))

