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
        return CapabilityProposalBatch(
            proposals=_apply_authoritative_read_owners(proposals)
        )


_BOOKSHELF_FALLBACK_CAPABILITIES = frozenset({
    "search_memory",
    "book_search",
})


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
