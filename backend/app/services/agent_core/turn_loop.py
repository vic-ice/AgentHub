from __future__ import annotations

import logging
from typing import Protocol

from app.schemas.chat import UserInput
from app.services.agent_core.contracts import (
    AgentCoreTurnResult,
    ControllerOutput,
    PublishedAnswer,
)
from app.services.agent_core.prompt_contracts import ControllerModelRequest
from app.services.agent_core.publication.contracts import (
    ReceiptEvidenceBundle,
)
from app.services.agent_core.publication.response_view import (
    project_external_answer_view,
)
from app.services.agent_core.publication.service import TrustedPublisher
from app.services.agent_core.receipt_projector import (
    ReceiptContextProjector,
)
from app.services.agent_core.research_state_projection import (
    project_research_controller_context,
)
from app.services.agent_core.turn_contracts import (
    ControllerRoundReceipt,
    TurnReceipt,
)
from app.services.agent_runtime.contracts import ExecutionContext


MAX_CONTROLLER_ROUNDS = 6
logger = logging.getLogger(__name__)


class ControllerPort(Protocol):
    async def decide(
        self,
        request: ControllerModelRequest,
    ) -> ControllerOutput: ...


class HarnessPort(Protocol):
    async def run(
        self,
        output: ControllerOutput,
        *,
        goal: str,
        context: ExecutionContext,
        user_input: UserInput | None = None,
    ) -> AgentCoreTurnResult: ...


class IntentRouterPort(Protocol):
    def decide(
        self,
        request: ControllerModelRequest,
    ) -> ControllerOutput | None: ...


class TurnControllerLoop:
    """Bound one Controller/ActionPlan/Receipt sequence to at most six rounds."""

    def __init__(
        self,
        *,
        controller: ControllerPort,
        harness: HarnessPort,
        projector: ReceiptContextProjector | None = None,
        publisher: TrustedPublisher | None = None,
        intent_router: IntentRouterPort | None = None,
        max_rounds: int = MAX_CONTROLLER_ROUNDS,
    ) -> None:
        value = int(max_rounds)
        if value < 1 or value > MAX_CONTROLLER_ROUNDS:
            raise ValueError("max_rounds must be between 1 and 6")
        self._controller = controller
        self._harness = harness
        self._projector = projector or ReceiptContextProjector()
        self._publisher = publisher or TrustedPublisher()
        self._intent_router = intent_router
        self._max_rounds = value

    async def run(
        self,
        *,
        model_request: ControllerModelRequest,
        context: ExecutionContext,
        goal: str,
        user_input: UserInput | None = None,
    ) -> TurnReceipt:
        request = model_request
        rounds: list[ControllerRoundReceipt] = []
        plan_receipts = []
        evidence: list[ReceiptEvidenceBundle] = []

        for round_no in range(1, self._max_rounds + 1):
            try:
                output = None
                if self._intent_router is not None:
                    output = self._intent_router.decide(request)
                if output is None:
                    output = await self._controller.decide(request)
            except Exception as exc:
                logger.exception(
                    "Controller decision failed for request %s",
                    context.request_id,
                )
                fallback = (
                    self._publisher.publish_evidence_fallback(evidence=evidence)
                    if request.phase == "synthesis" and evidence
                    else None
                )
                if fallback is not None:
                    return TurnReceipt(
                        status="completed",
                        request_id=context.request_id,
                        rounds=rounds,
                        plan_receipts=plan_receipts,
                        final_answer=fallback,
                    )
                return _failed_turn(
                    request_id=context.request_id,
                    rounds=rounds,
                    plan_receipts=plan_receipts,
                    content=_controller_failure_message(exc),
                )
            if request.phase == "synthesis" and output.mode != "direct_answer":
                fallback = self._publisher.publish_evidence_fallback(
                    evidence=evidence
                )
                if fallback is not None:
                    return TurnReceipt(
                        status="completed",
                        request_id=context.request_id,
                        rounds=rounds,
                        plan_receipts=plan_receipts,
                        final_answer=fallback,
                    )
                return _failed_turn(
                    request_id=context.request_id,
                    rounds=rounds,
                    plan_receipts=plan_receipts,
                    content="这次查到的内容还没能整理成完整回答，请稍后重试。",
                )
            if request.phase == "synthesis":
                try:
                    answer = self._publisher.publish_synthesis(
                        output,
                        evidence=evidence,
                    )
                except ValueError as exc:
                    logger.warning(
                        "Controller synthesis rejected for request %s: %s",
                        context.request_id,
                        exc,
                    )
                    answer = self._publisher.publish_evidence_fallback(
                        evidence=evidence
                    )
                except Exception:
                    logger.exception(
                        "Trusted synthesis publication failed for request %s",
                        context.request_id,
                    )
                    answer = self._publisher.publish_evidence_fallback(
                        evidence=evidence
                    )
                rounds.append(
                    ControllerRoundReceipt(
                        round_no=round_no,
                        output=output,
                        answer=answer,
                    )
                )
                if answer is not None:
                    return TurnReceipt(
                        status=_answer_status(answer),
                        request_id=context.request_id,
                        rounds=rounds,
                        plan_receipts=plan_receipts,
                        final_answer=answer,
                    )
                return _failed_turn(
                    request_id=context.request_id,
                    rounds=rounds,
                    plan_receipts=plan_receipts,
                    content=(
                        "这次找到的内容还不够可靠，我先不贸然给出结论。"
                    ),
                )
            try:
                result = await self._harness.run(
                    output,
                    goal=goal,
                    context=context,
                    user_input=user_input,
                )
            except Exception:
                logger.exception(
                    "Agent harness execution failed for request %s",
                    context.request_id,
                )
                fallback = (
                    self._publisher.publish_evidence_fallback(evidence=evidence)
                    if request.phase == "synthesis" and evidence
                    else None
                )
                if fallback is not None:
                    return TurnReceipt(
                        status="completed",
                        request_id=context.request_id,
                        rounds=rounds,
                        plan_receipts=plan_receipts,
                        final_answer=fallback,
                    )
                rounds.append(
                    ControllerRoundReceipt(
                        round_no=round_no,
                        output=output,
                    )
                )
                return _failed_turn(
                    request_id=context.request_id,
                    rounds=rounds,
                    plan_receipts=plan_receipts,
                    content="这次处理没有顺利完成，请稍后重试。",
                )

            if result.receipt is not None:
                plan_receipts.append(result.receipt)
                if result.plan is not None:
                    evidence.append(
                        ReceiptEvidenceBundle(
                            plan=result.plan,
                            receipt=result.receipt,
                        )
                    )

            answer = result.answer
            if (
                answer is None
                and result.plan is not None
                and result.receipt is not None
            ):
                answer = _research_terminal_answer(result.receipt)
            if (
                answer is not None
                and output.mode == "direct_answer"
                and evidence
            ):
                try:
                    answer = self._publisher.publish_synthesis(
                        output,
                        evidence=evidence,
                    )
                except Exception:
                    logger.exception(
                        "Trusted synthesis publication failed for request %s",
                        context.request_id,
                    )
                    fallback = self._publisher.publish_evidence_fallback(
                        evidence=evidence
                    )
                    if fallback is not None:
                        rounds.append(
                            ControllerRoundReceipt(
                                round_no=round_no,
                                output=result.output,
                                plan=result.plan,
                                receipt=result.receipt,
                            )
                        )
                        return TurnReceipt(
                            status="completed",
                            request_id=context.request_id,
                            rounds=rounds,
                            plan_receipts=plan_receipts,
                            final_answer=fallback,
                        )
                    rounds.append(
                        ControllerRoundReceipt(
                            round_no=round_no,
                            output=result.output,
                            plan=result.plan,
                            receipt=result.receipt,
                        )
                    )
                    return _failed_turn(
                        request_id=context.request_id,
                        rounds=rounds,
                        plan_receipts=plan_receipts,
                        content=(
                            "这次找到的内容还不够可靠，我先不贸然给出结论。"
                        ),
                    )

            round_receipt = ControllerRoundReceipt(
                round_no=round_no,
                output=result.output,
                plan=result.plan,
                receipt=result.receipt,
                answer=answer,
            )
            rounds.append(round_receipt)

            if answer is not None:
                return TurnReceipt(
                    status=_answer_status(answer),
                    request_id=context.request_id,
                    rounds=rounds,
                    plan_receipts=plan_receipts,
                    final_answer=answer,
                )
            if result.plan is None or result.receipt is None:
                return _failed_turn(
                    request_id=context.request_id,
                    rounds=rounds,
                    plan_receipts=plan_receipts,
                    content="这次处理没有得到可继续使用的结果，请稍后重试。",
                )
            if result.plan.response_mode != "model":
                return _failed_turn(
                    request_id=context.request_id,
                    rounds=rounds,
                    plan_receipts=plan_receipts,
                    content="这次处理没有形成完整回答，请稍后重试。",
                )
            has_completed = any(
                action.status == "completed"
                for action in result.receipt.actions
            )
            if not has_completed:
                reason = _first_failure_reason(result.receipt.actions)
                return _failed_turn(
                    request_id=context.request_id,
                    rounds=rounds,
                    plan_receipts=plan_receipts,
                    content=(
                        f"本轮未能完成：{reason}。请稍后重试或更换问法。"
                        if reason
                        else "本轮未能完成，请稍后重试或更换问法。"
                    ),
                )
            if result.receipt.status not in {"completed", "partial"}:
                return _failed_turn(
                    request_id=context.request_id,
                    rounds=rounds,
                    plan_receipts=plan_receipts,
                    content="本轮工具结果不足以继续安全综合。",
                )

            projected = self._projector.project(result.receipt)
            if not projected:
                return _failed_turn(
                    request_id=context.request_id,
                    rounds=rounds,
                    plan_receipts=plan_receipts,
                    content="本轮没有可供继续综合的受信结果。",
                )
            receipts = [*request.context.receipts, *projected][-32:]
            research_state = project_research_controller_context(
                [
                    action
                    for plan_receipt in plan_receipts
                    for action in plan_receipt.actions
                ]
            )
            request = request.model_copy(
                update={
                    "phase": (
                        "synthesis"
                        if _has_external_evidence(projected)
                        else request.phase
                    ),
                    "context": request.context.model_copy(
                        update={
                            "receipts": receipts,
                            "response_view": project_external_answer_view(evidence),
                            "trusted_research_state": research_state,
                        }
                    )
                }
            )

        fallback = self._publisher.publish_evidence_fallback(evidence=evidence)
        if fallback is not None:
            return TurnReceipt(
                status="completed",
                request_id=context.request_id,
                rounds=rounds,
                plan_receipts=plan_receipts,
                final_answer=fallback,
            )
        return TurnReceipt(
            status="limit_exceeded",
            request_id=context.request_id,
            rounds=rounds,
            plan_receipts=plan_receipts,
            final_answer=PublishedAnswer(
                status="failed",
                content="这次处理没有顺利形成可用回答，请稍后重试。",
                receipt_backed=False,
            ),
        )


def _has_external_evidence(projected) -> bool:
    return any(
        item.result_mode
        in {"book_evidence", "web_evidence", "weather_evidence"}
        for item in projected
    )


def _answer_status(answer: PublishedAnswer) -> str:
    if answer.status == "completed":
        return "completed"
    if answer.status == "clarification_required":
        return "clarification_required"
    return "failed"


def _first_failure_reason(actions) -> str:
    for action in actions:
        error = str(getattr(action, "error", "") or "").strip()
        if error:
            return error[:160]
    return ""


def _research_terminal_answer(receipt) -> PublishedAnswer | None:
    """Turn the completed research evidence into a deterministic answer."""

    from app.services.agent_core.publication.research_renderer import (
        merge_research_reports,
        research_terminal_answer,
    )

    reports = [
        action
        for action in receipt.actions
        if action.operation == "research_report_v1"
        and action.status == "completed"
        and isinstance(action.output, dict)
    ]
    if not reports:
        return None
    merged = merge_research_reports(
        [action.output for action in reports]
    )
    return research_terminal_answer(
        merged,
        receipt_refs=[action.action_id for action in reports],
    )


def _controller_failure_message(exc: Exception) -> str:
    message = str(exc).lower()
    class_name = type(exc).__name__.lower()
    if any(
        token in f"{class_name} {message}"
        for token in (
            "apiconnectionerror",
            "connecterror",
            "clientconnectorerror",
            "connection error",
            "cannot connect",
            "connect call failed",
            "connection refused",
            "connection reset",
            "network",
            "dns",
            "10013",
        )
    ):
        return (
            "模型服务网络连接失败：当前无法连接到模型供应商。"
            "请检查网络、代理或模型供应商配置后重试。"
        )
    if any(token in message for token in ("timeout", "timed out", "超时")):
        return "模型服务响应超时，请稍后重试或切换模型。"
    if any(
        token in message
        for token in (
            "quota",
            "allocationquota",
            "free tier",
            "rate limit",
            "429",
        )
    ):
        return (
            "模型服务暂时不可用：当前模型的额度已用尽或触发限流。"
            "请切换模型或稍后重试。"
        )
    if any(token in message for token in ("401", "403", "auth", "api key")):
        return "模型服务认证失败，请检查该模型的配置与密钥。"
    if any(token in message for token in ("empty", "未输出")):
        return "模型未返回有效内容，请稍后重试或切换模型。"
    if "controllerclient" in class_name:
        return "模型输出格式异常，请稍后重试或切换模型。"
    return "本轮模型决策未能完成，请稍后重试或更换问法。"


def _failed_turn(
    *,
    request_id: str,
    rounds: list[ControllerRoundReceipt],
    plan_receipts: list,
    content: str,
) -> TurnReceipt:
    return TurnReceipt(
        status="failed",
        request_id=request_id,
        rounds=rounds,
        plan_receipts=plan_receipts,
        final_answer=PublishedAnswer(
            status="failed",
            content=content,
            receipt_backed=False,
        ),
    )


__all__ = [
    "MAX_CONTROLLER_ROUNDS",
    "TurnControllerLoop",
]
