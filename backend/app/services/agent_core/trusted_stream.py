from __future__ import annotations

import logging
import asyncio
from collections.abc import AsyncGenerator

from app.schemas.chat import UserInput
from app.services.agent_core.contracts import PublishedAnswer
from app.services.agent_core.publication.graph import (
    project_public_execution_graph,
)
from app.services.agent_core.publication.stream import TrustedStreamSequencer
from app.utils.sse import sse, sse_error
from app.services.execution_progress import (
    CompletedExecutionStep,
    ExecutionProgressCollector,
    attach_progress_to_answer,
    bind_execution_progress,
)


logger = logging.getLogger(__name__)


class TrustedControllerStream:
    """Run the admitted Controller and expose only committed public events."""

    def __init__(
        self,
        *,
        entry=None,
        committer=None,
        database_factory=None,
        journal=None,
        model_resolver=None,
        model_cache_refresher=None,
    ) -> None:
        self._entry = entry
        self._committer = committer
        self._database_factory = database_factory
        self._journal = journal
        self._model_resolver = model_resolver
        self._model_cache_refresher = model_cache_refresher

    async def generate(
        self,
        user_input: UserInput,
    ) -> AsyncGenerator[str, None]:
        sequencer = TrustedStreamSequencer(
            request_id=user_input.request_id
        )
        yield sse(sequencer.turn_started().model_dump(mode="json"))
        progress_queue: asyncio.Queue[CompletedExecutionStep] = asyncio.Queue()
        collector = ExecutionProgressCollector(
            business_type=(
                "research" if user_input.research_mode == "deep_research" else "chat"
            ),
            callback=progress_queue.put_nowait,
        )


        try:
            (
                database,
                journal,
                entry_service,
                committer,
                model_resolver,
                model_cache_refresher,
            ) = self._dependencies()
        except Exception:
            logger.exception(
                "Trusted stream dependencies are unavailable"
            )
            yield sse_error(
                "本轮未能安全开始，请稍后重试。",
                error_type="turn_start_failed",
            )
            yield "data: [DONE]\n\n"
            return
        try:
            async with database.session() as session:
                user_event = await journal.record_user_message(
                    session,
                    user_input,
                )
        except Exception:
            logger.exception(
                "Trusted stream could not commit the user Journal event"
            )
            yield sse_error(
                "本轮未能安全开始，请稍后重试。",
                error_type="turn_start_failed",
            )
            yield "data: [DONE]\n\n"
            return

        if user_input.research_mode == "deep_research":
            async for event in self._run_deep_research_turn(
                sequencer=sequencer,
                database=database,
                user_input=user_input,
                committer=committer,
                collector=collector,
                progress_queue=progress_queue,
            ):
                yield event
            return

        requested_model = (
            user_input.model_uuid or user_input.model_name
        )
        try:
            model_name = model_resolver(requested_model)
            if requested_model and model_name:
                await model_cache_refresher(model_name)
        except Exception:
            logger.exception(
                "Trusted stream model resolution failed"
            )
            async for event in self._publish_failure(
                sequencer=sequencer,
                user_input=user_input,
                message="当前模型暂不可用，请稍后重试。",
                model_name="",
                database=database,
                committer=committer,
            ):
                yield event
            return
        if not model_name:
            async for event in self._publish_failure(
                sequencer=sequencer,
                user_input=user_input,
                message="当前没有可用的模型，请检查模型配置。",
                model_name="",
                database=database,
                committer=committer,
            ):
                yield event
            return
        if user_input.model_name != model_name:
            user_input = user_input.model_copy(
                update={"model_name": model_name}
            )

        try:
            async with database.session() as session:
                with bind_execution_progress(collector):
                    task = asyncio.create_task(entry_service.run(
                        session,
                        user_input=user_input,
                        model_name=model_name,
                        journal_sequence_watermark=user_event.sequence_no,
                    ))
                async for progress_event in self._stream_task_progress(
                    task, progress_queue=progress_queue, sequencer=sequencer
                ):
                    yield progress_event
                entry = await task
            if (
                not entry.handled
                or entry.message is None
                or entry.answer is None
            ):
                raise RuntimeError(
                    "trusted Controller returned no publishable answer"
                )
        except Exception:
            logger.exception(
                "Trusted Controller stream failed before publication"
            )
            async for event in self._publish_failure(
                sequencer=sequencer,
                user_input=user_input,
                message="本轮未能形成可信回答，请稍后重试。",
                model_name=model_name,
                database=database,
                committer=committer,
            ):
                yield event
            return

        answer = attach_progress_to_answer(entry.answer, collector)
        turn = entry.attempt.turn
        graph = (
            self._committer_graph(turn, user_input.request_id)
        )
        yield sse(
            sequencer.graph_snapshot(
                project_public_execution_graph(graph)
            ).model_dump(mode="json")
        )
        try:
            async with database.session() as session:
                committed = await committer.commit(
                    session,
                    user_input=user_input,
                    answer=answer,
                    turn=turn,
                    model_name=model_name,
                    agent_mode=str(
                        entry.message.custom_data.get(
                            "agent_mode", "controller_v1"
                        )
                    ),
                )
        except Exception:
            logger.exception(
                "Trusted stream publication transaction failed"
            )
            yield sse_error(
                "回答未能安全提交，因此本轮不会显示未提交结果。",
                error_type="publication_commit_failed",
            )
            yield "data: [DONE]\n\n"
            return

        if answer.status == "completed":
            terminal = sequencer.answer_completed(
                answer=answer,
                committed=committed,
            )
        elif answer.status == "clarification_required":
            terminal = sequencer.clarification_required(
                answer=answer,
                committed=committed,
            )
        else:
            terminal = sequencer.turn_failed(
                committed=committed,
                message=_failure_message(answer),
            )
        yield sse(terminal.model_dump(mode="json"))
        yield "data: [DONE]\n\n"

    async def _run_deep_research_turn(
        self,
        *,
        sequencer: TrustedStreamSequencer,
        database,
        user_input: UserInput,
        committer,
        collector: ExecutionProgressCollector,
        progress_queue: asyncio.Queue[CompletedExecutionStep],
    ) -> AsyncGenerator[str, None]:
        try:
            from app.services.research.deep_research_runner import (
                run_deep_research_turn,
            )

            with bind_execution_progress(collector):
                task = asyncio.create_task(run_deep_research_turn(user_input))
            async for progress_event in self._stream_task_progress(
                task, progress_queue=progress_queue, sequencer=sequencer
            ):
                yield progress_event
            answer = await task
            answer = attach_progress_to_answer(answer, collector)
        except Exception:
            logger.exception(
                "Deep Research runtime failed for request %s",
                user_input.request_id,
            )
            async for event in self._publish_failure(
                sequencer=sequencer,
                user_input=user_input,
                message=(
                    "\u6df1\u5ea6\u7814\u7a76\u672a\u80fd\u5b8c\u6210\uff0c"
                    "\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"
                ),
                model_name="",
                database=database,
                committer=committer,
            ):
                yield event
            return
        try:
            async with database.session() as session:
                committed = await committer.commit(
                    session,
                    user_input=user_input,
                    answer=answer,
                    turn=None,
                    model_name="",
                    agent_mode="deep_research",
                )
        except Exception:
            logger.exception(
                "Deep Research publication failed for request %s",
                user_input.request_id,
            )
            yield sse_error(
                "\u56de\u7b54\u672a\u80fd\u5b89\u5168\u63d0\u4ea4\uff0c"
                "\u56e0\u6b64\u672c\u8f6e\u4e0d\u4f1a\u663e\u793a\u672a\u63d0\u4ea4\u7ed3\u679c\u3002",
                error_type="publication_commit_failed",
            )
            yield "data: [DONE]\n\n"
            return
        yield sse(
            sequencer.graph_snapshot(
                project_public_execution_graph(
                    committed.execution_graph
                )
            ).model_dump(mode="json")
        )
        if answer.status == "completed":
            terminal = sequencer.answer_completed(
                answer=answer,
                committed=committed,
            )
        else:
            terminal = sequencer.turn_failed(
                committed=committed,
                message=answer.content,
            )
        yield sse(terminal.model_dump(mode="json"))
        yield "data: [DONE]\n\n"

    async def _publish_failure(
        self,
        *,
        sequencer: TrustedStreamSequencer,
        user_input: UserInput,
        message: str,
        model_name: str,
        database,
        committer,
    ) -> AsyncGenerator[str, None]:
        answer = PublishedAnswer(
            status="failed",
            content=message,
            receipt_backed=False,
            publication_mode="direct",
        )
        try:
            async with database.session() as session:
                committed = await committer.commit(
                    session,
                    user_input=user_input,
                    answer=answer,
                    turn=None,
                    model_name=model_name,
                )
        except Exception:
            logger.exception(
                "Trusted stream could not commit the failure lifecycle"
            )
            yield sse_error(
                "本轮未能安全完成，请稍后重试。",
                error_type="turn_failed",
            )
            yield "data: [DONE]\n\n"
            return
        yield sse(
            sequencer.graph_snapshot(
                project_public_execution_graph(
                    committed.execution_graph
                )
            ).model_dump(mode="json")
        )
        yield sse(
            sequencer.turn_failed(
                committed=committed,
                message=message,
            ).model_dump(mode="json")
        )
        yield "data: [DONE]\n\n"

    @staticmethod
    async def _stream_task_progress(
        task: asyncio.Task,
        *,
        progress_queue: asyncio.Queue[CompletedExecutionStep],
        sequencer: TrustedStreamSequencer,
    ) -> AsyncGenerator[str, None]:
        while not task.done() or not progress_queue.empty():
            try:
                step = await asyncio.wait_for(
                    progress_queue.get(),
                    timeout=0.1,
                )
            except asyncio.TimeoutError:
                continue
            yield sse(
                sequencer.step_completed(step).model_dump(mode="json")
            )


    def _dependencies(self):
        from app.infra.database import get_database
        from app.infra.llm import resolve_model_name
        from app.infra.llm.resolver import refresh_model_cache_if_missing
        from app.services.agent_core.chat_entry import AgentChatEntry
        from app.services.agent_core.publication.commit import (
            TurnPublicationCommitter,
        )
        from app.services.conversation import ConversationJournalService

        return (
            (
                self._database_factory or get_database
            )(),
            self._journal or ConversationJournalService(),
            self._entry or AgentChatEntry(),
            self._committer or TurnPublicationCommitter(),
            self._model_resolver or resolve_model_name,
            (
                self._model_cache_refresher
                or refresh_model_cache_if_missing
            ),
        )

    @staticmethod
    def _committer_graph(turn, request_id: str):
        from app.services.agent_core.publication.graph import (
            build_turn_execution_graph,
        )

        return build_turn_execution_graph(
            turn,
            request_id=request_id,
        )


def _failure_message(
    answer,
    *,
    fallback: str = "本轮未能安全完成，请稍后重试。",
) -> str:
    """Pass through the trusted failure content instead of hiding the cause."""

    content = str(getattr(answer, "content", "") or "").strip()
    return content if content else fallback


__all__ = ["TrustedControllerStream"]
