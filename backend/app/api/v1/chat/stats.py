"""Trace-backed token statistics for conversations and global views."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_db
from app.crud.chat import read_conversation_by_thread_id
from app.crud.trace import list_usage_trace_rows
from app.schemas.chat import (
    ConversationInDB,
    ConversationTokenStatsResponse,
    DailyStatsItem,
    TokenUsageAggregateItem,
)
from app.services.execution_progress import ModelTokenUsage


api_router = APIRouter(tags=["Chat"])


@api_router.get("/stats/daily", response_model=list[DailyStatsItem])
async def get_daily_stats(
    days: int = Query(30, ge=1, le=365),
    user_id: UUID | None = Query(None, description="Optional user scope filter"),
    db: AsyncSession = Depends(get_db),
) -> list[DailyStatsItem]:
    """Aggregate usage by the actual persisted turn date."""

    rows = await list_usage_trace_rows(db, days=days, user_id=user_id)
    aggregates = _aggregate_usage(rows, dimension="day")
    return [
        DailyStatsItem(
            date=item.key,
            conversation_count=item.conversation_count,
            turn_count=item.turn_count,
            model_call_count=item.model_call_count,
            input_tokens=item.input_tokens,
            output_tokens=item.output_tokens,
            reasoning_tokens=item.reasoning_tokens,
            cached_tokens=item.cached_tokens,
            total_tokens=item.total_tokens,
        )
        for item in aggregates
    ]


@api_router.get("/stats/usage", response_model=list[TokenUsageAggregateItem])
async def get_usage_stats(
    days: int = Query(30, ge=1, le=365),
    group_by: Literal["day", "model", "business"] = Query("day"),
    user_id: UUID | None = Query(None, description="Optional user scope filter"),
    db: AsyncSession = Depends(get_db),
) -> list[TokenUsageAggregateItem]:
    """Aggregate Provider-reported usage by date, model, or business type."""

    rows = await list_usage_trace_rows(db, days=days, user_id=user_id)
    return _aggregate_usage(rows, dimension=group_by)


@api_router.get(
    "/conversations/{thread_id}/stats",
    response_model=ConversationTokenStatsResponse,
)
async def get_conversation_stats(
    thread_id: UUID,
    user_id: UUID = Query(..., description="User ID who owns this conversation"),
    db: AsyncSession = Depends(get_db),
) -> ConversationTokenStatsResponse:
    """Return conversation usage accumulated from persisted turn traces."""

    conversation = await read_conversation_by_thread_id(
        db=db,
        thread_id=thread_id,
        user_id=user_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    rows = await list_usage_trace_rows(
        db,
        user_id=user_id,
        thread_id=thread_id,
    )
    usage = ModelTokenUsage()
    model_call_count = 0
    for row in rows:
        dag = row.dag_data if isinstance(row.dag_data, dict) else {}
        usage = usage.plus(_usage_value(dag.get("usage_summary")))
        model_call_count += len(_model_usage_entries(row))
    if not rows or usage.total_tokens <= 0:
        usage = ModelTokenUsage(
            input_tokens=int(conversation.input_tokens or 0),
            output_tokens=int(conversation.output_tokens or 0),
            total_tokens=int(conversation.total_tokens or 0),
        )

    payload = ConversationInDB.model_validate(conversation).model_dump()
    payload.update(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cached_tokens=usage.cached_tokens,
        turn_count=len(rows),
        model_call_count=model_call_count,
    )
    return ConversationTokenStatsResponse.model_validate(payload)


def _aggregate_usage(rows, *, dimension: Literal["day", "model", "business"]):
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        dag = row.dag_data if isinstance(row.dag_data, dict) else {}
        entries = _model_usage_entries(row)
        if not entries:
            entries = [(str(row.model_name or "unreported"), ModelTokenUsage(), False)]
        for model_name, usage, is_model_call in entries:
            if dimension == "day":
                key = row.created_at.date().isoformat()
            elif dimension == "model":
                key = model_name
            else:
                key = str(dag.get("business_type") or "chat")
            bucket = buckets.setdefault(
                key,
                {
                    "conversations": set(),
                    "turns": set(),
                    "model_call_count": 0,
                    "usage": ModelTokenUsage(),
                },
            )
            bucket["conversations"].add(str(row.thread_id))
            bucket["turns"].add(str(row.request_id))
            if is_model_call:
                bucket["model_call_count"] += 1
            bucket["usage"] = bucket["usage"].plus(usage)

    return [
        TokenUsageAggregateItem(
            dimension=dimension,
            key=key,
            conversation_count=len(bucket["conversations"]),
            turn_count=len(bucket["turns"]),
            model_call_count=bucket["model_call_count"],
            **bucket["usage"].model_dump(),
        )
        for key, bucket in sorted(buckets.items())
    ]


def _model_usage_entries(row) -> list[tuple[str, ModelTokenUsage, bool]]:
    dag = row.dag_data if isinstance(row.dag_data, dict) else {}
    entries: list[tuple[str, ModelTokenUsage, bool]] = []
    steps = dag.get("progress_steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict) or step.get("kind") != "model":
                continue
            entries.append(
                (
                    str(step.get("model_name") or row.model_name or "unreported"),
                    _usage_value(step.get("usage")),
                    True,
                )
            )
    if entries:
        return entries
    summary = _usage_value(dag.get("usage_summary"))
    if summary.total_tokens > 0:
        return [(str(row.model_name or "unreported"), summary, True)]
    return []


def _usage_value(raw: Any) -> ModelTokenUsage:
    try:
        return ModelTokenUsage.model_validate(raw or {})
    except (TypeError, ValueError):
        return ModelTokenUsage()
