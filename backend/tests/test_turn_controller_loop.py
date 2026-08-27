from __future__ import annotations

import unittest
import uuid

from app.services.agent_core.capabilities import CapabilityRegistry
from app.services.agent_core.core_capabilities import (
    CoreCapabilityAvailability,
)
from app.services.agent_core.contracts import (
    AgentCoreTurnResult,
    ControllerOutput,
    ControllerToolCall,
)
from app.services.agent_core.harness import AgentCoreHarness
from app.services.agent_core.prompt_contracts import (
    ControllerContextSnapshot,
    ControllerModelRequest,
)
from app.services.agent_core.turn_loop import TurnControllerLoop
from app.services.agent_runtime.contracts import (
    ActionPlan,
    ActionReceipt,
    ExecutionContext,
    PlanReceipt,
    PlannedAction,
)


def _request() -> ControllerModelRequest:
    return ControllerModelRequest(
        model_name="controller",
        current_user_message="继续完成任务",
        context=ControllerContextSnapshot(),
    )


def _enabled_registry() -> CapabilityRegistry:
    return CapabilityRegistry(
        core_availability=CoreCapabilityAvailability.all_enabled()
    )


def _context() -> ExecutionContext:
    return ExecutionContext(
        user_id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        request_id="turn-loop-request",
    )


def _tool_output() -> ControllerOutput:
    return ControllerOutput(
        mode="capability_proposals",
        tool_calls=[
            ControllerToolCall(
                call_id="read",
                name="conversation_read",
                arguments={
                    "target": "exchange",
                    "selection": "latest",
                    "count": 1,
                },
            )
        ],
    )


class _QueuedController:
    def __init__(self, outputs) -> None:
        self.outputs = list(outputs)
        self.requests = []

    async def decide(self, request):
        self.requests.append(request)
        return self.outputs.pop(0)


class _FailingController:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def decide(self, request):
        raise self.exc


class _ModelRoundHarness:
    def __init__(self, *, raw_output: str = "SECRET_RAW_OUTPUT") -> None:
        self.calls = 0
        self.raw_output = raw_output

    async def run(self, output, *, goal, context, user_input=None):
        self.calls += 1
        if output.mode == "direct_answer":
            return await AgentCoreHarness(
                registry=_enabled_registry()
            ).run(
                output,
                goal=goal,
                context=context,
                user_input=user_input,
            )
        plan = ActionPlan(
            plan_id=f"model-plan-{self.calls}",
            source="controller_proposal",
            route_type="slow_path",
            intent="model_round",
            goal=goal,
            response_mode="model",
            actions=[
                PlannedAction(
                    action_id=f"model-action-{self.calls}",
                    capability="conversation",
                    operation="conversation_read",
                )
            ],
        )
        return AgentCoreTurnResult(
            execution_mode="live",
            output=output,
            plan=plan,
            receipt=PlanReceipt(
                plan_id=plan.plan_id,
                request_id=context.request_id,
                route_type=plan.route_type,
                intent=plan.intent,
                status="completed",
                actions=[
                    ActionReceipt(
                        action_id=plan.actions[0].action_id,
                        capability="conversation",
                        operation="conversation_read",
                        status="completed",
                        output={"raw": self.raw_output},
                        admitted=True,
                    )
                ],
            ),
        )


class _TerminalRuntime:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls = 0

    async def execute(self, plan, *, context, user_input=None):
        self.calls += 1
        action = plan.actions[0]
        output = (
            {
                "status": "clarification_required",
                "clarification_question": "请补充信息。",
            }
            if self.status == "waiting"
            else None
        )
        return PlanReceipt(
            plan_id=plan.plan_id,
            request_id=context.request_id,
            route_type=plan.route_type,
            intent=plan.intent,
            status=self.status,
            actions=[
                ActionReceipt(
                    action_id=action.action_id,
                    capability=action.capability,
                    operation=action.operation,
                    status=self.status,
                    output=output,
                    admitted=True,
                )
            ],
        )


class _ResearchReportHarness:
    """Returns one completed receipt carrying two confirmed research reports."""

    async def run(self, output, *, goal, context, user_input=None):
        plan = ActionPlan(
            plan_id="research-plan",
            source="controller_proposal",
            route_type="slow_path",
            intent="research_topic",
            goal=goal,
            response_mode="model",
            actions=[
                PlannedAction(
                    action_id="report-1",
                    capability="research",
                    operation="research_report_v1",
                ),
                PlannedAction(
                    action_id="report-2",
                    capability="research",
                    operation="research_report_v1",
                ),
            ],
        )
        receipt = PlanReceipt(
            plan_id=plan.plan_id,
            request_id=context.request_id,
            route_type=plan.route_type,
            intent=plan.intent,
            status="completed",
            actions=[
                ActionReceipt(
                    action_id="report-1",
                    capability="research",
                    operation="research_report_v1",
                    status="completed",
                    output={
                        "objective": goal,
                        "sources": [
                            {
                                "url": "https://a.example/x",
                                "title": "A1",
                                "snippet": "snippet a",
                            },
                            {
                                "url": "https://b.example/y",
                                "title": "B",
                                "snippet": "snippet b",
                            },
                        ],
                        "findings": ["snippet a", "唯一发现"],
                    },
                    admitted=True,
                ),
                ActionReceipt(
                    action_id="report-2",
                    capability="research",
                    operation="research_report_v1",
                    status="completed",
                    output={
                        "objective": goal,
                        "sources": [
                            {
                                "url": "https://a.example/x",
                                "title": "A1-dup",
                                "snippet": "snippet a",
                            },
                            {
                                "url": "https://c.example/z",
                                "title": "C",
                                "snippet": "snippet c",
                            },
                        ],
                        "findings": ["唯一发现", "第二发现"],
                    },
                    admitted=True,
                ),
            ],
        )
        return AgentCoreTurnResult(
            execution_mode="live",
            output=output,
            plan=plan,
            receipt=receipt,
        )


class _ResearchRoundHarness:
    """Returns research receipts so the next Controller round sees the report."""

    async def run(self, output, *, goal, context, user_input=None):
        if output.mode == "direct_answer":
            return await AgentCoreHarness(
                registry=_enabled_registry()
            ).run(
                output,
                goal=goal,
                context=context,
                user_input=user_input,
            )
        plan = ActionPlan(
            plan_id="research-round-plan",
            source="controller_proposal",
            route_type="slow_path",
            intent="research_topic",
            goal=goal,
            response_mode="model",
            actions=[
                PlannedAction(
                    action_id="start-research",
                    capability="research",
                    operation="start_research",
                ),
                PlannedAction(
                    action_id="acquire-1",
                    capability="research",
                    operation="acquire_research_sources",
                ),
                PlannedAction(
                    action_id="add-evidence-1",
                    capability="research",
                    operation="add_evidence",
                ),
                PlannedAction(
                    action_id="evaluate-gaps-1",
                    capability="research",
                    operation="evaluate_research_gaps",
                )
            ],
        )
        receipt = PlanReceipt(
            plan_id=plan.plan_id,
            request_id=context.request_id,
            route_type=plan.route_type,
            intent=plan.intent,
            status="completed",
            actions=[
                ActionReceipt(
                    action_id="start-research",
                    capability="research",
                    operation="start_research",
                    status="completed",
                    business_input={"objective": goal},
                    output={"run": {"objective": goal}},
                    admitted=True,
                ),
                ActionReceipt(
                    action_id="acquire-1",
                    capability="research",
                    operation="acquire_research_sources",
                    status="completed",
                    output={
                        "round_index": 1,
                        "executed": True,
                        "task": {"query": f"{goal} 评分 书评"},
                    },
                    admitted=True,
                ),
                ActionReceipt(
                    action_id="add-evidence-1",
                    capability="research",
                    operation="add_evidence",
                    status="completed",
                    output={
                        "results": [
                            {
                                "steps": [
                                    {
                                        "step_type": "add_evidence",
                                        "status": "completed",
                                        "output": {
                                            "evidence_admission": {
                                                "allowed": True,
                                                "evidence": {
                                                    "claim": "《失控》作者是凯文·凯利",
                                                    "source_url": (
                                                        "https://a.example/book"
                                                    ),
                                                    "quality": "high",
                                                },
                                            }
                                        },
                                    }
                                ],
                                "evidence": [
                                    {
                                        "claim": "《失控》作者是凯文·凯利",
                                        "source_url": "https://a.example/book",
                                        "quality": "high",
                                    }
                                ],
                            }
                        ]
                    },
                    admitted=True,
                ),
                ActionReceipt(
                    action_id="evaluate-gaps-1",
                    capability="research",
                    operation="evaluate_research_gaps",
                    status="completed",
                    output={
                        "gaps": ["missing_review_evidence"],
                        "gap_descriptions": ["未找到可核验评分或评论依据。"],
                        "satisfied": False,
                        "should_continue": True,
                        "stop_reason": "continue",
                        "next_actions": ["plan_next_search"],
                        "exhausted_queries": [f"{goal} 评分 书评"],
                    },
                    admitted=True,
                ),
            ],
        )
        return AgentCoreTurnResult(
            execution_mode="live",
            output=output,
            plan=plan,
            receipt=receipt,
        )


class TurnControllerLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_answer_finishes_in_one_round(self) -> None:
        controller = _QueuedController(
            [ControllerOutput(mode="direct_answer", text="直接回答")]
        )
        receipt = await TurnControllerLoop(
            controller=controller,
            harness=AgentCoreHarness(registry=_enabled_registry()),
        ).run(
            model_request=_request(),
            context=_context(),
            goal="直接回答",
        )
        self.assertEqual(receipt.status, "completed")
        self.assertEqual(len(receipt.rounds), 1)
        self.assertEqual(receipt.final_answer.content, "直接回答")

    async def test_controller_connection_failure_gets_specific_message(self) -> None:
        receipt = await TurnControllerLoop(
            controller=_FailingController(
                OSError("Connect call failed ('39.96.198.249', 443)")
            ),
            harness=AgentCoreHarness(registry=_enabled_registry()),
        ).run(
            model_request=_request(),
            context=_context(),
            goal="你好",
        )
        self.assertEqual(receipt.status, "failed")
        self.assertIn("模型服务网络连接失败", receipt.final_answer.content)
        self.assertNotIn("本轮模型决策未能完成", receipt.final_answer.content)

    async def test_controller_timeout_failure_gets_specific_message(self) -> None:
        receipt = await TurnControllerLoop(
            controller=_FailingController(TimeoutError("request timed out")),
            harness=AgentCoreHarness(registry=_enabled_registry()),
        ).run(
            model_request=_request(),
            context=_context(),
            goal="你好",
        )
        self.assertEqual(receipt.status, "failed")
        self.assertIn("模型服务响应超时", receipt.final_answer.content)

    async def test_model_round_receipt_is_projected_without_raw_output(
        self,
    ) -> None:
        controller = _QueuedController(
            [
                _tool_output(),
                ControllerOutput(mode="direct_answer", text="综合完成"),
            ]
        )
        harness = _ModelRoundHarness()
        receipt = await TurnControllerLoop(
            controller=controller,
            harness=harness,
        ).run(
            model_request=_request(),
            context=_context(),
            goal="先读取再综合",
        )
        self.assertEqual(receipt.status, "completed")
        self.assertEqual(len(receipt.rounds), 2)
        self.assertEqual(
            receipt.final_answer.publication_mode,
            "model_synthesis",
        )
        self.assertTrue(receipt.final_answer.receipt_backed)
        self.assertEqual(
            receipt.final_answer.receipt_refs,
            ["model-action-1"],
        )
        self.assertEqual(len(controller.requests[1].context.receipts), 1)
        projected = controller.requests[1].context.receipts[0]
        self.assertNotIn(harness.raw_output, projected.summary)

    async def test_allowlisted_conversation_answer_is_projected(self) -> None:
        controller = _QueuedController(
            [
                _tool_output(),
                ControllerOutput(mode="direct_answer", text="综合完成"),
            ]
        )

        class _AnswerHarness(_ModelRoundHarness):
            async def run(
                self,
                output,
                *,
                goal,
                context,
                user_input=None,
            ):
                result = await super().run(
                    output,
                    goal=goal,
                    context=context,
                    user_input=user_input,
                )
                if result.receipt is None:
                    return result
                action = result.receipt.actions[0].model_copy(
                    update={"output": {"answer": "上一轮的可信回答"}}
                )
                return result.model_copy(
                    update={
                        "receipt": result.receipt.model_copy(
                            update={"actions": [action]}
                        )
                    }
                )

        receipt = await TurnControllerLoop(
            controller=controller,
            harness=_AnswerHarness(),
        ).run(
            model_request=_request(),
            context=_context(),
            goal="读取后综合",
        )
        self.assertEqual(receipt.status, "completed")
        self.assertIn(
            "上一轮的可信回答",
            controller.requests[1].context.receipts[0].summary,
        )

    async def test_waiting_receipt_stops_without_second_controller_call(
        self,
    ) -> None:
        controller = _QueuedController([_tool_output()])
        runtime = _TerminalRuntime("waiting")
        receipt = await TurnControllerLoop(
            controller=controller,
            harness=AgentCoreHarness(
                registry=_enabled_registry(),
                runtime=runtime,  # type: ignore[arg-type]
            ),
        ).run(
            model_request=_request(),
            context=_context(),
            goal="等待澄清",
        )
        self.assertEqual(receipt.status, "clarification_required")
        self.assertEqual(len(controller.requests), 1)
        self.assertEqual(runtime.calls, 1)

    async def test_failed_receipt_stops_without_second_controller_call(
        self,
    ) -> None:
        controller = _QueuedController([_tool_output()])
        runtime = _TerminalRuntime("failed")
        receipt = await TurnControllerLoop(
            controller=controller,
            harness=AgentCoreHarness(
                registry=_enabled_registry(),
                runtime=runtime,  # type: ignore[arg-type]
            ),
        ).run(
            model_request=_request(),
            context=_context(),
            goal="失败终止",
        )
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(len(controller.requests), 1)
        self.assertEqual(runtime.calls, 1)

    async def test_book_source_unavailable_falls_back_to_model_markdown(
        self,
    ) -> None:
        class _UnavailableBookHarness:
            async def run(
                self,
                output,
                *,
                goal,
                context,
                user_input=None,
            ):
                plan = ActionPlan(
                    plan_id="book-unavailable-plan",
                    source="controller_proposal",
                    route_type="fast_path",
                    intent="book_recommendation",
                    goal=goal,
                    response_mode="model",
                    actions=[
                        PlannedAction(
                            action_id="book-search",
                            capability="book_search",
                            operation="book_search_v1",
                        )
                    ],
                )
                return AgentCoreTurnResult(
                    execution_mode="live",
                    output=output,
                    plan=plan,
                    receipt=PlanReceipt(
                        plan_id=plan.plan_id,
                        request_id=context.request_id,
                        route_type=plan.route_type,
                        intent=plan.intent,
                        status="failed",
                        actions=[
                            ActionReceipt(
                                action_id="book-search",
                                capability="book_search",
                                operation="book_search_v1",
                                status="failed",
                                error="book_source_unavailable",
                                admitted=True,
                            )
                        ],
                    ),
                )

        controller = _QueuedController(
            [
                ControllerOutput(
                    mode="capability_proposals",
                    tool_calls=[
                        ControllerToolCall(
                            call_id="book-search",
                            name="book_search",
                            arguments={"query": "个人成长"},
                        )
                    ],
                ),
                ControllerOutput(
                    mode="direct_answer",
                    text=(
                        "本次未能完成外部核验，先给你一份基于稳定知识的建议。\n\n"
                        "## 推荐\n\n- 《金钱心理学》：理解金钱行为。"
                        "[伪造来源](https://not-admitted.example/book)"
                    ),
                ),
            ]
        )
        receipt = await TurnControllerLoop(
            controller=controller,
            harness=_UnavailableBookHarness(),
        ).run(
            model_request=_request(),
            context=_context(),
            goal="推荐个人成长书籍",
        )

        self.assertEqual(receipt.status, "completed")
        self.assertEqual(len(controller.requests), 2)
        self.assertEqual(controller.requests[1].phase, "synthesis")
        self.assertEqual(
            receipt.final_answer.publication_mode,
            "model_knowledge_fallback",
        )
        self.assertFalse(receipt.final_answer.receipt_backed)
        self.assertIn("## 推荐", receipt.final_answer.content)
        self.assertIn("《金钱心理学》", receipt.final_answer.content)
        self.assertNotIn("https://", receipt.final_answer.content)
        self.assertEqual(
            receipt.final_answer.custom_data["external_evidence_status"],
            "unavailable",
        )

    async def test_round_limit_stops_without_seventh_call(self) -> None:
        controller = _QueuedController([_tool_output(), _tool_output()])
        harness = _ModelRoundHarness()
        receipt = await TurnControllerLoop(
            controller=controller,
            harness=harness,
            max_rounds=2,
        ).run(
            model_request=_request(),
            context=_context(),
            goal="循环",
        )
        self.assertEqual(receipt.status, "limit_exceeded")
        self.assertEqual(len(controller.requests), 2)
        self.assertEqual(harness.calls, 2)
        self.assertEqual(receipt.final_answer.status, "failed")
        self.assertNotIn("轮数", receipt.final_answer.content)
        self.assertNotIn("控制", receipt.final_answer.content)

    async def test_research_merges_all_confirmed_reports(self) -> None:
        controller = _QueuedController(
            [
                ControllerOutput(
                    mode="capability_proposals",
                    tool_calls=[
                        ControllerToolCall(
                            call_id="research",
                            name="research_report_v1",
                            arguments={},
                        )
                    ],
                )
            ]
        )
        receipt = await TurnControllerLoop(
            controller=controller,
            harness=_ResearchReportHarness(),
        ).run(
            model_request=_request(),
            context=_context(),
            goal="深度搜索类似《失控》的书",
        )
        self.assertEqual(receipt.status, "completed")
        self.assertEqual(
            set(receipt.final_answer.receipt_refs),
            {"report-1", "report-2"},
        )
        content = receipt.final_answer.content
        self.assertIn("https://b.example/y", content)
        self.assertIn("https://c.example/z", content)
        self.assertEqual(content.count("https://a.example/x"), 1)

    async def test_research_round_projects_trusted_research_state(self) -> None:
        controller = _QueuedController(
            [
                ControllerOutput(
                    mode="capability_proposals",
                    tool_calls=[
                        ControllerToolCall(
                            call_id="research",
                            name="research_report_v1",
                            arguments={},
                        )
                    ],
                ),
                ControllerOutput(mode="direct_answer", text="研究完成"),
            ]
        )
        receipt = await TurnControllerLoop(
            controller=controller,
            harness=_ResearchRoundHarness(),
        ).run(
            model_request=_request(),
            context=_context(),
            goal="推荐类似《失控》的书",
        )
        self.assertEqual(receipt.status, "completed")
        second_request = controller.requests[1]
        self.assertIsNotNone(second_request.context.trusted_research_state)
        research = second_request.context.trusted_research_state
        assert research is not None
        self.assertEqual(research.objective, "推荐类似《失控》的书")
        self.assertEqual(research.step_count, 1)
        self.assertEqual(len(research.confirmed_facts), 1)
        self.assertEqual(
            research.confirmed_facts[0].content,
            "《失控》作者是凯文·凯利",
        )
        self.assertEqual(research.information_gaps, ["missing_review_evidence"])

    async def test_non_research_round_keeps_research_state_none(self) -> None:
        controller = _QueuedController(
            [
                _tool_output(),
                ControllerOutput(mode="direct_answer", text="综合完成"),
            ]
        )
        receipt = await TurnControllerLoop(
            controller=controller,
            harness=_ModelRoundHarness(),
        ).run(
            model_request=_request(),
            context=_context(),
            goal="先读取再综合",
        )
        self.assertEqual(receipt.status, "completed")
        self.assertIsNone(
            controller.requests[1].context.trusted_research_state
        )


if __name__ == "__main__":
    unittest.main()
