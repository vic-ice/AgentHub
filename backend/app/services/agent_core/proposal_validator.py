from __future__ import annotations

from app.services.agent_core.capabilities import CapabilityRegistry
from app.services.agent_core.contracts import (
    CapabilityProposalBatch,
    ControllerOutput,
    ValidatedCapabilityProposal,
)
from app.services.system_owned_fields import (
    MODEL_PROPOSAL_FORBIDDEN_FIELDS,
    find_forbidden_paths,
)
from app.services.external_capabilities.contracts import BookSearchInput
from app.services.memory.versioned_schema_registry import (
    VersionedMemorySchemaRegistry,
)


class ProposalValidator:
    """Validate model proposals without compiling or executing them."""

    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self._registry = registry or CapabilityRegistry()

    def validate(self, output: ControllerOutput) -> CapabilityProposalBatch:
        if output.mode != "capability_proposals":
            return CapabilityProposalBatch()

        call_ids = [item.call_id for item in output.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("controller tool call IDs must be unique")
        known = set(call_ids)

        proposals: list[ValidatedCapabilityProposal] = []
        invalid_calls: list[tuple[object, object, Exception]] = []
        for call in output.tool_calls:
            invalid = sorted(
                find_forbidden_paths(
                    call.arguments,
                    forbidden_fields=MODEL_PROPOSAL_FORBIDDEN_FIELDS,
                )
            )
            if invalid:
                raise ValueError(
                    "model proposal contains system-owned fields: "
                    + ", ".join(invalid)
                )
            missing = [item for item in call.depends_on if item not in known]
            if missing:
                raise ValueError(
                    f"tool call {call.call_id} has unknown dependencies: {missing}"
                )
            if call.call_id in call.depends_on:
                raise ValueError(
                    f"tool call {call.call_id} cannot depend on itself"
                )

            spec = self._registry.require_enabled(call.name)
            try:
                payload = spec.input_model.model_validate(call.arguments)
            except Exception as exc:
                invalid_calls.append((call, spec, exc))
                continue
            proposals.append(
                ValidatedCapabilityProposal(
                    call_id=call.call_id,
                    capability=spec.name,
                    arguments=payload.model_dump(mode="json"),
                    depends_on=list(call.depends_on),
                    side_effect=spec.side_effect,
                )
            )
        _reject_unsafe_partial_validation(
            proposals=proposals,
            invalid_calls=invalid_calls,
        )
        proposals = _complete_memory_read_predicate_fallback(proposals)
        proposals = _coalesce_recommendation_owner(proposals)
        proposals = _coalesce_equivalent_memory_reads(proposals)
        return CapabilityProposalBatch(
            proposals=_apply_authoritative_read_owners(proposals)
        )


_BOOKSHELF_FALLBACK_CAPABILITIES = frozenset({
    "search_memory",
    "book_search",
})


def _complete_memory_read_predicate_fallback(
    proposals: list[ValidatedCapabilityProposal],
) -> list[ValidatedCapabilityProposal]:
    """Recover a missing read predicate from the model's structured query.

    This is a bounded contract fallback, not a second interpretation of the
    user message: it only classifies the query field already produced by the
    Controller against the canonical Memory schema registry. Free-form or
    ambiguous queries remain unchanged.
    """

    registry = VersionedMemorySchemaRegistry()
    completed: list[ValidatedCapabilityProposal] = []
    for item in proposals:
        if item.capability != "search_memory" or str(
            item.arguments.get("predicate") or ""
        ).strip():
            completed.append(item)
            continue
        query = str(item.arguments.get("query") or "").strip()
        schema = registry.resolve_predicate(query)
        if schema is None:
            completed.append(item)
            continue
        arguments = dict(item.arguments)
        arguments["predicate"] = schema.schema_key
        completed.append(item.model_copy(update={"arguments": arguments}))
    return completed


def _coalesce_equivalent_memory_reads(
    proposals: list[ValidatedCapabilityProposal],
) -> list[ValidatedCapabilityProposal]:
    """Collapse synonym predicates for one otherwise-identical Memory read."""

    reads = [item for item in proposals if item.capability == "search_memory"]
    if len(reads) < 2:
        return proposals
    groups: dict[tuple[object, ...], list[ValidatedCapabilityProposal]] = {}
    for item in reads:
        arguments = item.arguments
        key = (
            arguments.get("scope"),
            str(arguments.get("query") or "").strip().casefold(),
            arguments.get("since"),
            arguments.get("until"),
        )
        groups.setdefault(key, []).append(item)

    replacements: dict[str, ValidatedCapabilityProposal] = {}
    suppressed: set[str] = set()
    suppressed_to_primary: dict[str, str] = {}
    for items in groups.values():
        if len(items) < 2:
            continue
        primary = items[0]
        duplicate_ids = {item.call_id for item in items[1:]}
        arguments = dict(primary.arguments)
        # Keep the primary validated predicate.  Clearing it looks like a
        # neutral union, but predicate-only reads would then lose their final
        # search constraint and fail the runtime SearchMemoryRequest contract.
        # VersionedMemorySearch owns synonym/legacy-schema expansion, so one
        # validated predicate is sufficient to cover equivalent aliases.
        arguments["depth"] = max(
            int(item.arguments.get("depth") or 1) for item in items
        )
        replacements[primary.call_id] = primary.model_copy(
            update={
                "arguments": arguments,
                "depends_on": list(dict.fromkeys(
                    dependency
                    for item in items
                    for dependency in item.depends_on
                    if dependency not in duplicate_ids
                    and dependency != primary.call_id
                )),
            }
        )
        suppressed.update(duplicate_ids)
        suppressed_to_primary.update(
            {duplicate_id: primary.call_id for duplicate_id in duplicate_ids}
        )

    if not suppressed:
        return proposals
    result: list[ValidatedCapabilityProposal] = []
    for item in proposals:
        if item.call_id in suppressed:
            continue
        candidate = replacements.get(item.call_id, item)
        candidate = candidate.model_copy(
            update={
                "depends_on": list(dict.fromkeys(
                    suppressed_to_primary.get(dependency, dependency)
                    for dependency in candidate.depends_on
                    if dependency != candidate.call_id
                ))
            }
        )
        result.append(candidate)
    return result


def _coalesce_recommendation_owner(
    proposals: list[ValidatedCapabilityProposal],
) -> list[ValidatedCapabilityProposal]:
    """Collapse one recommendation intent to one domain-owner invocation.

    The Controller may split anchors into multiple catalog queries. This
    boundary only combines already-validated structured fields; it does not
    inspect or reinterpret the user's natural-language message.
    """

    searches = [item for item in proposals if item.capability == "book_search"]
    if not searches:
        return proposals
    modes = {str(item.arguments.get("mode") or "recommendation") for item in searches}
    if len(modes) != 1:
        raise ValueError("lookup and recommendation book searches cannot be merged")

    primary = searches[0]
    duplicate_ids = {item.call_id for item in searches[1:]}
    mode = modes.pop()
    if duplicate_ids:
        queries = list(dict.fromkeys(
            str(item.arguments.get("query") or "").strip()
            for item in searches
            if str(item.arguments.get("query") or "").strip()
        ))
        languages = list(dict.fromkeys(
            str(item.arguments.get("language") or "").strip()
            for item in searches
            if str(item.arguments.get("language") or "").strip()
        ))
        if len(languages) > 1:
            raise ValueError("book searches with conflicting languages cannot be merged")
        audiences = list(dict.fromkeys(
            str(item.arguments.get("audience") or "").strip()
            for item in searches
            if str(item.arguments.get("audience") or "").strip()
        ))
        reference_titles = _merge_structured_lists(
            searches,
            field="reference_titles",
            limit=20,
        )
        excluded_titles = _merge_structured_lists(
            searches,
            field="excluded_titles",
            limit=40,
        )
        theme_exclusions = {
            _structured_value_key(value)
            for value in [*reference_titles, *excluded_titles]
            if _structured_value_key(value)
        }
        arguments = {
            "query": _merge_text_values(queries, limit=300),
            "mode": mode,
            "limit": max(int(item.arguments.get("limit") or 5) for item in searches),
            "response_depth": max(
                (
                    str(item.arguments.get("response_depth") or "balanced")
                    for item in searches
                ),
                key=lambda value: {"quick": 0, "balanced": 1, "deep": 2}.get(
                    value,
                    1,
                ),
            ),
            "language": languages[0] if languages else "",
            "genres": _merge_structured_lists(
                searches,
                field="genres",
                limit=10,
            ),
            "themes": _merge_structured_lists(
                searches,
                field="themes",
                limit=6,
                excluded_keys=theme_exclusions,
            ),
            "authors": _merge_structured_lists(
                searches,
                field="authors",
                limit=10,
            ),
            "candidate_titles": _merge_structured_lists(
                searches,
                field="candidate_titles",
                limit=10,
                excluded_keys=theme_exclusions,
            ),
            "audience": _merge_text_values(audiences, limit=120),
            "reference_titles": reference_titles,
            "excluded_titles": excluded_titles,
            "publication_year_from": max(
                (
                    int(item.arguments["publication_year_from"])
                    for item in searches
                    if item.arguments.get("publication_year_from") is not None
                ),
                default=None,
            ),
            "publication_year_to": min(
                (
                    int(item.arguments["publication_year_to"])
                    for item in searches
                    if item.arguments.get("publication_year_to") is not None
                ),
                default=None,
            ),
        }
        payload = BookSearchInput.model_validate(arguments)
        primary = primary.model_copy(
            update={
                "arguments": payload.model_dump(mode="json"),
                "depends_on": list(dict.fromkeys(
                    dependency
                    for item in searches
                    for dependency in item.depends_on
                    if dependency not in duplicate_ids
                    and dependency != primary.call_id
                )),
            }
        )

    # A parallel web_search would be a second, unpersonalized recommendation
    # candidate owner and could reintroduce Shelf-known books. Freshness and
    # provider fan-out belong inside RecommendationService/SearchGateway.
    web_fallback_ids = {
        item.call_id
        for item in proposals
        if mode == "recommendation" and item.capability == "web_search"
    }
    suppressed_ids = duplicate_ids | web_fallback_ids

    normalized: list[ValidatedCapabilityProposal] = []
    for item in proposals:
        if item.call_id in suppressed_ids:
            continue
        candidate = primary if item.call_id == primary.call_id else item
        dependencies = list(dict.fromkeys(
            primary.call_id if dependency in suppressed_ids else dependency
            for dependency in candidate.depends_on
            if dependency != candidate.call_id
        ))
        normalized.append(candidate.model_copy(update={"depends_on": dependencies}))
    return normalized


def _merge_structured_lists(
    searches: list[ValidatedCapabilityProposal],
    *,
    field: str,
    limit: int,
    excluded_keys: set[str] | None = None,
) -> list[str]:
    """Round-robin already structured values into the target contract bound."""

    groups = [
        [str(value or "").strip() for value in item.arguments.get(field, [])]
        for item in searches
    ]
    merged: list[str] = []
    seen: set[str] = set()
    depth = max((len(group) for group in groups), default=0)
    for index in range(depth):
        for group in groups:
            if index >= len(group):
                continue
            value = group[index]
            key = _structured_value_key(value)
            if (
                not value
                or not key
                or key in seen
                or key in (excluded_keys or set())
            ):
                continue
            seen.add(key)
            merged.append(value)
            if len(merged) >= limit:
                return merged
    return merged


def _merge_text_values(values: list[str], *, limit: int) -> str:
    merged = ""
    for value in values:
        candidate = value if not merged else f"{merged}；{value}"
        if len(candidate) > limit:
            break
        merged = candidate
    return merged[:limit]


def _structured_value_key(value: str) -> str:
    return "".join(
        character.casefold()
        for character in str(value or "")
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )


def _reject_unsafe_partial_validation(
    *,
    proposals: list[ValidatedCapabilityProposal],
    invalid_calls: list[tuple[object, object, Exception]],
) -> None:
    """Fail closed except for an unneeded invalid Shelf fallback.

    One valid authoritative Shelf proposal can complete a current-Shelf read.
    An additional malformed read-only fallback must not poison that result, but
    this exception never converts semantics, admits side effects, or hides a
    failed dependency. All other invalid proposals retain fail-closed behavior.
    """

    if not invalid_calls:
        return
    bookshelf_owned = any(
        item.capability == "bookshelf_read"
        for item in proposals
    )
    for call, spec, error in invalid_calls:
        capability = str(getattr(spec, "name", "") or "")
        side_effect = bool(getattr(spec, "side_effect", True))
        depended_on = any(
            str(getattr(call, "call_id", "")) in item.depends_on
            for item in proposals
        )
        if (
            not bookshelf_owned
            or capability not in _BOOKSHELF_FALLBACK_CAPABILITIES
            or side_effect
            or depended_on
        ):
            raise error

_BOOKSHELF_OWNED_MEMORY_PREDICATES = frozenset({
    "reading_status",
    "evaluation",
})


def _apply_authoritative_read_owners(
    proposals: list[ValidatedCapabilityProposal],
) -> list[ValidatedCapabilityProposal]:
    """Merge structurally redundant reads after model interpretation.

    The Controller may split one current-Shelf question into multiple calls or
    also request its derived Memory facts. This boundary does not reinterpret
    user text: it applies the declared read-owner contract only to validated
    canonical filters, scope and predicate fields. Historical Memory reads and
    unrelated predicates remain independent capabilities.
    """

    bookshelf_reads = [
        item
        for item in proposals
        if item.capability == "bookshelf_read"
    ]
    if not bookshelf_reads:
        return proposals

    primary = bookshelf_reads[0]
    duplicate_bookshelf_ids = {
        item.call_id for item in bookshelf_reads[1:]
    }
    if duplicate_bookshelf_ids:
        queries = list(dict.fromkeys(
            str(item.arguments.get("query") or "").strip()
            for item in bookshelf_reads
            if str(item.arguments.get("query") or "").strip()
        ))
        arguments = {
            "scope": "current",
            "statuses": list(dict.fromkeys(
                status
                for item in bookshelf_reads
                for status in item.arguments.get("statuses", [])
            )),
            "evaluations": list(dict.fromkeys(
                evaluation
                for item in bookshelf_reads
                for evaluation in item.arguments.get("evaluations", [])
            )),
            "query": queries[0] if len(queries) == 1 else "",
            "limit": max(
                int(item.arguments.get("limit") or 5000)
                for item in bookshelf_reads
            ),
        }
        primary = primary.model_copy(
            update={
                "arguments": arguments,
                "depends_on": list(dict.fromkeys(
                    dependency
                    for item in bookshelf_reads
                    for dependency in item.depends_on
                )),
            }
        )

    redundant_memory_ids = {
        item.call_id
        for item in proposals
        if item.capability == "search_memory"
        and item.arguments.get("scope") == "current"
        and (
            not str(item.arguments.get("predicate") or "").strip()
            or str(item.arguments.get("predicate") or "").strip().casefold()
            in _BOOKSHELF_OWNED_MEMORY_PREDICATES
        )
    }
    suppressed = duplicate_bookshelf_ids | redundant_memory_ids
    if not suppressed:
        return proposals

    normalized: list[ValidatedCapabilityProposal] = []
    for item in proposals:
        if item.call_id in suppressed:
            continue
        candidate = primary if item.call_id == primary.call_id else item
        dependencies = list(dict.fromkeys(
            primary.call_id if dependency in suppressed else dependency
            for dependency in candidate.depends_on
            if dependency != candidate.call_id
        ))
        dependencies = [
            dependency
            for dependency in dependencies
            if dependency != candidate.call_id
        ]
        normalized.append(
            candidate.model_copy(update={"depends_on": dependencies})
        )
    return normalized
