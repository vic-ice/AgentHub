"""Version-chain memory management endpoints."""

import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_db
from app.models.memory_management_event import MemoryManagementEvent
from app.schemas.memory import (
    MemoryAdminCurrentResponse,
    MemoryAdminFact,
    MemoryAdminHistoryResponse,
    MemoryEditRequest,
    MemoryForgetAdminRequest,
)
from app.services.memory.canonicalizer import MemoryCanonicalizer
from app.services.memory.admin_projection import (
    collapse_current_versions,
    history_versions_for,
    project_memory_record,
)
from app.services.memory.classification import derive_domain_kind
from app.services.memory.version_contracts import (
    ForgetMemoryTargetProposal,
    MemoryAssertionProposal,
    MemoryMutationReceipt,
    MemoryVersionRecord,
)
from app.services.memory.version_errors import MemoryVersionTargetNotFound
from app.services.memory.read_gateway import MemoryReadGateway
from app.services.memory.write_gateway import MemoryWriteGateway

api_router = APIRouter(prefix="/memory", tags=["Memory"])

_SELF_NAME_COMPATIBILITY_KEYS = (
    "personal.fact:name",
    "personal.correction:name",
    "identity.self_reported_name:self",
)


@api_router.get(
    "/{user_id}/current",
    response_model=MemoryAdminCurrentResponse,
)
async def list_current_memories(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> MemoryAdminCurrentResponse:
    """List current canonical facts exactly as the chat memory path sees them."""

    reader = MemoryReadGateway(db)
    heads = await reader.current_versions(user_id=user_id, limit=500)
    return MemoryAdminCurrentResponse(
        facts=[
            _to_admin_fact(head)
            for head in collapse_current_versions(heads)
            if not head.is_tombstone
        ]
    )


@api_router.get(
    "/{user_id}/history",
    response_model=MemoryAdminHistoryResponse,
)
async def memory_history(
    user_id: UUID,
    memory_key: str = Query(min_length=1, max_length=256),
    db: AsyncSession = Depends(get_db),
) -> MemoryAdminHistoryResponse:
    """Return the full version timeline for one fact."""

    reader = MemoryReadGateway(db)
    exact = await reader.history_versions_by_key(
        user_id=user_id,
        memory_key=memory_key,
    )
    if not exact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory fact not found",
        )
    records = exact
    if _to_admin_fact(exact[-1]).presentation_key in {
        "identity.self_reported_name",
        "entity.name",
    }:
        records = await reader.history_versions(user_id=user_id, limit=1000)
    versions = history_versions_for(records, memory_key=memory_key)
    return MemoryAdminHistoryResponse(
        memory_key=memory_key,
        versions=[_to_admin_fact(version) for version in versions],
    )


@api_router.post(
    "/edit",
    response_model=MemoryMutationReceipt,
    status_code=status.HTTP_201_CREATED,
)
async def edit_memory(
    request: MemoryEditRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryMutationReceipt:
    """Create or correct one canonical fact from the memory panel."""

    target_record = None
    if request.memory_key:
        target_record = await _require_manageable_target(
            db,
            user_id=request.user_id,
            memory_key=request.memory_key,
            action="edit",
        )
    evidence = request.evidence_quote or _admin_evidence(
        "更新",
        request.predicate,
        request.value,
    )
    assertion = MemoryAssertionProposal(
        subject="self",
        predicate=request.predicate,
        value=request.value,
        qualifiers=request.qualifiers,
        evidence_quote=evidence,
    )
    canonical = MemoryCanonicalizer().canonicalize(
        [assertion],
        source_text=evidence,
    )
    if canonical.status != "ready":
        raise _clarification_error(
            canonical.reason_codes,
            canonical.clarification_question,
        )

    fact = canonical.facts[0]
    event = MemoryManagementEvent(
        user_id=request.user_id,
        thread_id=request.thread_id,
        action="create",
        schema_key=(target_record.schema_key if target_record else fact.schema_key),
        memory_key=(target_record.memory_key if target_record else fact.memory_key),
        subject="self",
        predicate=request.predicate,
        value=request.value,
        qualifiers=request.qualifiers,
        evidence_quote=evidence,
        previous_value=None,
    )
    db.add(event)
    await db.flush()
    await db.refresh(event)

    gateway = MemoryWriteGateway(db)
    if target_record is not None:
        gateway_result = await gateway.commit_admin_correction(
            fact.model_copy(update={"evidence_quote": evidence}),
            target_memory_key=target_record.memory_key,
            user_id=request.user_id,
            thread_id=request.thread_id,
            source_event_id=event.id,
            receipt_id=_admin_receipt_id(request.user_id, "edit", event.id),
        )
    else:
        gateway_result = await gateway.commit_facts(
            canonical.facts,
            user_id=request.user_id,
            thread_id=request.thread_id,
            source_event_id=event.id,
            receipt_id=_admin_receipt_id(request.user_id, "edit", event.id),
            evidence_quote=evidence,
            source_kind="admin_action",
        )
    if gateway_result.get("status") == "clarification_required":
        raise _clarification_error(
            ["gateway_entity_resolution_ambiguous"],
            gateway_result.get("clarification_question", ""),
        )
    receipt = MemoryMutationReceipt.model_validate(gateway_result["receipt"])
    if receipt.mutations:
        mutation = receipt.mutations[0]
        event.action = "correct" if mutation.status == "revised" else "create"
        event.schema_key = mutation.version.schema_key
        event.memory_key = mutation.memory_key
        event.previous_value = (
            mutation.previous.value if mutation.previous is not None else None
        )
    await db.commit()
    return receipt


@api_router.post("/forget", response_model=MemoryMutationReceipt)
async def forget_memory(
    request: MemoryForgetAdminRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryMutationReceipt:
    """Tombstone one existing fact from the memory panel."""

    target_record = None
    if request.memory_key:
        target_record = await _require_manageable_target(
            db,
            user_id=request.user_id,
            memory_key=request.memory_key,
            action="forget",
        )
    evidence = request.evidence_quote or _admin_evidence(
        "遗忘",
        request.predicate,
        request.identity,
    )
    target = ForgetMemoryTargetProposal(
        subject="self",
        predicate=request.predicate,
        identity=request.identity,
        qualifiers=request.qualifiers,
        evidence_quote=evidence,
    )
    event = MemoryManagementEvent(
        user_id=request.user_id,
        thread_id=request.thread_id,
        action="forget",
        schema_key=(target_record.schema_key if target_record else None),
        memory_key=(target_record.memory_key if target_record else None),
        subject="self",
        predicate=request.predicate,
        value={},
        qualifiers=request.qualifiers,
        evidence_quote=evidence,
        previous_value=None,
    )
    db.add(event)
    await db.flush()
    await db.refresh(event)

    gateway = MemoryWriteGateway(db)
    try:
        if target_record is not None:
            gateway_result = await gateway.forget_admin_target(
                user_id=request.user_id,
                thread_id=request.thread_id,
                target_memory_key=target_record.memory_key,
                source_event_id=event.id,
                receipt_id=_admin_receipt_id(request.user_id, "forget", event.id),
                evidence_quote=evidence,
            )
        else:
            gateway_result = await gateway.forget_targets(
                user_id=request.user_id,
                thread_id=request.thread_id,
                targets=[target],
                source_text=evidence,
                source_event_id=event.id,
                receipt_id=_admin_receipt_id(request.user_id, "forget", event.id),
                source_kind="admin_action",
            )
    except MemoryVersionTargetNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory fact not found",
        ) from exc
    if gateway_result.get("status") == "clarification_required":
        raise _clarification_error(
            ["gateway_target_resolution_failed"],
            gateway_result.get("clarification_question", ""),
        )
    if gateway_result.get("status") == "rejected":
        raise _clarification_error(
            [str(gateway_result.get("reason") or "gateway_target_rejected")],
            "",
        )
    receipt = MemoryMutationReceipt.model_validate(gateway_result["receipt"])
    if receipt.mutations:
        mutation = receipt.mutations[0]
        event.schema_key = mutation.version.schema_key
        event.memory_key = mutation.memory_key
        event.previous_value = (
            mutation.previous.value if mutation.previous is not None else None
        )
    await db.commit()
    return receipt


def _admin_evidence(action: str, predicate: str, value) -> str:
    del predicate, value
    return f"用户在记忆中心{action}了这条事实。"


async def _require_manageable_target(
    db: AsyncSession,
    *,
    user_id: UUID,
    memory_key: str,
    action: str,
) -> MemoryVersionRecord:
    reader = MemoryReadGateway(db)
    records = await reader.current_versions_by_keys(
        user_id=user_id,
        memory_keys=[memory_key],
    )
    target = next(
        (
            record
            for record in records
            if record.memory_key == memory_key and not record.is_tombstone
        ),
        None,
    )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory fact not found",
        )
    presentation = _to_admin_fact(target)
    allowed = presentation.can_edit if action == "edit" else presentation.can_forget
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this memory is managed by its owning feature"
                if presentation.category_key == "reading"
                else f"memory fact cannot be {action}ed"
            ),
        )
    return target


def _admin_receipt_id(user_id: UUID, action: str, event_id: UUID) -> str:
    return "admin-" + hashlib.sha256(
        f"{user_id}:{action}:{event_id}".encode("utf-8")
    ).hexdigest()[:40]


def _to_admin_fact(record: MemoryVersionRecord) -> MemoryAdminFact:
    fallback_domain, fallback_kind = derive_domain_kind(
        "", record.subject, record.schema_key
    )
    domain = str(record.value.get("domain") or fallback_domain)
    kind = str(record.value.get("kind") or fallback_kind)
    presentation = project_memory_record(
        record,
        domain=domain,
        kind=kind,
    )
    return MemoryAdminFact(
        schema_key=record.schema_key,
        memory_key=record.memory_key,
        subject=record.subject,
        predicate=record.predicate,
        value=record.value,
        qualifiers=record.qualifiers,
        evidence_quote=record.evidence_quote,
        version_no=record.version_no,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
        is_tombstone=record.is_tombstone,
        domain=domain,
        kind=kind,
        presentation_key=presentation.presentation_key,
        category_key=presentation.category_key,
        category_label=presentation.category_label,
        display_value=presentation.display_value,
        can_edit=presentation.can_edit,
        can_forget=presentation.can_forget,
        show_evidence=presentation.show_evidence,
    )


def _clarification_error(
    reason_codes,
    question: str,
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "reason_codes": list(reason_codes or []),
            "question": str(question or ""),
        },
    )
