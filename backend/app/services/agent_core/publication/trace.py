from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import chat as chat_crud
from app.crud.trace import get_dag_by_request_id, upsert_trace
from app.schemas.chat import ChatMessage, UserInput
from app.schemas.trace import ExecutionDag, StepOutput
from app.services.agent_runtime.execution_graph import ExecutionGraph
from app.services.execution_progress import (
    CompletedExecutionStep,
    ModelTokenUsage,
)


async def persist_publication_trace(
    db: AsyncSession,
    *,
    user_input: UserInput,
    message: ChatMessage,
    graph: ExecutionGraph,
    model_name: str,
) -> None:
    """Persist the authoritative turn trace and provider-reported usage."""

    if user_input.thread_id is None:
        raise ValueError("publication trace requires a thread_id")

    progress_steps, usage, business_type = _progress_from_message(message)
    progress_by_action = {
        item.action_id: item
        for item in progress_steps
        if item.action_id is not None
    }
    graph_nodes = {node.node_id: node for node in graph.nodes}
    steps: list[StepOutput] = [
        StepOutput(
            step_number=1,
            message_type="human",
            content=user_input.content,
        )
    ]
    for node in sorted(graph.nodes, key=lambda item: item.order):
        if node.kind != "action":
            continue
        action_id = _raw_action_id(node.action_id)
        progress = progress_by_action.get(action_id)
        dependencies = [
            _raw_action_id(graph_nodes[edge.source_id].action_id)
            for edge in graph.edges
            if edge.target_id == node.node_id
            and edge.relation == "dependency"
            and graph_nodes[edge.source_id].action_id is not None
        ]
        steps.append(
            StepOutput(
                step_number=node.step_number,
                message_type="tool",
                content=None,
                tool_name=node.operation or node.label,
                tool_args=node.metadata.get("tool_args"),
                tool_output=progress.detail if progress is not None else None,
                tool_call_id=node.action_id,
                system_executed=bool(node.metadata.get("system_executed")),
                tool_status=node.status,
                tool_error=progress.error if progress is not None else None,
                latency_ms=(progress.duration_ms if progress is not None else None),
                action_id=node.action_id,
                depends_on=dependencies,
            )
        )
    steps.append(
        StepOutput(
            step_number=len(graph.nodes),
            message_type="ai",
            content=message.content,
            model_name=model_name or None,
        )
    )
    dag = ExecutionDag(
        thread_id=str(user_input.thread_id),
        nodes=[],
        edges=[],
        total_steps=len(steps),
        steps=steps,
        execution_graph=graph,
        progress_steps=progress_steps,
        usage_summary=usage,
        business_type=business_type,
    )

    previous = await get_dag_by_request_id(db, user_input.request_id)
    previous_usage = _usage_from_dag(previous)
    await upsert_trace(
        db=db,
        thread_id=user_input.thread_id,
        request_id=user_input.request_id,
        dag_data=dag.model_dump(mode="json"),
        total_steps=len(steps),
        model_name=model_name or None,
    )
    delta = ModelTokenUsage(
        input_tokens=max(0, usage.input_tokens - previous_usage.input_tokens),
        output_tokens=max(0, usage.output_tokens - previous_usage.output_tokens),
        reasoning_tokens=max(0, usage.reasoning_tokens - previous_usage.reasoning_tokens),
        cached_tokens=max(0, usage.cached_tokens - previous_usage.cached_tokens),
        total_tokens=max(0, usage.total_tokens - previous_usage.total_tokens),
    )
    if delta.total_tokens > 0:
        await chat_crud.update_conversation_tokens(
            db=db,
            thread_id=user_input.thread_id,
            input_tokens=delta.input_tokens,
            output_tokens=delta.output_tokens,
            total_tokens=delta.total_tokens,
        )


def _progress_from_message(
    message: ChatMessage,
) -> tuple[list[CompletedExecutionStep], ModelTokenUsage, str]:
    payload = message.custom_data.get("execution_progress")
    if not isinstance(payload, dict):
        return [], ModelTokenUsage(), "chat"
    progress_steps: list[CompletedExecutionStep] = []
    for raw in payload.get("steps", []):
        try:
            progress_steps.append(CompletedExecutionStep.model_validate(raw))
        except (TypeError, ValueError):
            continue
    try:
        usage = ModelTokenUsage.model_validate(payload.get("usage_summary") or {})
    except (TypeError, ValueError):
        usage = ModelTokenUsage()
    business_type = str(payload.get("business_type") or "chat").strip() or "chat"
    return progress_steps, usage, business_type


def _usage_from_dag(dag: dict[str, Any] | None) -> ModelTokenUsage:
    if not isinstance(dag, dict):
        return ModelTokenUsage()
    try:
        return ModelTokenUsage.model_validate(dag.get("usage_summary") or {})
    except (TypeError, ValueError):
        return ModelTokenUsage()


def _raw_action_id(action_id: str | None) -> str:
    value = str(action_id or "")
    return value.split(":", 1)[-1]


__all__ = ["persist_publication_trace"]
