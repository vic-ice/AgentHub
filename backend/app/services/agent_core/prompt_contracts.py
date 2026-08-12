from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.services.agent_core.contracts import AgentCoreModel


class ConversationContextTurn(AgentCoreModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=32_000)


class TrustedMemoryContext(AgentCoreModel):
    schema_key: str = Field(min_length=1, max_length=128)
    fact: str = Field(min_length=1, max_length=2_000)
    source: Literal["user_journal"] = "user_journal"


class TrustedReceiptSource(AgentCoreModel):
    title: str = Field(default="", max_length=300)
    url: str = Field(min_length=1, max_length=2_000)
    snippet: str = Field(default="", max_length=1_000)
    published_date: str = Field(default="", max_length=64)


class TrustedReceiptContext(AgentCoreModel):
    capability: str = Field(min_length=1, max_length=128)
    status: Literal[
        "completed",
        "failed",
        "partial",
        "blocked",
        "skipped",
        "waiting",
    ]
    summary: str = Field(min_length=1, max_length=2_000)
    result_mode: str = Field(default="", max_length=128)
    facts: list[str] = Field(default_factory=list, max_length=20)
    sources: list[TrustedReceiptSource] = Field(
        default_factory=list,
        max_length=20,
    )
    limitations: list[str] = Field(default_factory=list, max_length=10)


class TrustedTaskContext(AgentCoreModel):
    status: Literal[
        "planning",
        "running",
        "waiting_clarification",
        "failed",
        "completed",
    ]
    goal: str = Field(min_length=1, max_length=4_000)
    next_step: str | None = Field(default=None, max_length=2_000)


class TrustedResearchRunContext(AgentCoreModel):
    """Compact pointer to one session research run.

    Chat only carries the existence and the rough conclusion of a research
    run; all details are fetched on demand through research_read.
    """

    run_id: str = Field(min_length=1, max_length=64)
    objective: str = Field(min_length=1, max_length=300)
    status: str = Field(default="completed", max_length=32)
    conclusion: str = Field(default="", max_length=300)
    evidence_count: int = Field(default=0, ge=0)
    created_at: str = Field(default="", max_length=64)


class TrustedWorkingStateContext(AgentCoreModel):
    last_turn_status: Literal[
        "empty",
        "incomplete",
        "completed",
        "waiting_clarification",
        "failed",
    ]
    active_goal: str | None = Field(default=None, max_length=4_000)
    pending_question: str | None = Field(default=None, max_length=4_000)


class TrustedResearchFact(AgentCoreModel):
    content: str = Field(min_length=1, max_length=320)
    source: str = Field(default="", max_length=2_000)
    confidence: Literal["high", "medium", "low"] = "medium"
    step: int = Field(default=0, ge=0)


class TrustedResearchStateContext(AgentCoreModel):
    """Bounded evolving research workspace for the Controller.

    Facts are leads rebuilt from trusted receipts, not verified conclusions.
    Low-confidence or single-source facts remain unverified until
    independently corroborated.
    """

    objective: str = Field(min_length=1, max_length=4_000)
    status: str = Field(default="running", max_length=64)
    step_count: int = Field(default=0, ge=0)
    confirmed_facts: list[TrustedResearchFact] = Field(
        default_factory=list,
        max_length=20,
    )
    open_questions: list[str] = Field(default_factory=list, max_length=10)
    information_gaps: list[str] = Field(default_factory=list, max_length=10)
    current_focus: str = Field(default="", max_length=1_000)
    exhausted_queries: list[str] = Field(default_factory=list, max_length=8)
    conflicts: list[str] = Field(default_factory=list, max_length=5)


class TrustedConversationSummary(AgentCoreModel):
    user_requests: list[str] = Field(default_factory=list, max_length=100)
    user_statements: list[str] = Field(default_factory=list, max_length=100)
    decisions: list[str] = Field(default_factory=list, max_length=100)
    corrections: list[str] = Field(default_factory=list, max_length=100)
    unresolved: list[str] = Field(default_factory=list, max_length=100)
    active_tasks: list[str] = Field(default_factory=list, max_length=100)
    receipt_refs: list[str] = Field(default_factory=list, max_length=128)


class ControllerContextSnapshot(AgentCoreModel):
    conversation_summary: TrustedConversationSummary | None = None
    conversation: list[ConversationContextTurn] = Field(
        default_factory=list,
        max_length=100,
    )
    memories: list[TrustedMemoryContext] = Field(
        default_factory=list,
        max_length=50,
    )
    receipts: list[TrustedReceiptContext] = Field(
        default_factory=list,
        max_length=32,
    )
    working_state: TrustedWorkingStateContext | None = None
    research_runs: list[TrustedResearchRunContext] = Field(
        default_factory=list,
        max_length=10,
    )
    task: TrustedTaskContext | None = None
    trusted_research_state: TrustedResearchStateContext | None = None
    context_visibility_note: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "Honest statement about context visibility limits for this turn, "
            "e.g. when older journal content was truncated because the summary "
            "service was unavailable."
        ),
    )


class ControllerModelRequest(AgentCoreModel):
    model_name: str = Field(min_length=1, max_length=256)
    current_user_message: str = Field(min_length=1, max_length=32_000)
    context: ControllerContextSnapshot = Field(
        default_factory=ControllerContextSnapshot
    )
    timeout_seconds: float = Field(default=60, ge=1, le=180)


__all__ = [
    "ControllerContextSnapshot",
    "ControllerModelRequest",
    "ConversationContextTurn",
    "TrustedConversationSummary",
    "TrustedMemoryContext",
    "TrustedReceiptContext",
    "TrustedResearchRunContext",
    "TrustedReceiptSource",
    "TrustedResearchFact",
    "TrustedResearchStateContext",
    "TrustedTaskContext",
    "TrustedWorkingStateContext",
]
