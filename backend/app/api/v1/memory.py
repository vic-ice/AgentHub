"""Version-chain memory management endpoints."""

import hashlib
import json
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
            for head in heads
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
    records = await reader.history_versions(user_id=user_id, limit=1000)
    versions = [
        record
        for record in records
        if record.memory_key == memory_key
    ]
    if not versions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory fact not found",
        )
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
        schema_key=fact.schema_key,
        memory_key=fact.memory_key,
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
        schema_key=None,
        memory_key=None,
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
    return (
        f"用户在记忆面板{action}：predicate={predicate} "
        f"value={json.dumps(value, ensure_ascii=False)}"
    )


def _admin_receipt_id(user_id: UUID, action: str, event_id: UUID) -> str:
    return "admin-" + hashlib.sha256(
        f"{user_id}:{action}:{event_id}".encode("utf-8")
    ).hexdigest()[:40]


def _to_admin_fact(record: MemoryVersionRecord) -> MemoryAdminFact:
    fallback_domain, fallback_kind = derive_domain_kind(
        "", record.subject, record.schema_key
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
        domain=str(record.value.get("domain") or fallback_domain),
        kind=str(record.value.get("kind") or fallback_kind),
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
