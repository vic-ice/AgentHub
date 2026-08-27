from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone

from app.schemas.chat import UserInput
from app.services.agent_core.compiler import WorkflowCompiler
from app.services.agent_core.contracts import (
    CapabilityProposalBatch,
    ControllerOutput,
    ValidatedCapabilityProposal,
)
from app.services.agent_core.publication.commit import TurnPublicationCommitter
from app.services.agent_core.publication.contracts import (
    CommittedPublication,
    ReceiptEvidenceBundle,
)
from app.services.agent_core.publication.graph import (
    build_turn_execution_graph,
    project_public_execution_graph,
)
from app.services.agent_core.publication.service import TrustedPublisher
from app.services.agent_core.publication.response_view import (
    project_external_answer_view,
)
from app.services.agent_core.publication.stream import TrustedStreamSequencer
from app.services.agent_core.trusted_stream import TrustedControllerStream
from app.services.agent_core.receipt_projector import ReceiptContextProjector
from app.services.agent_core.turn_contracts import (
    ControllerRoundReceipt,
    TurnReceipt,
)
from app.services.agent_runtime.contracts import (
    ActionPlan,
    ActionReceipt,
    PlanReceipt,
    PlannedAction,
)
from app.services.agent_runtime.execution_graph import build_execution_graph
from app.services.conversation.journal_contracts import (
    ConversationJournalEvent,
)


def _plan(
    *,
    response_mode: str = "model",
    side_effect: bool = False,
) -> ActionPlan:
    return ActionPlan(
        plan_id="plan-r5",
        source="controller_proposal",
        route_type="slow_path",
        intent="r5",
        goal="形成可信回答",
        response_mode=response_mode,
        actions=[
            PlannedAction(
                action_id="internal-action-r5",
                capability="web",
                operation="web_search_v2",
                metadata={"side_effect": side_effect},
            )
        ],
    )


def _receipt(
    plan: ActionPlan,
    *,
    status: str = "completed",
    output: dict | None = None,
) -> PlanReceipt:
    return PlanReceipt(
        plan_id=plan.plan_id,
        request_id="request-r5",
        route_type=plan.route_type,
        intent=plan.intent,
        status=status,
        actions=[
            ActionReceipt(
                action_id=plan.actions[0].action_id,
                capability=plan.actions[0].capability,
                operation=plan.actions[0].operation,
                status=status,
                output=output,
                admitted=True,
            )
        ],
    )


def _web_output() -> dict:
    return {
        "result_mode": "web_evidence",
        "status": "ok",
        "query": "近期好评图书",
        "sources": [
            {
                "title": "来源一",
                "url": "https://example.com/one",
                "snippet": "第一条经过清洗的证据。",
                "published_date": "2026-07-01",
            },
            {
                "title": "来源二",
                "url": "https://example.com/two",
                "snippet": "第二条经过清洗的证据。",
                "published_date": "2026-07-02",
            },
            {
                "title": "来源三",
                "url": "https://example.com/three",
                "snippet": "第三条经过清洗的证据。",
                "published_date": "2026-07-03",
            },
        ],
        "provider_raw": "MUST_NOT_LEAK",
    }


class TrustedPublicationTests(unittest.TestCase):
    def test_balanced_book_synthesis_may_prioritize_admitted_items(self) -> None:
        plan = _plan().model_copy(
            update={
                "actions": [
                    PlannedAction(
                        action_id="book-action-coverage",
                        capability="books",
                        operation="book_search_v1",
                    )
                ]
            }
        )
        output = {
            "result_mode": "book_evidence",
            "status": "ok",
            "query": "双主题推荐",
            "response_depth": "balanced",
            "candidate_count": 2,
            "sources": [
                {
                    "title": "候选甲",
                    "url": "https://book.example/a",
                    "snippet": "甲的可信简介。",
                },
                {
                    "title": "候选乙",
                    "url": "https://book.example/b",
                    "snippet": "乙的可信简介。",
                },
            ],
            "items": [
                {
                    "title": "候选甲",
                    "theme": "主题甲",
                    "summary": "甲的可信简介。",
                    "catalog_url": "https://book.example/a",
                },
                {
                    "title": "候选乙",
                    "theme": "主题乙",
                    "summary": "乙的可信简介。",
                    "catalog_url": "https://book.example/b",
                },
            ],
        }
        receipt = _receipt(plan, output=output)

        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text="先推荐《候选甲》，它适合主题甲。",
            ),
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
        )

        self.assertIn("候选甲", answer.content)
        self.assertNotIn("其他已核验候选", answer.content)
        self.assertNotIn("候选乙", answer.content)

    def test_candidate_coverage_accepts_title_without_marketing_parenthetical(self) -> None:
        plan = _plan().model_copy(
            update={
                "actions": [
                    PlannedAction(
                        action_id="book-action-title-alias",
                        capability="books",
                        operation="book_search_v1",
                    )
                ]
            }
        )
        output = {
            "result_mode": "book_evidence",
            "status": "ok",
            "query": "说服沟通",
            "response_depth": "deep",
            "candidate_count": 1,
            "sources": [
                {
                    "title": "说服：沟通中的认知偏见与群体认同",
                    "url": "https://book.example/persuasion",
                    "snippet": "讨论说服中的认知偏见。",
                }
            ],
            "items": [
                {
                    "title": "说服：沟通中的认知偏见与群体认同（你越自信越可能落入陷阱）",
                    "theme": "说服沟通",
                    "summary": "讨论说服中的认知偏见。",
                    "catalog_url": "https://book.example/persuasion",
                }
            ],
        }
        receipt = _receipt(plan, output=output)

        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text="推荐《说服：沟通中的认知偏见与群体认同》。",
            ),
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
        )

        self.assertNotIn("其他已核验候选", answer.content)

    def test_rich_book_items_drive_presentation_and_admit_supporting_urls(self) -> None:
        plan = _plan().model_copy(
            update={
                "actions": [
                    PlannedAction(
                        action_id="book-action-rich",
                        capability="books",
                        operation="book_search_v1",
                    )
                ]
            }
        )
        output = {
            "result_mode": "book_evidence",
            "status": "ok",
            "query": "财商心理",
            "response_depth": "deep",
            "candidate_count": 1,
            "sources": [
                {
                    "title": "金钱心理学",
                    "url": "https://book.example/money",
                    "snippet": "作者：摩根·豪泽尔",
                }
            ],
            "coverage": [
                {
                    "theme": "财商心理",
                    "candidate_count": 1,
                    "status": "partial",
                }
            ],
            "items": [
                {
                    "title": "金钱心理学",
                    "authors": ["摩根·豪泽尔"],
                    "theme": "财商心理",
                    "summary": "讨论行为与长期金钱决策。",
                    "catalog_url": "https://book.example/money",
                    "evidence_sources": [
                        {
                            "title": "出版社简介",
                            "url": "https://publisher.example/money",
                            "snippet": "讨论行为与长期金钱决策。",
                        }
                    ],
                    "evidence_provider_count": 2,
                }
            ],
        }
        receipt = _receipt(plan, output=output)
        bundle = ReceiptEvidenceBundle(plan=plan, receipt=receipt)

        view = project_external_answer_view([bundle])

        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.response_depth, "deep")
        self.assertEqual(view.books[0].theme, "财商心理")
        self.assertEqual(view.coverage[0].status, "partial")
        self.assertEqual(
            view.books[0].supporting_sources[0].url,
            "https://publisher.example/money",
        )
        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text=(
                    "推荐《金钱心理学》，可参考"
                    "[出版社简介](https://publisher.example/money)。"
                ),
            ),
            evidence=[bundle],
        )
        self.assertEqual(answer.status, "completed")

    def test_synthesis_normalizes_admitted_url_and_strips_unadmitted_links(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text=(
                    "可参考[已核验来源](https://example.com/one/?tracking=1)，"
                    "补充站点不会作为证据：[未知来源](https://unknown.example/item)。"
                ),
            ),
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
        )

        self.assertIn("[已核验来源](https://example.com/one)", answer.content)
        self.assertIn("未知来源", answer.content)
        self.assertNotIn("unknown.example", answer.content)

    def test_direct_and_model_synthesis_are_distinct_modes(self) -> None:
        publisher = TrustedPublisher()
        direct = publisher.publish_direct(
            ControllerOutput(mode="direct_answer", text="你好。")
        )
        self.assertEqual(direct.publication_mode, "direct")
        self.assertFalse(direct.receipt_backed)

        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        synthesis = publisher.publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text=(
                    "## 推荐结论\n\n"
                    "- [来源一](https://example.com/one)：第一条证据。\n"
                    "- [来源二](https://example.com/two)：第二条证据。"
                ),
            ),
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
        )
        self.assertEqual(synthesis.publication_mode, "model_synthesis")
        self.assertTrue(synthesis.receipt_backed)
        self.assertEqual(synthesis.receipt_refs, ["internal-action-r5"])

    def test_synthesis_rejects_internal_runtime_text(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        with self.assertRaisesRegex(ValueError, "technical"):
            TrustedPublisher().publish_synthesis(
                ControllerOutput(
                    mode="direct_answer",
                    text="dependency did not complete: internal-action-r5",
                ),
                evidence=[
                    ReceiptEvidenceBundle(plan=plan, receipt=receipt)
                ],
            )

    def test_synthesis_rejects_thinking_markup(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        with self.assertRaisesRegex(ValueError, "technical"):
            TrustedPublisher().publish_synthesis(
                ControllerOutput(
                    mode="direct_answer",
                    text="我准备继续处理。</think>",
                ),
                evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            )

    def test_synthesis_rejects_plain_string_internal_monologue(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        with self.assertRaisesRegex(ValueError, "technical"):
            TrustedPublisher().publish_synthesis(
                ControllerOutput(
                    mode="direct_answer",
                    text=(
                        "我需要先分析用户需求，再检查约束和引用。\n"
                        "接下来我将起草最终回答，并确保看起来完整。"
                    ),
                ),
                evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            )

    def test_explicit_table_is_deterministically_completed_from_evidence(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text=(
                    "已经找到几条可核验资料，可以据此进行比较。"
                    "可参考[来源一](https://example.com/one)。"
                ),
            ),
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            user_request="请搜索并用表格对比",
        )

        self.assertIn("| 来源 | 摘要 |", answer.content)
        self.assertIn("| --- | --- |", answer.content)

    def test_web_synthesis_without_an_admitted_citation_is_rejected(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        with self.assertRaisesRegex(ValueError, "admitted source citation"):
            TrustedPublisher().publish_synthesis(
                ControllerOutput(
                    mode="direct_answer",
                    text="火星常住人口已达一百万人，来自最新官方普查。",
                ),
                evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            )

    def test_synthesis_rejects_raw_tool_invocation_text(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        publisher = TrustedPublisher()
        for leaked_text in (
            'book_search(query="任意图书")',
            'web_search({"query":"任意主题"})',
            '{"name":"book_search","arguments":{"query":"任意图书"}}',
        ):
            with self.subTest(leaked_text=leaked_text):
                with self.assertRaisesRegex(ValueError, "technical"):
                    publisher.publish_synthesis(
                        ControllerOutput(
                            mode="direct_answer",
                            text=leaked_text,
                        ),
                        evidence=[
                            ReceiptEvidenceBundle(plan=plan, receipt=receipt)
                        ],
                    )

    def test_book_synthesis_must_name_an_admitted_candidate(self) -> None:
        plan = _plan().model_copy(
            update={
                "actions": [
                    PlannedAction(
                        action_id="book-action",
                        capability="books",
                        operation="book_search_v1",
                    )
                ]
            }
        )
        receipt = _receipt(
            plan,
            output={
                "status": "ok",
                "query": "通用推荐请求",
                "candidate_count": 1,
                "sources": [
                    {
                        "title": "可核验候选作品",
                        "url": "https://book.example/admitted",
                        "snippet": "作者：示例作者",
                    }
                ],
            },
        )
        with self.assertRaisesRegex(ValueError, "candidate title"):
            TrustedPublisher().publish_synthesis(
                ControllerOutput(
                    mode="direct_answer",
                    text="我是一个模型，但没有使用搜索结果。",
                ),
                evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            )
        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text="推荐《可核验候选作品》，因为它符合当前方向。",
            ),
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
        )
        self.assertEqual(answer.status, "completed")

    def test_verified_book_table_keeps_model_prose_and_binds_source_link(self) -> None:
        plan = _plan().model_copy(
            update={
                "actions": [
                    PlannedAction(
                        action_id="book-table-action",
                        capability="books",
                        operation="book_search_v1",
                    )
                ]
            }
        )
        receipt = _receipt(
            plan,
            output={
                "status": "ok",
                "query": "通用推荐请求",
                "candidate_count": 1,
                "sources": [
                    {
                        "title": "可核验候选作品",
                        "url": "https://book.example/admitted",
                        "snippet": "本书通过具体案例讲解沟通方法。",
                    }
                ],
            },
        )
        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text=(
                    "| 书名 | 推荐理由 |\n"
                    "| --- | --- |\n"
                    "| 可核验候选作品 | 这是模型写出的具体取舍，不应被固定模板覆盖。 |"
                ),
            ),
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            user_request="请用Markdown表格，列为书名、推荐理由。",
        )

        self.assertIn("这是模型写出的具体取舍", answer.content)
        self.assertIn(
            "[可核验候选作品](https://book.example/admitted)",
            answer.content,
        )
        self.assertNotIn("以下对比仅使用本轮已核验书目", answer.content)

    def test_synthesis_does_not_hardcode_a_markdown_report_shape(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text=(
                    "我更建议先看[来源一](https://example.com/one)，"
                    "它最贴近你现在关注的方向。"
                ),
            ),
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
        )
        self.assertEqual(answer.status, "completed")
        self.assertNotIn("研究结论", answer.content)

    def test_external_fallback_is_a_user_view_not_a_receipt_dump(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        answer = TrustedPublisher().publish_evidence_fallback(
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)]
        )
        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertIn("🔎", answer.content)
        self.assertIn("[来源一](https://example.com/one)", answer.content)
        self.assertNotIn("回执", answer.content)
        self.assertNotIn("证据限制", answer.content)

    def test_external_fallback_preserves_explicit_table_contract(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        request = "请用Markdown表格，列为来源、摘要。"
        answer = TrustedPublisher().publish_evidence_fallback(
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            user_request=request,
        )
        self.assertIsNotNone(answer)
        assert answer is not None
        from app.services.publication_safety import table_contract_satisfied

        self.assertTrue(
            table_contract_satisfied(answer.content, request=request)
        )
        self.assertNotIn("### 🔎 相关资料", answer.content)

    def test_book_table_fallback_does_not_use_author_biography_as_reason(self) -> None:
        plan = _plan().model_copy(
            update={
                "actions": [
                    PlannedAction(
                        action_id="book-author-bio",
                        capability="books",
                        operation="book_search_v1",
                    )
                ]
            }
        )
        receipt = _receipt(
            plan,
            output={
                "result_mode": "book_evidence",
                "status": "ok",
                "query": "理财入门",
                "candidate_count": 1,
                "sources": [
                    {
                        "title": "示例理财书",
                        "url": "https://book.example/money",
                        "snippet": "作者：示例作者",
                    }
                ],
                "items": [
                    {
                        "title": "示例理财书",
                        "authors": ["示例作者"],
                        "summary": (
                            "## 作者简介\n\n示例作者1960年出生于某地，"
                            "是著名投资家、企业家、演说家和畅销书作家。"
                            "他创立多家公司，代表作畅销多年。"
                        ),
                        "catalog_url": "https://book.example/money",
                    }
                ],
            },
        )
        request = (
            "请用Markdown表格，列为书名、作者、适合人群、"
            "推荐理由、局限。"
        )

        answer = TrustedPublisher().publish_evidence_fallback(
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            user_request=request,
        )

        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertNotIn("1960年", answer.content)
        self.assertNotIn("著名投资家", answer.content)
        self.assertIn("缺少可核验的内容型推荐依据", answer.content)
        self.assertIn("标题和内容摘要未明确标注适合人群", answer.content)
        self.assertIn("缺少内容型来源与明确适读标记", answer.content)

    def test_book_table_fallback_uses_content_after_author_biography(self) -> None:
        plan = _plan().model_copy(
            update={
                "actions": [
                    PlannedAction(
                        action_id="book-content-after-bio",
                        capability="books",
                        operation="book_search_v1",
                    )
                ]
            }
        )
        receipt = _receipt(
            plan,
            output={
                "result_mode": "book_evidence",
                "status": "ok",
                "query": "人工智能入门",
                "candidate_count": 1,
                "sources": [
                    {
                        "title": "人工智能入门",
                        "url": "https://book.example/ai",
                        "snippet": "作者：示例作者",
                    }
                ],
                "items": [
                    {
                        "title": "人工智能入门",
                        "authors": ["示例作者"],
                        "summary": (
                            "## 作者简介 示例作者1960年出生于某地，"
                            "是著名教授和畅销书作家。 [...] "
                            "## 内容简介 纸质版 24.70元 "
                            "本书通过故事和案例讲解人工智能基础知识，"
                            "适合零基础读者。"
                        ),
                        "catalog_url": "https://book.example/ai",
                    }
                ],
            },
        )
        request = (
            "请用Markdown表格，列为书名、作者、适合人群、"
            "推荐理由、局限。"
        )

        answer = TrustedPublisher().publish_evidence_fallback(
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            user_request=request,
        )

        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertNotIn("1960年", answer.content)
        self.assertNotIn("24.70元", answer.content)
        self.assertIn("通过故事和案例讲解人工智能基础知识", answer.content)
        self.assertIn("零基础或初学者", answer.content)
        self.assertIn("单一公开来源", answer.content)

    def test_valid_model_book_table_may_prioritize_a_verified_subset(self) -> None:
        plan = _plan().model_copy(
            update={
                "actions": [
                    PlannedAction(
                        action_id="book-complete-table",
                        capability="books",
                        operation="book_search_v1",
                    )
                ]
            }
        )
        receipt = _receipt(
            plan,
            output={
                "result_mode": "book_evidence",
                "status": "ok",
                "query": "沟通与理财",
                "candidate_count": 2,
                "sources": [
                    {
                        "title": "沟通练习",
                        "url": "https://book.example/communication",
                        "snippet": "本书通过案例讲解沟通方法，适合初学者。",
                    },
                    {
                        "title": "理财入门",
                        "url": "https://book.example/finance",
                        "snippet": "本书介绍日常理财原理和实践方法。",
                    },
                ],
                "items": [
                    {
                        "title": "沟通练习",
                        "authors": ["甲"],
                        "summary": "本书通过案例讲解沟通方法，适合初学者。",
                        "catalog_url": "https://book.example/communication",
                    },
                    {
                        "title": "理财入门",
                        "authors": ["乙"],
                        "summary": "本书介绍日常理财原理和实践方法。",
                        "catalog_url": "https://book.example/finance",
                    },
                ],
            },
        )
        request = "请用Markdown表格，列为书名、作者、推荐理由。"
        draft = (
            "模型自写内容\n\n"
            "| 书名 | 作者 | 推荐理由 |\n"
            "| --- | --- | --- |\n"
            "| 沟通练习 | 甲 | 通过案例讲解沟通方法 |"
        )

        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(mode="direct_answer", text=draft),
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            user_request=request,
        )

        self.assertIn("沟通练习", answer.content)
        self.assertNotIn("理财入门", answer.content)
        self.assertIn("模型自写内容", answer.content)
        self.assertIn("通过案例讲解沟通方法", answer.content)
        self.assertIn(
            "[沟通练习](https://book.example/communication)",
            answer.content,
        )

    def test_synthesis_rejects_internal_receipt_language(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        with self.assertRaisesRegex(ValueError, "technical"):
            TrustedPublisher().publish_synthesis(
                ControllerOutput(
                    mode="direct_answer",
                    text="根据本轮通过回执校验的结果，推荐来源一。",
                ),
                evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
            )

    def test_model_synthesis_can_use_side_effect_receipt_as_evidence(self) -> None:
        plan = _plan(side_effect=True)
        receipt = _receipt(plan, output=_web_output())
        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text=(
                    "## 结果\n\n"
                    "- [来源一](https://example.com/one)：证据。"
                ),
            ),
            evidence=[
                ReceiptEvidenceBundle(plan=plan, receipt=receipt)
            ],
        )
        self.assertTrue(answer.receipt_backed)

        with self.assertRaisesRegex(ValueError, "deterministic receipt"):
            TrustedPublisher().publish_synthesis(
                ControllerOutput(
                    mode="direct_answer",
                    text="已经更新你的阅读状态。",
                ),
                evidence=[
                    ReceiptEvidenceBundle(plan=plan, receipt=receipt)
                ],
            )

    def test_waiting_receipt_only_publishes_clarification(self) -> None:
        plan = ActionPlan(
            plan_id="waiting-plan",
            source="controller_proposal",
            route_type="slow_path",
            intent="remember",
            goal="记住名字",
            response_mode="receipt",
            actions=[
                PlannedAction(
                    action_id="remember",
                    capability="memory",
                    operation="remember_memory_v2",
                    metadata={"side_effect": True},
                )
            ],
        )
        receipt = PlanReceipt(
            plan_id=plan.plan_id,
            request_id="request-r5",
            route_type=plan.route_type,
            intent=plan.intent,
            status="waiting",
            actions=[
                ActionReceipt(
                    action_id="remember",
                    capability="memory",
                    operation="remember_memory_v2",
                    status="waiting",
                    admitted=True,
                    output={
                        "status": "clarification_required",
                        "clarification_question": "你的名字是什么？",
                    },
                )
            ],
        )
        answer = TrustedPublisher().publish_deterministic(plan, receipt)
        self.assertEqual(answer.status, "clarification_required")
        self.assertEqual(answer.publication_mode, "deterministic_receipt")
        self.assertNotIn("完成", answer.content)

    def test_compiler_orders_read_after_side_effect_in_one_batch(
        self,
    ) -> None:
        plan = WorkflowCompiler().compile(
            CapabilityProposalBatch(
                proposals=[
                    ValidatedCapabilityProposal(
                        call_id="remember",
                        capability="remember_memory",
                        side_effect=True,
                    ),
                    ValidatedCapabilityProposal(
                        call_id="web",
                        capability="web_search",
                        side_effect=False,
                    ),
                ]
            ),
            goal="记住名字并查资料",
        )
        self.assertEqual(plan.response_mode, "model")
        self.assertEqual(
            plan.actions[1].depends_on,
            [plan.actions[0].action_id],
        )


class TrustedEvidenceProjectionTests(unittest.TestCase):
    def test_external_receipt_is_typed_and_raw_fields_are_dropped(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        projected = ReceiptContextProjector().project(receipt)
        terminal = projected[-1]
        dumped = terminal.model_dump_json()
        self.assertEqual(terminal.result_mode, "web_evidence")
        self.assertEqual(len(terminal.sources), 3)
        self.assertNotIn("provider_raw", dumped)
        self.assertNotIn("MUST_NOT_LEAK", dumped)


class TrustedStreamProtocolTests(unittest.TestCase):
    def test_clarification_turn_projects_waiting_response(self) -> None:
        output = ControllerOutput(
            mode="request_clarification",
            text="请告诉我需要记住的名字。",
        )
        answer = TrustedPublisher().publish_direct(output)
        turn = TurnReceipt(
            status="clarification_required",
            request_id="request-waiting",
            rounds=[
                ControllerRoundReceipt(
                    round_no=1,
                    output=output,
                    answer=answer,
                )
            ],
            final_answer=answer,
        )

        internal = build_turn_execution_graph(
            turn,
            request_id=turn.request_id,
        )
        public = project_public_execution_graph(internal)

        self.assertEqual(internal.nodes[-1].status, "waiting")
        self.assertEqual(public.nodes[-1].status, "waiting")

    def test_waiting_receipt_remains_waiting_in_graphs(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, status="waiting")

        internal = build_execution_graph(plan, receipt)
        public = project_public_execution_graph(internal)

        self.assertEqual(internal.nodes[1].status, "waiting")
        self.assertEqual(public.nodes[1].status, "waiting")

    def test_public_graph_keeps_authoritative_edges_without_internal_ids(
        self,
    ) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        graph = project_public_execution_graph(
            build_execution_graph(plan, receipt)
        )
        dumped = graph.model_dump_json()
        self.assertEqual(
            [(edge.source_id, edge.target_id) for edge in graph.edges],
            [("turn", "step-1"), ("step-1", "answer")],
        )
        self.assertNotIn("internal-action-r5", dumped)
        self.assertNotIn("provider_raw", dumped)

    def test_answer_completed_requires_committed_journal_event(self) -> None:
        plan = _plan()
        receipt = _receipt(plan, output=_web_output())
        answer = TrustedPublisher().publish_synthesis(
            ControllerOutput(
                mode="direct_answer",
                text=(
                    "## 推荐结论\n\n"
                    "- [来源一](https://example.com/one)：证据。"
                ),
            ),
            evidence=[ReceiptEvidenceBundle(plan=plan, receipt=receipt)],
        )
        turn = TurnReceipt(
            status="completed",
            request_id="request-r5",
            rounds=[
                ControllerRoundReceipt(
                    round_no=1,
                    output=ControllerOutput(
                        mode="capability_proposals",
                        tool_calls=[
                            {
                                "call_id": "web",
                                "name": "web_search",
                                "arguments": {"query": "近期好评图书"},
                            }
                        ],
                    ),
                    plan=plan,
                    receipt=receipt,
                ),
                ControllerRoundReceipt(
                    round_no=2,
                    output=ControllerOutput(
                        mode="direct_answer",
                        text=answer.content,
                    ),
                    answer=answer,
                ),
            ],
            plan_receipts=[receipt],
            final_answer=answer,
        )
        sequencer = TrustedStreamSequencer(request_id="request-r5")
        started = sequencer.turn_started()
        self.assertEqual(started.sequence, 1)
        self.assertEqual(started.type, "turn.started")
        with self.assertRaisesRegex(ValueError, "committed"):
            sequencer.answer_completed(answer=answer, committed=None)

        event = ConversationJournalEvent.model_validate(
            {
                "id": "2fd42c87-694b-45a6-ae9a-cc90f5685ef4",
                "user_id": "0564cc32-d249-4ed8-ad25-05835fc82aaf",
                "thread_id": "c418a420-013d-4194-a7c4-ffddf6c1f700",
                "request_id": "request-r5",
                "exchange_id": "3aa80a90-560f-44da-85f1-60b62b9ded61",
                "sequence_no": 2,
                "event_type": "assistant_published",
                "role": "assistant",
                "content": answer.content,
                "receipt_refs": answer.receipt_refs,
                "created_at": "2026-07-30T00:00:00Z",
            }
        )
        graph_event = sequencer.graph_snapshot(
            project_public_execution_graph(
                build_execution_graph(plan, receipt)
            )
        )
        committed = CommittedPublication(
            message={
                "type": "ai",
                "content": answer.content,
                "request_id": "request-r5",
            },
            journal_event=event,
            execution_graph=build_execution_graph(plan, receipt),
        )
        completed = sequencer.answer_completed(
            answer=turn.final_answer,
            committed=committed,
        )
        self.assertEqual(graph_event.sequence, 2)
        self.assertEqual(completed.sequence, 3)
        self.assertEqual(completed.type, "answer.completed")
        self.assertEqual(
            completed.content["journal_sequence"],
            event.sequence_no,
        )


class PublicationCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_started_precedes_database_or_model_work(self) -> None:
        user_input = UserInput(
            content="你好",
            user_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
            request_id="request-r5-first-event",
        )
        stream = TrustedControllerStream(
            database_factory=lambda: (_ for _ in ()).throw(
                AssertionError(
                    "database opened before turn.started"
                )
            )
        ).generate(user_input)
        first = await anext(stream)
        await stream.aclose()
        self.assertIn('"type": "turn.started"', first)
        self.assertIn('"sequence": 1', first)

    async def test_commit_receipt_is_returned_only_after_db_commit(
        self,
    ) -> None:
        user_id = uuid.uuid4()
        thread_id = uuid.uuid4()

        class _DatabaseSession:
            committed = False

            async def commit(self):
                self.committed = True

        db = _DatabaseSession()

        class _Journal:
            async def record_assistant_message(
                self,
                db_session,
                *,
                user_input,
                message,
            ):
                self.assert_not_committed = not db_session.committed
                return ConversationJournalEvent(
                    id=uuid.uuid4(),
                    user_id=user_input.user_id,
                    thread_id=user_input.thread_id,
                    request_id=user_input.request_id,
                    exchange_id=uuid.uuid4(),
                    sequence_no=2,
                    event_type="assistant_published",
                    role="assistant",
                    content=message.content,
                    receipt_refs=[],
                    created_at=datetime.now(timezone.utc),
                )

        trace_observations: list[bool] = []

        async def _trace_writer(
            db_session,
            *,
            user_input,
            message,
            graph,
            model_name,
        ):
            del user_input, message, graph, model_name
            trace_observations.append(db_session.committed)

        answer = TrustedPublisher().publish_direct(
            ControllerOutput(mode="direct_answer", text="可信回答。")
        )
        journal = _Journal()
        committed = await TurnPublicationCommitter(
            journal=journal,
            trace_writer=_trace_writer,
        ).commit(
            db,  # type: ignore[arg-type]
            user_input=UserInput(
                content="你好",
                user_id=user_id,
                thread_id=thread_id,
                request_id="request-r5-commit",
            ),
            answer=answer,
            turn=None,
            model_name="fixture",
            agent_mode="plain_chat",
        )
        self.assertTrue(journal.assert_not_committed)
        self.assertEqual(trace_observations, [False])
        self.assertTrue(db.committed)
        self.assertEqual(
            committed.journal_event.event_type,
            "assistant_published",
        )
        self.assertEqual(
            committed.message.custom_data["agent_mode"],
            "plain_chat",
        )


if __name__ == "__main__":
    unittest.main()
