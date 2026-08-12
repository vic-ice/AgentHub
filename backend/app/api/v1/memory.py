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
from app.services.memory.version_contracts import (
    ForgetMemoryTargetProposal,
    MemoryAssertionProposal,
    MemoryMutationReceipt,
    MemoryVersionCommitCommand,
    MemoryVersionForgetCommand,
    MemoryVersionRecord,
)
from app.services.memory.version_store import MemoryVersionStore

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

    store = MemoryVersionStore(db)
    heads = await store.list_current(user_id=user_id, limit=500)
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

    store = MemoryVersionStore(db)
    records = await store.list_history(user_id=user_id, limit=1000)
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
    store = MemoryVersionStore(db)
    heads = await store.list_current(user_id=request.user_id, limit=500)
    head = next(
        (item for item in heads if item.memory_key == fact.memory_key),
        None,
    )
    event = MemoryManagementEvent(
        user_id=request.user_id,
        thread_id=request.thread_id,
        action="correct" if head is not None else "create",
        schema_key=fact.schema_key,
        memory_key=fact.memory_key,
        subject="self",
        predicate=request.predicate,
        value=request.value,
        qualifiers=request.qualifiers,
        evidence_quote=evidence,
        previous_value=head.value if head is not None else None,
    )
    db.add(event)
    await db.flush()
    await db.refresh(event)

    receipt = await store.commit(
        MemoryVersionCommitCommand(
            facts=canonical.facts,
            source_event_id=event.id,
            receipt_id=_admin_receipt_id(request.user_id, "edit", event.id),
        ),
        user_id=request.user_id,
        thread_id=request.thread_id,
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
    resolution = MemoryCanonicalizer().resolve_targets(
        [target],
        source_text=evidence,
    )
    if resolution.status != "ready":
        raise _clarification_error(
            resolution.reason_codes,
            resolution.clarification_question,
        )

    store = MemoryVersionStore(db)
    event = MemoryManagementEvent(
        user_id=request.user_id,
        thread_id=request.thread_id,
        action="forget",
        schema_key=resolution.targets[0].schema_key,
        memory_key=resolution.targets[0].memory_key,
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

    receipt = await store.forget(
        MemoryVersionForgetCommand(
            memory_keys=[
                item.memory_key for item in resolution.targets
            ],
            source_event_id=event.id,
            receipt_id=_admin_receipt_id(request.user_id, "forget", event.id),
            evidence_quote=evidence,
        ),
        user_id=request.user_id,
        thread_id=request.thread_id,
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
