from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from unittest import mock

from app.services.research.contracts import (
    ResearchEvidence,
    ResearchRun,
    ResearchStateResult,
    ResearchStateSnapshot,
)
from app.services.research.loop.contracts import ResearchLoopBudget
from app.services.research.reviewer import (
    build_evolving_workspace,
    review_research_state,
)


BUDGET = ResearchLoopBudget(
    max_search_rounds=2,
    max_results_per_round=5,
    max_records_per_round=3,
    min_independent_sources=1,
)


def _state(objective, claims, gaps=None, conflicts=None):
    run_id = uuid.uuid4()
    user_id = uuid.uuid4()
    return ResearchStateResult(
        run=ResearchRun(
            id=run_id,
            user_id=user_id,
            objective=objective,
            status="active",
        ),
        state=ResearchStateSnapshot(
            run_id=run_id,
            objective=objective,
            gaps=gaps or [],
            conflicts=conflicts or [],
            exhausted_queries=[],
            next_actions=["evaluate_research_gaps"],
        ),
        steps=[],
        evidence=[
            ResearchEvidence(
                run_id=run_id,
                source_type="web",
                source_title=f"source-{index}",
                source_url=f"https://example.com/{index}",
                claim=claim,
                excerpt=claim,
                quality=quality,
                relevance=3,
            )
            for index, (claim, quality) in enumerate(claims)
        ],
    )


def _fake_llm(payload: dict):
    class FakeModel:
        async def ainvoke(self, prompt):
            del prompt
            return type(
                "Resp",
                (),
                {"content": json.dumps(payload, ensure_ascii=False)},
            )()

    return lambda model_id, thinking_mode=None: FakeModel()


class EvolvingWorkspaceTests(unittest.TestCase):
    def test_workspace_distills_evidence_without_raw_observations(self):
        state = _state(
            "推荐一本机器学习入门书",
            [("《机器学习》是入门经典。", "high")],
        )
        workspace = build_evolving_workspace(state, round_index=1)
        self.assertEqual(len(workspace.confirmed_facts), 1)
        self.assertEqual(workspace.confirmed_facts[0].confidence, "high")
        self.assertIn("source-0", workspace.confirmed_facts[0].source)
        self.assertEqual(workspace.step_count, 1)

    def test_workspace_dedups_and_bounds(self):
        claims = [("《机器学习》是经典。", "medium")] * 3
        state = _state("x", claims)
        workspace = build_evolving_workspace(state, round_index=1)
        self.assertEqual(len(workspace.confirmed_facts), 1)

    def test_workspace_carries_gaps_and_conflicts(self):
        state = _state(
            "x",
            [("《机器学习》是经典。", "medium")],
            gaps=["缺少对比"],
            conflicts=["评分矛盾"],
        )
        workspace = build_evolving_workspace(state, round_index=1)
        self.assertEqual(workspace.information_gaps, ["缺少对比"])
        self.assertEqual(workspace.conflicts, ["评分矛盾"])


class ReviewerTests(unittest.TestCase):
    def _review(self, state, payload=None, raise_call=False, model_id="m"):
        if raise_call:

            def boom(model_id, thinking_mode=None):
                raise RuntimeError("model unavailable")

            factory = boom
        else:
            factory = _fake_llm(payload or {})
        with mock.patch(
            "app.infra.llm.get_llm",
            side_effect=factory,
        ), mock.patch(
            "app.services.research.reviewer._resolve_model_id",
            return_value=model_id,
        ):
            return asyncio.run(
                review_research_state(
                    state,
                    round_index=1,
                    budget=BUDGET,
                    model_id=model_id,
                )
            )

    def test_valid_review_is_used(self):
        state = _state(
            "哪个框架最适合初学者",
            [("PyTorch 提供动态计算图。", "medium")],
        )
        review = self._review(
            state,
            {
                "verdict": "insufficient",
                "known_summary": "已知框架介绍",
                "missing_questions": ["哪个框架更适合初学者？"],
                "next_subquestions": ["PyTorch vs TensorFlow 初学者对比"],
                "conflicts": [],
                "stop_reason": "缺少对比",
                "reasons": ["证据未对比"],
            },
        )
        self.assertEqual(review.provider, "runtime_llm")
        self.assertEqual(review.verdict, "insufficient")
        self.assertEqual(review.missing_questions, ["哪个框架更适合初学者？"])
        self.assertEqual(review.next_subquestions, ["PyTorch vs TensorFlow 初学者对比"])
        self.assertIn("evolving_report", review.metadata)

    def test_model_failure_falls_back_to_rules(self):
        state = _state(
            "x",
            [("a", "medium")],
            gaps=["缺少评价"],
        )
        review = self._review(state, raise_call=True)
        self.assertEqual(review.provider, "deterministic")
        self.assertEqual(review.verdict, "insufficient")
        self.assertEqual(review.missing_questions, ["缺少评价"])

    def test_invalid_json_falls_back(self):
        state = _state("x", [("a", "medium")], gaps=[])
        review = self._review(state, payload={"verdict": "maybe"})
        self.assertEqual(review.provider, "deterministic")
        self.assertEqual(review.verdict, "sufficient")

    def test_model_cannot_mark_sufficient_while_evidence_gaps_remain(self):
        state = _state(
            "推荐一些可靠候选",
            [("某候选被一个来源列入书单。", "low")],
            gaps=["insufficient_evidence_quality"],
        )
        review = self._review(
            state,
            {
                "verdict": "sufficient",
                "known_summary": "已有一个候选",
                "missing_questions": [],
                "next_subquestions": [],
                "conflicts": [],
                "stop_reason": "已有结果",
                "reasons": [],
            },
        )
        self.assertEqual(review.provider, "deterministic")
        self.assertEqual(review.verdict, "insufficient")
        self.assertEqual(
            review.missing_questions,
            ["insufficient_evidence_quality"],
        )

    def test_invalid_review_output_is_not_retried_four_times(self):
        state = _state("x", [("足够长的可核验候选事实。", "medium")], gaps=[])
        calls = 0

        class InvalidModel:
            async def ainvoke(self, prompt):
                nonlocal calls
                del prompt
                calls += 1
                return type("Resp", (), {"content": "not json"})()

        with mock.patch(
            "app.infra.llm.get_llm",
            return_value=InvalidModel(),
        ), mock.patch(
            "app.services.research.reviewer._resolve_model_id",
            return_value="m",
        ):
            review = asyncio.run(
                review_research_state(
                    state,
                    round_index=1,
                    budget=BUDGET,
                    model_id="m",
                )
            )
        self.assertEqual(calls, 1)
        self.assertEqual(review.provider, "deterministic")

    def test_no_evidence_short_circuits_without_model(self):
        state = _state("x", [])

        def boom(model_id, thinking_mode=None):
            raise AssertionError("model must not be called")

        with mock.patch(
            "app.infra.llm.get_llm",
            side_effect=boom,
        ):
            review = asyncio.run(
                review_research_state(
                    state,
                    round_index=1,
                    budget=BUDGET,
                )
            )
        self.assertEqual(review.provider, "deterministic")
        self.assertEqual(review.verdict, "insufficient")

    def test_verdict_enums_and_bounds(self):
        state = _state("x", [("a", "medium")])
        review = self._review(
            state,
            {
                "verdict": "budget_exhausted",
                "missing_questions": ["q" + str(i) for i in range(10)],
                "next_subquestions": ["n" + str(i) for i in range(10)],
                "conflicts": ["c" + str(i) for i in range(10)],
            },
        )
        self.assertEqual(review.verdict, "budget_exhausted")
        self.assertLessEqual(len(review.missing_questions), 5)
        self.assertLessEqual(len(review.next_subquestions), 3)
        self.assertLessEqual(len(review.conflicts), 5)


class QueuePlanningTests(unittest.TestCase):
    def setUp(self):
        from app.services.research.deep_research_runner import (
            _enqueue_subquestions,
            _normalize_query,
            _plan_next_task,
        )

        self._plan = _plan_next_task
        self._enqueue = _enqueue_subquestions
        self._normalize = _normalize_query

    def test_pending_subquestion_drives_next_round(self):
        task = self._plan(
            objective="推荐机器学习入门书",
            round_index=2,
            budget=BUDGET,
            previous_assessment=None,
            pending_subquestions=["《机器学习》难度分级 初学者"],
            used_queries=set(),
        )
        self.assertIsNotNone(task)
        self.assertTrue(task.should_search)
        self.assertEqual(task.purpose, "reviewer_gap")
        self.assertEqual(
            task.query,
            "site:book.douban.com/subject/ 《机器学习》难度分级 初学者",
        )
        self.assertEqual(
            task.include_url_prefixes,
            ["https://book.douban.com/subject/"],
        )

    def test_used_subquestion_is_skipped(self):
        used = self._normalize("已搜过的子问题")
        task = self._plan(
            objective="x",
            round_index=2,
            budget=BUDGET,
            previous_assessment=None,
            pending_subquestions=["已搜过的子问题", "新子问题"],
            used_queries={used},
        )
        self.assertIsNotNone(task)
        self.assertEqual(task.query, self._normalize("新子问题"))

    def test_empty_queue_falls_back_to_planner(self):
        task = self._plan(
            objective="推荐一本书",
            round_index=1,
            budget=BUDGET,
            previous_assessment=None,
            pending_subquestions=[],
            used_queries=set(),
        )
        self.assertIsNotNone(task)
        self.assertTrue(task.should_search)
        self.assertNotEqual(task.purpose, "reviewer_gap")

    def test_enqueue_dedups_and_bounds(self):
        pending = ["q1"]
        result = self._enqueue(
            pending,
            ["q1", "q2", "q2", "q3", "q4", "q5", "q6", "q7"],
            used_queries=set(),
        )
        self.assertEqual(result, ["q1", "q2", "q3", "q4", "q5", "q6"])

    def test_enqueue_skips_used_subquestions(self):
        used = self._normalize("已用过")
        result = self._enqueue(
            [],
            ["已用过", "新问题"],
            used_queries={used},
        )
        self.assertEqual(result, [self._normalize("新问题")])

    def test_normalize_collapses_whitespace_and_case(self):
        self.assertEqual(
            self._normalize("  PyTorch   vs  TensorFlow  "),
            "pytorch vs tensorflow",
        )


class ReportWorkspaceProjectionTests(unittest.TestCase):
    def test_workspace_projection_is_bounded(self):
        from app.services.research.publication.report_writer import (
            _workspace_from_review,
        )

        review = {
            "missing_questions": ["缺少对比"],
            "metadata": {
                "evolving_report": {
                    "objective": "x",
                    "confirmed_facts": [
                        {"content": "书推荐", "confidence": "high", "source": "s"}
                    ],
                    "information_gaps": ["gap1"],
                    "conflicts": ["conflict1"],
                }
            },
        }
        workspace = _workspace_from_review(review)
        self.assertEqual(workspace["confirmed_facts"][0]["confidence"], "high")
        self.assertEqual(workspace["information_gaps"], ["gap1"])
        self.assertEqual(workspace["conflicts"], ["conflict1"])
        self.assertEqual(workspace["missing_questions"], ["缺少对比"])

    def test_workspace_projection_handles_none_and_junk(self):
        from app.services.research.publication.report_writer import (
            _workspace_from_review,
        )

        self.assertEqual(_workspace_from_review(None), {})
        self.assertEqual(_workspace_from_review({"metadata": {}}), {
            "objective": "",
            "confirmed_facts": [],
            "information_gaps": [],
            "conflicts": [],
            "missing_questions": [],
        })
        junk = {"metadata": {"evolving_report": {"confirmed_facts": "nope"}}}
        self.assertEqual(_workspace_from_review(junk)["confirmed_facts"], [])


class FreeformRenderTests(unittest.TestCase):
    def test_freeform_extracts_and_gates(self):
        from app.services.research.publication.report_writer import (
            _render_freeform,
        )

        body = (
            "用户要求写报告。分析事实与结构规划。"
            "起草报告：\n# 2024年机器学习入门书推荐研究报告\n\n"
            "## 结论\n推荐书籍覆盖经典与实战，适合不同背景初学者。 [1] [2]"
            + "推荐书籍覆盖经典与实战，适合不同背景初学者。 [1] " * 8
            + "\n\n## 主要发现\n《机器学习》是入门经典。 [1] "
            + "《机器学习》是入门经典。 [1] " * 8
        )
        rendered, _cited, ok = _render_freeform(
            body,
            objective="推荐机器学习入门书",
            evidence=[
                {
                    "source_id": "s1",
                    "source_title": "T",
                    "source_url": "https://a.com",
                    "claim": "推荐 [1]",
                }
            ],
            language="zh-CN",
        )
        self.assertTrue(ok)
        self.assertTrue(rendered.startswith("# 2024"))

    def test_freeform_rejects_planning_only(self):
        from app.services.research.publication.report_writer import (
            _render_freeform,
        )

        rendered, _cited, ok = _render_freeform(
            "用户要求写报告，分析事实。",
            objective="x",
            evidence=[],
            language="zh-CN",
        )
        self.assertFalse(ok)
        self.assertEqual(rendered, "")

    def test_evidence_report_without_any_citation_is_rejected(self):
        from app.services.research.publication.report_writer import (
            _render_final_deliverable,
        )

        rendered, cited, ok = _render_final_deliverable(
            "# 结论\n\n这是一个长度足够但完全没有引用支持的研究结论。" * 4,
            evidence=[
                {
                    "source_id": "s1",
                    "source_title": "来源",
                    "source_url": "https://example.com",
                }
            ],
            language="zh-CN",
        )
        self.assertFalse(ok)
        self.assertEqual(cited, [])
        self.assertTrue(rendered)

    def test_model_authored_sources_are_replaced_by_canonical_sources(self):
        from app.services.research.publication.report_writer import (
            _render_final_deliverable,
        )

        rendered, cited, ok = _render_final_deliverable(
            (
                "# 报告\n\n这是有证据支持的事实结论，并且正文长度足以通过最终交付门禁。 [1]\n\n"
                "## 说明\n这部分只用于说明报告正文仍由证据约束，不包含第二套来源编号。 [1]\n\n"
                "**来源：**\n1. 模型伪造来源"
            ),
            evidence=[
                {
                    "source_id": "s1",
                    "source_title": "权威来源",
                    "source_url": "https://example.com/source",
                }
            ],
            language="zh-CN",
        )
        self.assertTrue(ok)
        self.assertEqual(cited, ["s1"])
        self.assertNotIn("模型伪造来源", rendered)
        self.assertEqual(rendered.count("### 🔗 参考来源"), 1)
        self.assertNotIn("## 研究结论", rendered)

    def test_malformed_source_url_and_its_citation_are_not_published(self):
        from app.services.research.publication.report_writer import (
            _render_final_deliverable,
        )

        rendered, cited, ok = _render_final_deliverable(
            (
                "# 推荐结果 📚\n\n"
                "有效资料支持这条建议 [1]，畸形链接对应的内容不应发布 [2]。"
                "这段正文长度足够用于验证最终发布前的 URL 完整性过滤，"
                "并确保一条坏链接不会让已有可靠内容整体失败。"
            ),
            evidence=[
                {
                    "source_id": "valid",
                    "source_title": "有效来源",
                    "source_url": "https://example.com/source",
                },
                {
                    "source_id": "broken",
                    "source_title": "截断来源",
                    "source_url": "https://example.com/%E5%85%B3%E",
                },
            ],
            language="zh-CN",
        )
        self.assertTrue(ok)
        self.assertEqual(cited, ["valid"])
        self.assertIn("https://example.com/source", rendered)
        self.assertNotIn("%E5%85%B3%E", rendered)
        self.assertNotIn("[2]", rendered)


class ResearchDeliveryStatusTests(unittest.TestCase):
    def test_partial_source_backed_answer_is_a_successful_delivery(self):
        from app.services.research.deep_research_runner import (
            _research_delivery_status,
        )

        self.assertEqual(
            _research_delivery_status(
                content="有来源支持的部分回答，并诚实说明剩余限制。",
                evidence_count=2,
            ),
            "completed",
        )
        self.assertEqual(
            _research_delivery_status(content="", evidence_count=2),
            "failed",
        )


class ReportFailureClassificationTests(unittest.TestCase):
    def test_classification(self):
        from app.services.research.publication.report_writer import (
            classify_report_failure,
        )

        def attempt(error="", body=0, ok=False):
            return {"ok": ok, "error": error, "body_chars": body}

        self.assertEqual(classify_report_failure([]), "no_model")
        self.assertEqual(
            classify_report_failure([attempt(ok=True)]),
            "success",
        )
        self.assertEqual(
            classify_report_failure(
                [attempt(error="Rate limit exceeded free-models-per-day")]
            ),
            "rate_limited",
        )
        self.assertEqual(
            classify_report_failure([attempt(error="request timed out")]),
            "timeout",
        )
        self.assertEqual(
            classify_report_failure(
                [attempt(error="enable_thinking parameter restricted")]
            ),
            "model_config",
        )
        self.assertEqual(
            classify_report_failure([attempt(body=0)]),
            "empty_output",
        )
        self.assertEqual(
            classify_report_failure([attempt(body=120)]),
            "gate_rejected",
        )


class MessageTextExtractionTests(unittest.TestCase):
    """The final answer must be taken directly when present.

    DashScope thinking models return the deliverable as a bare string at the
    end of content, with reasoning in thinking blocks; the parser must prefer
    it instead of falling back to the thinking dump.
    """

    def _message(self, content):
        from langchain_core.messages import AIMessage

        return AIMessage(content=content)

    def test_bare_string_final_answer_wins_over_thinking(self):
        from app.services.research.publication.report_writer import (
            _message_text,
        )

        message = self._message(
            [
                "",
                {"type": "thinking", "thinking": "planning the report..."},
                {"type": "thinking", "thinking": "checking constraints..."},
                "2024\u5e74\u673a\u5668\u5b66\u4e60\u5165\u95e8\u4e66\u7c4d\u63a8\u8350\u7814\u7a76\u62a5\u544a\n\n\u7ed3\u8bba\n\u63a8\u8350\u4e66\u7c4d\u8986\u76d6\u7ecf\u5178\u4e0e\u5b9e\u6218\u3002",
            ]
        )
        text = _message_text(message)
        self.assertIn("2024\u5e74\u673a\u5668\u5b66\u4e60\u5165\u95e8\u4e66\u7c4d\u63a8\u8350\u7814\u7a76\u62a5\u544a", text)
        self.assertNotIn("planning the report", text)
        self.assertNotIn("checking constraints", text)

    def test_text_block_preferred_over_thinking(self):
        from app.services.research.publication.report_writer import (
            _message_text,
        )

        message = self._message(
            [
                {"type": "thinking", "thinking": "planning..."},
                {"type": "text", "text": "\u6700\u7ec8\u7b54\u6848"},
            ]
        )
        self.assertEqual(_message_text(message), "\u6700\u7ec8\u7b54\u6848")

    def test_thinking_fallback_when_no_final_text(self):
        from app.services.research.publication.report_writer import (
            _message_text,
        )

        message = self._message(
            [
                {"type": "thinking", "thinking": "\u601d\u8003\u5185\u5bb9"},
                {"type": "thinking", "thinking": "\u66f4\u591a\u601d\u8003"},
            ]
        )
        self.assertEqual(_message_text(message), "\u601d\u8003\u5185\u5bb9\u66f4\u591a\u601d\u8003")

    def test_final_deliverable_is_not_extracted(self):
        from app.services.research.publication.report_writer import (
            _render_final_deliverable,
        )

        body = (
            "为什么最终这么推荐：推荐《机器学习》与《动手学深度学习》[1][2]。\n\n"
            "1. 理由\n证据支撑内容：该书以代码驱动，适合编程背景读者快速建立直觉。\n\n"
            "2. 取舍\n与替代方案对比：相比纯理论教材，它牺牲部分数学推导但降低入门门槛。\n\n"
            "3. 边界\n适用条件说明：需要读者具备基础 Python 能力，适合实战派初学者。"
        )
        rendered, _cited, ok = _render_final_deliverable(
            body,
            evidence=[{"source_id": "s1"}],
            language="zh-CN",
        )
        self.assertTrue(ok)
        self.assertIn("为什么最终这么推荐", rendered)
        self.assertIn("3. 边界", rendered)

    def test_plain_string_content(self):
        from app.services.research.publication.report_writer import (
            _message_text,
        )

        self.assertEqual(_message_text("\u76f4\u63a5\u6587\u672c"), "\u76f4\u63a5\u6587\u672c")


if __name__ == "__main__":
    unittest.main()
