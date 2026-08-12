from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent_core.context_assembler import (
    ContextAssembler,
    ContextAssemblyResult,
    ContextCompressionRequired,
    ConversationContextLoader,
)
from app.services.agent_core.prompt_contracts import (
    TrustedMemoryContext,
    TrustedReceiptContext,
    TrustedTaskContext,
    TrustedWorkingStateContext,
)
from app.services.conversation.summary_contracts import SummaryProvider
from app.services.conversation.summary_service import ConversationSummaryService


logger = logging.getLogger(__name__)


class ContextPreparationError(RuntimeError):
    pass


class ControllerContextCoordinator:
    """Coordinate derived summary creation; projection remains in collaborators."""

    def __init__(
        self,
        *,
        loader: ConversationContextLoader | None = None,
        assembler: ContextAssembler | None = None,
        summaries: ConversationSummaryService | None = None,
        max_summary_rounds: int = 8,
    ) -> None:
        self._loader = loader or ConversationContextLoader()
        self._assembler = assembler or ContextAssembler()
        self._summaries = summaries or ConversationSummaryService()
        self._max_summary_rounds = max(1, max_summary_rounds)

    async def prepare(
        self,
        db: AsyncSession,
        *,
        user_id: UUID,
        thread_id: UUID,
        current_request_id: str,
        current_user_message: str,
        summary_provider: SummaryProvider,
        memories: list[TrustedMemoryContext] | None = None,
        receipts: list[TrustedReceiptContext] | None = None,
        working_state: TrustedWorkingStateContext | None = None,
        task: TrustedTaskContext | None = None,
        through_sequence: int | None = None,
    ) -> ContextAssemblyResult:
        for _round in range(self._max_summary_rounds + 1):
            material = await self._loader.load(
                db,
                user_id=user_id,
                thread_id=thread_id,
                exclude_request_id=current_request_id,
                through_sequence=through_sequence,
            )
            try:
                return self._assembler.assemble(
                    material,
                    current_user_message=current_user_message,
                    memories=memories,
                    receipts=receipts,
                    working_state=working_state,
                    task=task,
                )
            except ContextCompressionRequired as required:
                if _round >= self._max_summary_rounds:
                    return self._degraded(
                        material,
                        reason="compression_rounds_exhausted",
                        current_user_message=current_user_message,
                        memories=memories,
                        receipts=receipts,
                        working_state=working_state,
                        task=task,
                    )
                try:
                    await self._summaries.summarize_next(
                        db,
                        user_id=user_id,
                        thread_id=thread_id,
                        provider=summary_provider,
                        through_sequence=required.through_sequence,
                        source_upper_bound=through_sequence,
                    )
                except Exception as exc:
                    logger.exception(
                        "conversation summary failed for user=%s thread=%s "
                        "through=%s; degrading to recent exact window",
                        user_id,
                        thread_id,
                        required.through_sequence,
                    )
                    return self._degraded(
                        material,
                        reason=f"summary_failed:{exc.__class__.__name__}",
                        current_user_message=current_user_message,
                        memories=memories,
                        receipts=receipts,
                        working_state=working_state,
                        task=task,
                    )
        raise ContextPreparationError("context preparation did not converge")

    def _degraded(
        self,
        material,
        *,
        reason: str,
        current_user_message: str,
        memories: list[TrustedMemoryContext] | None = None,
        receipts: list[TrustedReceiptContext] | None = None,
        working_state: TrustedWorkingStateContext | None = None,
        task: TrustedTaskContext | None = None,
    ) -> ContextAssemblyResult:
        """Assemble the most recent exact window without a summary."""

        return self._assembler.assemble(
            material,
            current_user_message=current_user_message,
            memories=memories,
            receipts=receipts,
            working_state=working_state,
            task=task,
            allow_truncation=True,
            degradation_reason=reason,
        )


__all__ = ["ContextPreparationError", "ControllerContextCoordinator"]
