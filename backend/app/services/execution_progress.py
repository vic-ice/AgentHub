from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


StepKind = Literal["model", "action", "research"]
StepStatus = Literal["completed", "failed", "blocked", "skipped", "waiting"]


class ModelTokenUsage(BaseModel):
    """Provider-reported usage for one completed model call."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    def plus(self, other: "ModelTokenUsage") -> "ModelTokenUsage":
        return ModelTokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class CompletedExecutionStep(BaseModel):
    """A compact, already-completed execution fact suitable for UI progress."""

    step_id: str = Field(min_length=1, max_length=160)
    order: int = Field(ge=1)
    kind: StepKind
    status: StepStatus
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=1000)
    business_type: str = Field(default="chat", min_length=1, max_length=64)
    model_name: str | None = Field(default=None, max_length=256)
    action_id: str | None = Field(default=None, max_length=256)
    operation: str | None = Field(default=None, max_length=256)
    duration_ms: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=500)
    usage: ModelTokenUsage | None = None


ProgressCallback = Callable[[CompletedExecutionStep], Awaitable[None] | None]


class ExecutionProgressCollector:
    """Request-local collection of completed steps and provider usage."""

    def __init__(
        self,
        *,
        business_type: str,
        callback: ProgressCallback | None = None,
    ) -> None:
        self.business_type = str(business_type or "chat").strip() or "chat"
        self.callback = callback
        self.steps: list[CompletedExecutionStep] = []

    async def complete(
        self,
        *,
        kind: StepKind,
        status: StepStatus,
        title: str,
        detail: str = "",
        step_id: str | None = None,
        model_name: str | None = None,
        action_id: str | None = None,
        operation: str | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
        usage: ModelTokenUsage | None = None,
    ) -> CompletedExecutionStep:
        order = len(self.steps) + 1
        resolved_id = str(step_id or f"{kind}-{order}").strip()
        if any(item.step_id == resolved_id for item in self.steps):
            resolved_id = f"{resolved_id}-{order}"
        step = CompletedExecutionStep(
            step_id=resolved_id,
            order=order,
            kind=kind,
            status=status,
            title=str(title or "Completed step").strip()[:200],
            detail=str(detail or "").strip()[:1000],
            business_type=self.business_type,
            model_name=_optional_text(model_name, 256),
            action_id=_optional_text(action_id, 256),
            operation=_optional_text(operation, 256),
            duration_ms=max(0, int(duration_ms)) if duration_ms is not None else None,
            error=_optional_text(error, 500),
            usage=usage,
        )
        self.steps.append(step)
        if self.callback is not None:
            try:
                result = self.callback(step)
                if result is not None:
                    await result
            except Exception:
                logger.exception("Execution progress callback failed open")
        return step

    def usage_summary(self) -> ModelTokenUsage:
        total = ModelTokenUsage()
        for step in self.steps:
            if step.usage is not None:
                total = total.plus(step.usage)
        return total


_collector_var: ContextVar[ExecutionProgressCollector | None] = ContextVar(
    "execution_progress_collector",
    default=None,
)


@contextmanager
def bind_execution_progress(collector: ExecutionProgressCollector):
    token = _collector_var.set(collector)
    try:
        yield collector
    finally:
        _collector_var.reset(token)


def current_execution_progress() -> ExecutionProgressCollector | None:
    return _collector_var.get()


async def report_completed_step(**kwargs: Any) -> CompletedExecutionStep | None:
    collector = current_execution_progress()
    if collector is None:
        return None
    return await collector.complete(**kwargs)


async def report_model_completion(
    response: Any,
    *,
    title: str,
    detail: str,
    model_name: str,
    duration_ms: int,
    status: StepStatus = "completed",
    error: str | None = None,
) -> CompletedExecutionStep | None:
    return await report_completed_step(
        kind="model",
        status=status,
        title=title,
        detail=detail,
        model_name=model_name,
        duration_ms=duration_ms,
        error=error,
        usage=extract_model_usage(response),
    )


def extract_model_usage(message: Any) -> ModelTokenUsage:
    """Normalize Provider usage metadata without estimating model tokens."""

    if message is None:
        return ModelTokenUsage()
    candidates: list[dict[str, Any]] = []
    usage_metadata = getattr(message, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        candidates.append(usage_metadata)
    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        for key in ("token_usage", "usage"):
            value = response_metadata.get(key)
            if isinstance(value, dict):
                candidates.append(value)
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict):
        value = additional_kwargs.get("usage")
        if isinstance(value, dict):
            candidates.append(value)
    if not candidates:
        return ModelTokenUsage()

    input_tokens = max(
        _int_value(raw, "input_tokens", "prompt_tokens")
        for raw in candidates
    )
    output_tokens = max(
        _int_value(raw, "output_tokens", "completion_tokens")
        for raw in candidates
    )
    total_tokens = max(_int_value(raw, "total_tokens") for raw in candidates)
    input_details = [
        _mapping_value(raw, "input_token_details", "prompt_tokens_details")
        for raw in candidates
    ]
    output_details = [
        _mapping_value(raw, "output_token_details", "completion_tokens_details")
        for raw in candidates
    ]
    cached_tokens = max(
        [
            _int_value(
                raw,
                "cached_tokens",
                "cache_read",
                "cache_read_input_tokens",
            )
            for raw in candidates
        ]
        + [
            _int_value(details, "cache_read", "cached_tokens")
            for details in input_details
        ]
    )
    reasoning_tokens = max(
        [_int_value(raw, "reasoning_tokens") for raw in candidates]
        + [
            _int_value(details, "reasoning", "reasoning_tokens")
            for details in output_details
        ]
    )
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    return ModelTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
    )

def attach_progress_to_answer(answer: Any, collector: ExecutionProgressCollector):
    """Attach final progress evidence without changing the public answer schema."""

    custom_data = dict(getattr(answer, "custom_data", {}) or {})
    custom_data["execution_progress"] = {
        "business_type": collector.business_type,
        "steps": [item.model_dump(mode="json") for item in collector.steps],
        "usage_summary": collector.usage_summary().model_dump(mode="json"),
    }
    model_copy = getattr(answer, "model_copy", None)
    if not callable(model_copy):
        raise TypeError("progress can only be attached to a typed published answer")
    return model_copy(update={"custom_data": custom_data})


def summarize_step_result(value: Any) -> str:
    if value is None:
        return "\u6b65\u9aa4\u5df2\u5b8c\u6210"
    if isinstance(value, str):
        return " ".join(value.split())[:500]
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("status", "message", "summary", "conclusion", "count"):
            item = value.get(key)
            if item not in (None, "", [], {}):
                parts.append(f"{key}: {item}")
        if parts:
            return "; ".join(parts)[:500]
        return f"\u8fd4\u56de {len(value)} \u4e2a\u5b57\u6bb5"
    if isinstance(value, (list, tuple, set)):
        return f"\u8fd4\u56de {len(value)} \u9879\u7ed3\u679c"
    return " ".join(str(value).split())[:500]


def _mapping_value(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        item = value.get(key)
        if isinstance(item, dict):
            return item
    return {}


def _int_value(value: dict[str, Any], *keys: str) -> int:
    for key in keys:
        item = value.get(key)
        if isinstance(item, bool):
            continue
        try:
            if item is not None:
                return max(0, int(item))
        except (TypeError, ValueError):
            continue
    return 0


def _optional_text(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


__all__ = [
    "CompletedExecutionStep",
    "attach_progress_to_answer",
    "ExecutionProgressCollector",
    "ModelTokenUsage",
    "bind_execution_progress",
    "current_execution_progress",
    "extract_model_usage",
    "report_completed_step",
    "report_model_completion",
    "summarize_step_result",
]
