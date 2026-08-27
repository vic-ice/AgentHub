from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.infra.llm.history import model_history_projector
from app.services.agent_core.capabilities import CapabilityRegistry
from app.services.agent_core.core_capabilities import (
    CoreCapabilityAvailability,
)
from app.services.external_capabilities.availability import (
    ExternalCapabilityAvailability,
)
from app.services.agent_core.controller_client import (
    PLAN_TASK_TOOL,
    ControllerClient,
    ControllerClientError,
    _default_model_factory,
)
from app.services.agent_core.prompt_contracts import (
    ControllerContextSnapshot,
    ControllerModelRequest,
    ConversationContextTurn,
    TrustedMemoryContext,
    TrustedReceiptContext,
)


def _request() -> ControllerModelRequest:
    return ControllerModelRequest(
        model_name="test-controller",
        current_user_message="我刚才说了什么，你怎么回答的？",
        context=ControllerContextSnapshot(
            conversation=[
                ConversationContextTurn(role="user", content="你好我是冰露"),
                ConversationContextTurn(
                    role="assistant",
                    content="你好，冰露！",
                ),
            ],
            memories=[
                TrustedMemoryContext(
                    schema_key="identity.self_reported_name",
                    fact="用户自述姓名为冰露",
                )
            ],
            receipts=[
                TrustedReceiptContext(
                    capability="remember_memory",
                    status="completed",
                    summary="姓名事实已创建",
                )
            ],
        ),
    )


def _enabled_registry() -> CapabilityRegistry:
    return CapabilityRegistry(
        core_availability=CoreCapabilityAvailability.all_enabled()
    )


class _BoundModel:
    def __init__(self, response: AIMessage) -> None:
        self.response = response
        self.schemas = None
        self.tool_choice = None
        self.messages = None

    def bind_tools(self, schemas, **kwargs):
        self.schemas = schemas
        self.tool_choice = kwargs.get("tool_choice")
        return self

    async def ainvoke(self, messages):
        self.messages = messages
        return self.response


class _FailingBoundModel(_BoundModel):
    async def ainvoke(self, messages):
        self.messages = messages
        raise TimeoutError("selected model timed out")


class ControllerClientTests(unittest.IsolatedAsyncioTestCase):
    def test_default_factory_explicitly_disables_thinking_by_default(self) -> None:
        model = object()
        with patch(
            "app.services.agent_core.controller_client.get_llm",
            return_value=model,
        ) as get_llm:
            result = _default_model_factory("configured-controller")

        self.assertIs(result, model)
        get_llm.assert_called_once_with(
            "configured-controller",
            thinking_mode=False,
        )

    def test_default_factory_honors_explicit_thinking_request(self) -> None:
        with patch(
            "app.services.agent_core.controller_client.get_llm",
            return_value=object(),
        ) as get_llm:
            _default_model_factory(
                "configured-controller",
                thinking_mode=True,
            )
        get_llm.assert_called_once_with(
            "configured-controller",
            thinking_mode=True,
        )

    async def test_explicit_search_cannot_be_bypassed_by_direct_model_answer(self) -> None:
        model = _BoundModel(AIMessage(content="我直接凭记忆回答。"))
        registry = CapabilityRegistry(
            availability=ExternalCapabilityAvailability(
                web_search=True,
                book_search=True,
            )
        )
        output = await ControllerClient(
            registry=registry,
            model_factory=lambda _name: model,
        ).decide(
            ControllerModelRequest(
                model_name="test-controller",
                current_user_message="请搜索本周人工智能的重要更新",
            )
        )

        self.assertEqual(output.mode, "capability_proposals")
        self.assertEqual(output.tool_calls[0].name, "web_search")
        self.assertIn("本周人工智能", output.tool_calls[0].arguments["query"])

    async def test_explicit_book_recommendation_uses_book_owner(self) -> None:
        model = _BoundModel(AIMessage(content="我直接推荐三本。"))
        registry = CapabilityRegistry(
            availability=ExternalCapabilityAvailability(
                web_search=True,
                book_search=True,
            )
        )
        output = await ControllerClient(
            registry=registry,
            model_factory=lambda _name: model,
        ).decide(
            ControllerModelRequest(
                model_name="test-controller",
                current_user_message="请推荐三本人工智能入门书",
            )
        )

        self.assertEqual(output.mode, "capability_proposals")
        self.assertEqual(output.tool_calls[0].name, "book_search")
        self.assertEqual(model.tool_choice, "auto")
        self.assertEqual(
            [schema["function"]["name"] for schema in model.schemas],
            ["book_search"],
        )
        parameters = model.schemas[0]["function"]["parameters"]
        self.assertIn("evidence_strategy", parameters["required"])
        self.assertEqual(
            parameters["$defs"]["EvidenceStrategy"]["required"],
            ["facets"],
        )
        self.assertEqual(
            output.tool_calls[0].arguments["mode"],
            "recommendation",
        )

    async def test_book_owner_uses_reference_anchors_without_speculative_themes(self) -> None:
        model = _BoundModel(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "book_search",
                        "args": {
                            "query": "类似参考书的新书",
                            "mode": "recommendation",
                            "themes": [],
                            "reference_titles": ["非暴力沟通", "小狗钱钱"],
                        },
                        "id": "call-books",
                        "type": "tool_call",
                    }
                ],
            )
        )
        registry = CapabilityRegistry(
            availability=ExternalCapabilityAvailability(
                web_search=True,
                book_search=True,
            )
        )

        output = await ControllerClient(
            registry=registry,
            model_factory=lambda _name: model,
        ).decide(
            ControllerModelRequest(
                model_name="test-controller",
                current_user_message=(
                    "请推荐几本类似《非暴力沟通》《小狗钱钱》的新书，"
                    "请用表格列出推荐理由。"
                ),
            )
        )

        arguments = output.tool_calls[0].arguments
        self.assertEqual(
            arguments["query"],
            "类似《非暴力沟通》、《小狗钱钱》的书 推荐",
        )
        self.assertEqual(arguments["themes"], [])
        self.assertEqual(
            arguments["excluded_titles"],
            ["非暴力沟通", "小狗钱钱"],
        )

    async def test_outside_shelf_recommendation_cannot_degrade_to_shelf_read(self) -> None:
        model = _BoundModel(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "bookshelf_read",
                        "args": {
                            "scope": "current",
                            "statuses": [],
                            "evaluations": [],
                            "query": "",
                            "limit": 20,
                        },
                        "id": "call-shelf",
                        "type": "tool_call",
                    },
                    {
                        "name": "book_search",
                        "args": {
                            "query": "类似参考书的新书",
                            "mode": "recommendation",
                            "evidence_strategy": {
                                "facets": [{"name": ""}]
                            },
                        },
                        "id": "call-books",
                        "type": "tool_call",
                    },
                ],
            )
        )
        registry = CapabilityRegistry(
            availability=ExternalCapabilityAvailability(
                web_search=True,
                book_search=True,
            )
        )

        output = await ControllerClient(
            registry=registry,
            model_factory=lambda _name: model,
        ).decide(
            ControllerModelRequest(
                model_name="test-controller",
                current_user_message=(
                    "请推荐几本我书架之外的新书，类似《非暴力沟通》"
                    "和《小狗钱钱》。"
                ),
            )
        )

        self.assertEqual([call.name for call in output.tool_calls], ["book_search"])
        arguments = output.tool_calls[0].arguments
        self.assertEqual(arguments["mode"], "recommendation")
        self.assertEqual(
            arguments["reference_titles"],
            ["非暴力沟通", "小狗钱钱"],
        )
        self.assertNotIn("evidence_strategy", arguments)

    async def test_explicit_search_falls_back_when_selected_model_fails(self) -> None:
        model = _FailingBoundModel(AIMessage(content=""))
        registry = CapabilityRegistry(
            availability=ExternalCapabilityAvailability(
                web_search=True,
                book_search=True,
            )
        )

        output = await ControllerClient(
            registry=registry,
            model_factory=lambda _name: model,
        ).decide(
            ControllerModelRequest(
                model_name="slow-controller",
                current_user_message="请联网查找本周人工智能的重要更新",
            )
        )

        self.assertEqual(output.mode, "capability_proposals")
        self.assertEqual(output.tool_calls[0].name, "web_search")

    async def test_book_fallback_uses_compact_reference_query(self) -> None:
        model = _FailingBoundModel(AIMessage(content=""))
        registry = CapabilityRegistry(
            availability=ExternalCapabilityAvailability(
                web_search=True,
                book_search=True,
            )
        )

        output = await ControllerClient(
            registry=registry,
            model_factory=lambda _name: model,
        ).decide(
            ControllerModelRequest(
                model_name="slow-controller",
                current_user_message=(
                    "请推荐几本类似《非暴力沟通》《小狗钱钱》的书，"
                    "请用Markdown表格列出作者和推荐理由。"
                ),
            )
        )

        arguments = output.tool_calls[0].arguments
        self.assertEqual(arguments["query"], "类似《非暴力沟通》、《小狗钱钱》的书 推荐")
        self.assertNotIn("Markdown", arguments["query"])
        self.assertEqual(
            arguments["reference_titles"],
            ["非暴力沟通", "小狗钱钱"],
        )

    async def test_whole_conversation_is_sent_and_direct_answer_is_parsed(
        self,
    ) -> None:
        model = _BoundModel(AIMessage(content="你说你叫冰露，我向你问好。"))
        with patch(
            "app.services.agent_core.controller_client."
            "model_history_projector.project",
            wraps=model_history_projector.project,
        ) as project:
            output = await ControllerClient(
                registry=_enabled_registry(),
                model_factory=lambda _name: model
            ).decide(_request())

        self.assertEqual(output.mode, "direct_answer")
        project.assert_called_once()
        self.assertIn("冰露", output.text)
        human_messages = [
            message.content
            for message in model.messages
            if isinstance(message, HumanMessage)
        ]
        self.assertEqual(
            human_messages,
            ["你好我是冰露", "我刚才说了什么，你怎么回答的？"],
        )
        self.assertTrue(
            any("trusted_memory_facts" in str(message.content) for message in model.messages)
        )

    async def test_capability_schema_is_generated_without_system_identity(
        self,
    ) -> None:
        model = _BoundModel(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "conversation_read",
                        "args": {
                            "target": "exchange",
                            "selection": "latest",
                            "count": 1,
                        },
                        "id": "call-read",
                        "type": "tool_call",
                    }
                ],
            )
        )
        output = await ControllerClient(
            registry=_enabled_registry(),
            model_factory=lambda _name: model
        ).decide(_request())

        self.assertEqual(output.mode, "capability_proposals")
        self.assertEqual(output.tool_calls[0].name, "conversation_read")
        serialized = str(model.schemas)
        for forbidden in ("user_id", "thread_id", "request_id", "memory_key"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(model.tool_choice, "auto")

    async def test_clarification_is_control_signal_not_capability(self) -> None:
        model = _BoundModel(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_clarification",
                        "args": {"question": "你希望我记住哪一个名字？"},
                        "id": "call-clarify",
                        "type": "tool_call",
                    }
                ],
            )
        )
        output = await ControllerClient(
            registry=_enabled_registry(),
            model_factory=lambda _name: model
        ).decide(_request())

        self.assertEqual(output.mode, "request_clarification")
        self.assertIn("哪一个名字", output.text)
        self.assertFalse(output.tool_calls)

    async def test_clarification_cannot_mix_with_business_capability(self) -> None:
        response = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "request_clarification",
                    "args": {"question": "请确认。"},
                    "id": "call-clarify",
                    "type": "tool_call",
                },
                {
                    "name": "search_memory",
                    "args": {"query": "name", "predicate": ""},
                    "id": "call-search",
                    "type": "tool_call",
                },
            ],
        )
        with self.assertRaisesRegex(
            ControllerClientError,
            "cannot be mixed",
        ):
            ControllerClient(
                registry=_enabled_registry(),
                model_factory=lambda _name: _BoundModel(response)
            ).parse(response)

    async def test_plan_task_is_typed_exclusive_control_signal(self) -> None:
        model = _BoundModel(
            AIMessage(
                content="我会先制定一个可恢复的两步任务。",
                tool_calls=[
                    {
                        "name": "plan_task",
                        "args": {
                            "goal": "查找并汇总最新图书",
                            "steps": [
                                {
                                    "step_key": "search",
                                    "title": "查找图书",
                                    "capability": "book_search",
                                    "arguments": {"query": "最新好评图书"},
                                },
                                {
                                    "step_key": "summarize",
                                    "title": "汇总结果",
                                    "capability": "research_start",
                                    "arguments": {"topic": "最新好评图书"},
                                    "depends_on": ["search"],
                                },
                            ],
                            "success_criteria": ["形成带来源的可读报告"],
                        },
                        "id": "call-task",
                        "type": "tool_call",
                    }
                ],
            )
        )
        output = await ControllerClient(
            registry=_enabled_registry(),
            model_factory=lambda _name: model
        ).decide(_request())

        self.assertEqual(output.mode, "task_plan_proposal")
        self.assertEqual(
            output.task_plan_proposal.goal,
            "查找并汇总最新图书",
        )
        self.assertEqual(len(output.task_plan_proposal.steps), 2)
        self.assertFalse(output.tool_calls)
        self.assertIn("制定", output.progress_text)
        self.assertEqual(
            PLAN_TASK_TOOL["function"]["parameters"]["title"],
            "ControllerTaskPlanProposal",
        )
        self.assertEqual(
            PLAN_TASK_TOOL["function"]["parameters"]["properties"]["steps"][
                "minItems"
            ],
            2,
        )
        self.assertEqual(
            output.task_plan_proposal.contract_version,
            "task-plan-v1",
        )
        serialized = str(PLAN_TASK_TOOL)
        self.assertNotIn("create_task_plan", serialized)
        for forbidden in (
            "user_id",
            "thread_id",
            "origin_request_id",
            "task_id",
            "plan_version_id",
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_plan_task_cannot_mix_with_business_capability(self) -> None:
        response = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "plan_task",
                    "args": {
                        "goal": "读取再检索",
                        "steps": [
                            {
                                "step_key": "read",
                                "title": "读对话",
                                "capability": "conversation_read",
                            }
                        ],
                    },
                    "id": "call-task",
                    "type": "tool_call",
                },
                {
                    "name": "conversation_read",
                    "args": {
                        "target": "exchange",
                        "selection": "latest",
                        "count": 1,
                    },
                    "id": "call-read",
                    "type": "tool_call",
                },
            ],
        )
        with self.assertRaisesRegex(
            ControllerClientError,
            "cannot be mixed",
        ):
            ControllerClient().parse(response)

    def test_plan_task_rejects_single_step_model_proposal(self) -> None:
        response = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "plan_task",
                    "args": {
                        "goal": "Read the prior exchange",
                        "steps": [
                            {
                                "step_key": "read",
                                "title": "Read the prior exchange",
                                "capability": "conversation_read",
                            }
                        ],
                    },
                    "id": "call-task",
                    "type": "tool_call",
                }
            ],
        )

        with self.assertRaisesRegex(
            ControllerClientError,
            "Controller proposal contract",
        ):
            ControllerClient().parse(response)

if __name__ == "__main__":
    unittest.main()
