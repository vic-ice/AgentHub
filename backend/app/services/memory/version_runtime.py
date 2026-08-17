from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from app.infra.database import get_database
from app.schemas.chat import UserInput
from app.services.agent_runtime.contracts import ExecutionContext
from app.services.conversation import ConversationEventRepository
from app.services.memory.canonicalizer import MemoryCanonicalizer
from app.services.memory.version_contracts import (
    ForgetMemoryRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
)
from app.services.memory.vector_recall import recall_memory_keys
from app.services.memory.version_search import VersionedMemorySearch
from app.services.memory.read_gateway import MemoryReadGateway
from app.services.memory.write_gate import (
    validate_forget_write,
    validate_remember_write,
)


async def execute_remember_memory(
    arguments: dict[str, Any],
    *,
    action_id: str,
    context: ExecutionContext,
    user_input: UserInput | None,
) -> dict[str, Any]:
    request = RememberMemoryRequest.model_validate(arguments)
    source_text = _source_text(context=context, user_input=user_input)
    ok, reason = validate_remember_write(
        source_text,
        [item.model_dump(mode="json") for item in request.assertions],
    )
    if not ok:
        return {"status": "rejected", "reason": reason}
    from app.services.memory.turn_compiler import compiled_turn_from_assertions
    from app.services.memory.write_gateway import MemoryWriteGateway

    compiled = compiled_turn_from_assertions(
        request.assertions,
        raw_text=source_text,
    )
    source_event_id, database = await _source_event_id(context)
    if source_event_id is None:
        return _missing_source_result()
    async with database.session() as session:
        gateway = MemoryWriteGateway(session)
        outcome = await gateway.commit_compiled(
            compiled=compiled,
            user_id=context.user_id,
            thread_id=context.thread_id,
            source_event_id=source_event_id,
            receipt_id=_receipt_id(context, action_id),
        )
    return outcome



async def execute_search_memory(
    arguments: dict[str, Any],
    *,
    context: ExecutionContext,
) -> dict[str, Any]:
    request = SearchMemoryRequest.model_validate(arguments)
    semantic_memory_keys = ()
    if request.scope == "current" and request.query:
        semantic_memory_keys = await recall_memory_keys(
            user_id=context.user_id,
            query=request.query,
        )
    database = get_database()
    async with database.session() as session:
        reader = MemoryReadGateway(session)
        if request.scope == "current":
            records = await reader.current_versions(
                user_id=context.user_id,
                limit=500,
            )
            if semantic_memory_keys:
                records = _merge_records(
                    records,
                    await reader.current_versions_by_keys(
                        user_id=context.user_id,
                        memory_keys=semantic_memory_keys,
                    ),
                )
        else:
            records = await reader.history_versions(
                user_id=context.user_id,
                limit=500,
            )
    return VersionedMemorySearch().search(
        records,
        request,
        semantic_memory_keys=semantic_memory_keys,
    ).model_dump(mode="json")


async def execute_forget_memory(
    arguments: dict[str, Any],
    *,
    action_id: str,
    context: ExecutionContext,
    user_input: UserInput | None,
) -> dict[str, Any]:
    request = ForgetMemoryRequest.model_validate(arguments)
    source_text = _source_text(context=context, user_input=user_input)
    ok, reason = validate_forget_write(source_text)
    if not ok:
        return {"status": "rejected", "reason": reason}
    resolution = MemoryCanonicalizer().resolve_targets(
        request.targets,
        source_text=source_text,
    )
    if resolution.status != "ready":
        return resolution.model_dump(mode="json")
    source_event_id, database = await _source_event_id(context)
    if source_event_id is None:
        return _missing_source_result()
    from app.services.memory.write_gateway import MemoryWriteGateway

    async with database.session() as session:
        outcome = await MemoryWriteGateway(session).forget(
            user_id=context.user_id,
            thread_id=context.thread_id,
            memory_keys=[item.memory_key for item in resolution.targets],
            source_event_id=source_event_id,
            receipt_id=_receipt_id(context, action_id),
            evidence_quote=source_text,
        )
    return dict(outcome["receipt"])


async def _source_event_id(
    context: ExecutionContext,
) -> tuple[UUID | None, Any]:
    database = get_database()
    if context.thread_id is None:
        return None, database
    async with database.session() as session:
        source_event = await ConversationEventRepository().get_request_event(
            session,
            user_id=context.user_id,
            thread_id=context.thread_id,
            request_id=context.request_id,
            role="user",
        )
    return (
        source_event.id if source_event is not None else None,
        database,
    )


def _source_text(
    *,
    context: ExecutionContext,
    user_input: UserInput | None,
) -> str:
    return (
        user_input.content
        if user_input is not None
        else str(context.metadata.get("user_message") or "")
    )


def _receipt_id(context: ExecutionContext, action_id: str) -> str:
    return "memory-" + hashlib.sha256(
        f"{context.request_id}:{action_id}".encode("utf-8")
    ).hexdigest()[:40]


def _missing_source_result() -> dict[str, Any]:
    return {
        "result_mode": "memory_mutation_receipt",
        "status": "failed",
        "error": "source user event is not committed",
    }


def _merge_records(primary: list[Any], extra: list[Any]) -> list[Any]:
    seen = {getattr(item, "id", None) for item in primary}
    merged = list(primary)
    for item in extra:
        item_id = getattr(item, "id", None)
        if item_id in seen:
            continue
        seen.add(item_id)
        merged.append(item)
    return merged
