from __future__ import annotations

import unittest
from uuid import uuid4

from app.services.agent_core.contracts import (
    AgentCoreTurnResult,
    PublishedAnswer,
)
from app.services.agent_core.prompt_contracts import (
    ControllerContextSnapshot,
    ControllerModelRequest,
)
from app.services.agent_core.turn_contracts import TurnReceipt
from app.services.agent_core.turn_loop import TurnControllerLoop
from app.services.agent_runtime.contracts import ExecutionContext
from app.services.memory.intent import (
    MemoryIntent,
    MemoryIntentClassifier,
    MemoryIntentRouter,
)


class _FakeRequest:
    def __init__(self, text, conversation=None):
        self.current_user_message = text
        self.context = type(
            "Context",
            (),
            {"conversation": conversation or []},
        )()


class MemoryIntentClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = MemoryIntentClassifier()

    def _kind(self, text: str, topic=None):
        intent = self.classifier.classify(text, previous_topic=topic)
        return intent.kind if intent is not None else None

    def test_canonical_intents(self) -> None:
        self.assertEqual(self._kind("我是谁"), "current")
        self.assertEqual(self._kind("我叫什么"), "current")
        self.assertEqual(self._kind("我之前叫什么"), "previous")
        self.assertEqual(self._kind("我最早叫什么"), "earliest")
        self.assertEqual(self._kind("我改过几次名字"), "timeline")
        self.assertEqual(self._kind("什么时候说的我叫冰露"), "when")
        self.assertEqual(self._kind("我叫冰露"), "write")
        self.assertEqual(self._kind("我喜欢科幻小说"), "write")
        self.assertEqual(self._kind("我有一只猫"), "write")
        self.assertEqual(self._kind("我还有一只狗"), "write")
        self.assertEqual(self._kind("忘了我叫冰露"), "forget")
        self.assertIsNone(self._kind("帮我查一下今天的天气"))

    def test_correction_requires_new_name(self) -> None:
        intent = self.classifier.classify("我不叫黄仁")
        self.assertEqual(intent.kind, "correct")
        self.assertIn("new", intent.requires)

    def test_anaphora_resolution(self) -> None:
        topic = MemoryIntent(
            kind="read",
            predicate="name",
            scope="previous",
            evidence_quote="x",
        )
        self.assertEqual(self._kind("那之前呢", topic=topic), "read")
        self.assertEqual(self._kind("那最早呢", topic=topic), "read")
        when = self.classifier.classify("这是啥时候说的", previous_topic=topic)
        self.assertEqual(when.kind, "when")
        self.assertEqual(when.scope, "timeline")

    def test_slot_values(self) -> None:
        prefer = self.classifier.classify("我喜欢科幻小说")
        self.assertEqual(
            prefer.value,
            {"entity": "科幻小说", "polarity": "like"},
        )
        possess = self.classifier.classify("我有一只猫")
        self.assertEqual(possess.value, {"entity": "猫"})

    def test_natural_memory_write_variants(self) -> None:
        remember = self.classifier.classify("别忘了我叫冰露")
        self.assertEqual(remember.kind, "write")
        self.assertEqual(remember.value, {"name": "冰露"})

        shorthand = self.classifier.classify("记一下我叫冰露")
        self.assertEqual(shorthand.kind, "write")
        self.assertEqual(shorthand.value, {"name": "冰露"})

        correction = self.classifier.classify("我改名叫小露")
        self.assertEqual(correction.kind, "correct")
        self.assertEqual(correction.value, {"name": "小露"})

    def test_preference_and_pet_entity_slots(self) -> None:
        avoid = self.classifier.classify("我想避免血腥悬疑")
        self.assertEqual(
            avoid.value,
            {"entity": "血腥悬疑", "polarity": "avoid"},
        )

        pet = self.classifier.classify("我养了一只猫叫小白")
        self.assertEqual(pet.kind, "write")
        self.assertEqual(pet.predicate, "has")
        self.assertEqual(pet.value, {"entity": "猫"})
        self.assertEqual(len(pet.assertions), 1)


class MemoryIntentRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = MemoryIntentRouter()

    def test_write_proposal(self) -> None:
        output = self.router.decide(_FakeRequest("我叫冰露"))
        self.assertEqual(output.mode, "capability_proposals")
        call = output.tool_calls[0]
        self.assertEqual(call.name, "remember_memory")
        self.assertEqual(
            call.arguments["assertions"][0]["value"],
            {"name": "冰露"},
        )

    def test_generic_possession_clarifies(self) -> None:
        output = self.router.decide(_FakeRequest("我有一只宠物"))
        self.assertEqual(output.mode, "request_clarification")
        self.assertIn("宠物", output.text)
        self.assertIn("先记着", output.text)

    def test_specific_possession_saves_directly(self) -> None:
        output = self.router.decide(_FakeRequest("我有一只橘猫"))
        self.assertEqual(output.mode, "capability_proposals")
        call = output.tool_calls[0]
        self.assertEqual(call.name, "remember_memory")
        self.assertEqual(
            call.arguments["assertions"][0]["value"],
            {"entity": "橘猫"},
        )

    def test_entity_subject_like_writes_entity_fact(self) -> None:
        output = self.router.decide(_FakeRequest("小猪喜欢吃胡萝卜"))
        self.assertEqual(output.mode, "capability_proposals")
        call = output.tool_calls[0]
        self.assertEqual(call.name, "remember_memory")
        assertion = call.arguments["assertions"][0]
        self.assertEqual(assertion["subject"], "小猪")
        self.assertEqual(assertion["predicate"], "likes")
        self.assertEqual(
            assertion["value"],
            {"entity": "胡萝卜", "polarity": "like"},
        )

    def test_named_possession_saves_entity_fact(self) -> None:
        output = self.router.decide(_FakeRequest("我有一个小猪，叫xiaoyan"))
        self.assertEqual(output.mode, "capability_proposals")
        call = output.tool_calls[0]
        self.assertEqual(call.name, "remember_memory")
        assertions = call.arguments["assertions"]
        self.assertEqual(len(assertions), 1)
        self.assertEqual(assertions[0]["value"], {"entity": "小猪"})

    def test_decline_saves_previous_partial(self) -> None:
        previous = type("Turn", (), {"role": "user", "content": "我有一只宠物"})()
        output = self.router.decide(
            _FakeRequest("先记着吧", conversation=[previous])
        )
        self.assertEqual(output.mode, "capability_proposals")
        call = output.tool_calls[0]
        self.assertEqual(
            call.arguments["assertions"][0]["value"],
            {"entity": "宠物"},
        )

    def test_bare_completion_uses_pending_clarification(self) -> None:
        previous = type("Turn", (), {"role": "user", "content": "我有一只宠物"})()
        working = type(
            "Working",
            (),
            {"pending_question": "你提到我有一只宠物，能告诉我具体是什么宠物吗？"},
        )()
        context = type(
            "Context",
            (),
            {"conversation": [previous], "working_state": working},
        )()
        request = type(
            "Request",
            (),
            {"current_user_message": "橘猫", "context": context},
        )()
        output = self.router.decide(request)
        self.assertEqual(output.mode, "capability_proposals")
        call = output.tool_calls[0]
        self.assertEqual(
            call.arguments["assertions"][0]["value"],
            {"entity": "橘猫"},
        )

    def test_read_proposal(self) -> None:
        output = self.router.decide(_FakeRequest("我之前叫什么"))
        self.assertEqual(output.mode, "capability_proposals")
        call = output.tool_calls[0]
        self.assertEqual(call.name, "search_memory")
        self.assertEqual(call.arguments["scope"], "previous")
        self.assertEqual(call.arguments["predicate"], "name")

    def test_clarification_on_missing_slot(self) -> None:
        output = self.router.decide(_FakeRequest("我不叫黄仁"))
        self.assertEqual(output.mode, "request_clarification")
        self.assertIn("新", output.text)

    def test_unknown_falls_back_to_model(self) -> None:
        self.assertIsNone(self.router.decide(_FakeRequest("帮我查一下今天的天气")))

    def test_early_match_gate(self) -> None:
        self.assertTrue(self.router.matches("我之前叫什么"))
        self.assertTrue(self.router.matches("我叫冰露"))
        self.assertFalse(self.router.matches("那之前呢"))
        self.assertFalse(self.router.matches("帮我查一下今天的天气"))

    def test_anaphora_uses_previous_user_turn(self) -> None:
        previous = type("Turn", (), {"role": "user", "content": "我叫冰露"})()
        output = self.router.decide(
            _FakeRequest("那之前呢", conversation=[previous])
        )
        self.assertEqual(output.mode, "capability_proposals")
        self.assertEqual(output.tool_calls[0].arguments["scope"], "previous")
        self.assertEqual(output.tool_calls[0].arguments["predicate"], "name")


class MemoryIntentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_skips_controller(self) -> None:
        class _Controller:
            async def decide(self, request):
                raise AssertionError("controller must not be called")

        class _Harness:
            async def run(self, output, *, goal, context, user_input=None):
                return AgentCoreTurnResult(
                    execution_mode="live",
                    output=output,
                    answer=PublishedAnswer(
                        status="completed",
                        content="ok",
                        receipt_backed=False,
                    ),
                )

        request = ControllerModelRequest(
            model_name="rule-router",
            current_user_message="我有一只宠物",
            context=ControllerContextSnapshot(conversation=[]),
        )
        context = ExecutionContext(
            user_id=uuid4(),
            request_id="intent-loop",
            model_name="rule-router",
        )
        loop = TurnControllerLoop(
            controller=_Controller(),
            harness=_Harness(),
            intent_router=MemoryIntentRouter(),
        )
        turn = await loop.run(
            model_request=request,
            context=context,
            goal="我有一只宠物",
        )
        self.assertIsInstance(turn, TurnReceipt)
        self.assertEqual(turn.final_answer.content, "ok")

    async def test_failed_evidence_fails_fast_with_reason(self) -> None:
        from app.services.agent_core.contracts import (
            ControllerOutput,
            ControllerToolCall,
        )
        from app.services.agent_runtime.contracts import (
            ActionPlan,
            ActionReceipt,
            PlanReceipt,
            PlannedAction,
        )

        class _Controller:
            async def decide(self, request):
                return ControllerOutput(
                    mode="capability_proposals",
                    tool_calls=[
                        ControllerToolCall(
                            call_id="c1",
                            name="research_start",
                            arguments={},
                        )
                    ],
                )

        plan = ActionPlan(
            source="controller_proposal",
            route_type="fast_path",
            intent="controller_capability_batch",
            response_mode="model",
            goal="深度搜索书籍推荐",
            actions=[
                PlannedAction(
                    action_id="search",
                    capability="research",
                    operation="research_search_v1",
                    arguments={},
                )
            ],
        )
        receipt = PlanReceipt(
            plan_id=plan.plan_id,
            request_id="research-fail",
            route_type="fast_path",
            intent="controller_capability_batch",
            status="partial",
            actions=[
                ActionReceipt(
                    action_id="search",
                    capability="research",
                    operation="research_search_v1",
                    status="failed",
                    error="provider adapter_error: upstream timeout",
                )
            ],
        )

        class _Harness:
            async def run(self, output, *, goal, context, user_input=None):
                return AgentCoreTurnResult(
                    execution_mode="live",
                    output=output,
                    plan=plan,
                    receipt=receipt,
                    answer=None,
                )

        request = ControllerModelRequest(
            model_name="rule-router",
            current_user_message="深度搜索书籍推荐",
            context=ControllerContextSnapshot(conversation=[]),
        )
        context = ExecutionContext(
            user_id=uuid4(),
            request_id="research-fail",
            model_name="rule-router",
        )
        loop = TurnControllerLoop(
            controller=_Controller(),
            harness=_Harness(),
        )
        turn = await loop.run(
            model_request=request,
            context=context,
            goal="深度搜索书籍推荐",
        )
        self.assertEqual(turn.status, "failed")
        self.assertIn("upstream timeout", turn.final_answer.content)

    async def test_research_report_terminates_turn(self) -> None:
        from app.services.agent_core.contracts import (
            ControllerOutput,
            ControllerToolCall,
        )
        from app.services.agent_runtime.contracts import (
            ActionPlan,
            ActionReceipt,
            PlanReceipt,
            PlannedAction,
        )

        calls = {"count": 0}

        class _Controller:
            async def decide(self, request):
                calls["count"] += 1
                if calls["count"] > 1:
                    raise AssertionError(
                        "controller must not be called after a research report"
                    )
                return ControllerOutput(
                    mode="capability_proposals",
                    tool_calls=[
                        ControllerToolCall(
                            call_id="r",
                            name="research_start",
                            arguments={},
                        )
                    ],
                )

        plan = ActionPlan(
            source="controller_proposal",
            route_type="fast_path",
            intent="controller_capability_batch",
            response_mode="model",
            goal="深度搜索书籍推荐",
            actions=[
                PlannedAction(
                    action_id="report",
                    capability="research",
                    operation="research_report_v1",
                    arguments={},
                )
            ],
        )
        receipt = PlanReceipt(
            plan_id=plan.plan_id,
            request_id="research-ok",
            route_type="fast_path",
            intent="controller_capability_batch",
            status="completed",
            actions=[
                ActionReceipt(
                    action_id="report",
                    capability="research",
                    operation="research_report_v1",
                    status="completed",
                    output={
                        "status": "ok",
                        "objective": "书籍推荐",
                        "findings": ["系统思维"],
                        "sources": [
                            {
                                "url": "https://book.douban.com/subject/1/",
                                "title": "系统思维",
                                "snippet": "复杂系统经典",
                            }
                        ],
                    },
                )
            ],
        )

        class _Harness:
            async def run(self, output, *, goal, context, user_input=None):
                return AgentCoreTurnResult(
                    execution_mode="live",
                    output=output,
                    plan=plan,
                    receipt=receipt,
                    answer=None,
                )

        request = ControllerModelRequest(
            model_name="rule-router",
            current_user_message="深度搜索书籍推荐",
            context=ControllerContextSnapshot(conversation=[]),
        )
        context = ExecutionContext(
            user_id=uuid4(),
            request_id="research-ok",
            model_name="rule-router",
        )
        loop = TurnControllerLoop(
            controller=_Controller(),
            harness=_Harness(),
        )
        turn = await loop.run(
            model_request=request,
            context=context,
            goal="深度搜索书籍推荐",
        )
        self.assertEqual(turn.status, "completed")
        self.assertIn("book.douban.com", turn.final_answer.content)
        self.assertEqual(calls["count"], 1)


if __name__ == "__main__":
    unittest.main()