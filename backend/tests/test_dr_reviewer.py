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
        self.assertEqual(review.stop_reason, "rule_evidence_satisfied")

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
        self.assertEqual(review.verdict, "insufficient")
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
        self.assertEqual(task.query, "《机器学习》难度分级 初学者")
        self.assertEqual(task.include_url_prefixes, [])

    def test_meta_review_gap_is_compiled_into_topic_search_terms(self):
        task = self._plan(
            objective="有什么深度学习书籍推荐",
            round_index=2,
            budget=BUDGET,
            previous_assessment=None,
            pending_subquestions=["已核验的推荐候选数量不足，需要继续验证同类图书。"],
            used_queries=set(),
        )
        self.assertIsNotNone(task)
        self.assertIn("深度学习", task.query)
        self.assertIn("经典教材", task.query)
        self.assertIn("PyTorch", task.query)
        self.assertNotIn("数量不足", task.query)

    def test_catalog_batch_verifies_up_to_eight_discovered_candidates(self):
        titles = {f"候选书 {index}": f"候选书 {index} 作者 {index}" for index in range(8)}
        task = self._plan(
            objective="推荐机器学习入门书",
            round_index=2,
            budget=BUDGET,
            previous_assessment=None,
            pending_subquestions=[],
            used_queries=set(),
            candidate_hints=titles,
            prefer_catalog_batch=True,
        )
        self.assertIsNotNone(task)
        self.assertTrue(task.metadata["candidate_catalog_batch"])
        self.assertEqual(len(task.metadata["candidate_titles"]), 8)

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
    def test_book_report_structure_accepts_one_table_and_one_route(self):
        from app.services.research.publication.report_writer import (
            _report_structure_contract,
        )

        body = (
            "如果目标是先建立代码直觉，我会先选《Python深度学习》；"
            "如果更看重逐步推导，则先选《动手学深度学习》。\n\n"
            "## 对比与取舍\n\n"
            "| 书名 | 在方案中的角色 | 适合的目标 | 核心取舍 | 依据 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 《Python深度学习》 | 建立代码直觉 | 快速理解训练流程 | 理论展开较少 | [1] |\n"
            "| 《动手学深度学习》 | 补齐实现细节 | 边学边练 | 内容覆盖较宽 | [2] |\n\n"
            + "选择时应围绕已有基础、希望优先获得的能力以及可以接受的理论密度来判断。"
            * 16
            + "\n\n## 阅读顺序\n\n"
            "1. 用《Python深度学习》建立模型训练的整体认识。\n"
            "2. 能独立解释训练流程后，用《动手学深度学习》补充实现与练习。"
        )

        ok, details = _report_structure_contract(
            body,
            objective="推荐深度学习书籍并安排阅读顺序",
            evidence=[],
        )

        self.assertTrue(ok, details)

    def test_book_report_structure_rejects_duplicated_prose_block(self):
        from app.services.research.publication.report_writer import (
            _report_structure_contract,
        )

        repeated = (
            "《Python深度学习》适合先建立代码直觉，但理论推导较少；"
            "选择时要结合已有编程基础，并用原始资料核对版本差异与练习范围。"
        )
        body = (
            "先按目标选择，不需要把所有候选都从头读完。\n\n"
            "## 对比与取舍\n\n"
            "| 书名 | 角色 | 取舍 | 依据 |\n"
            "| --- | --- | --- | --- |\n"
            "| 《Python深度学习》 | 入门 | 理论较少 | [1] |\n\n"
            f"{repeated}\n\n{repeated}\n\n"
            "## 怎么选\n\n先确认目标，再根据证据选择。"
        )

        ok, details = _report_structure_contract(
            body,
            objective="推荐深度学习书籍",
            evidence=[],
        )

        self.assertFalse(ok)
        self.assertIn("candidate_descriptions_repeated", details["reasons"])
        self.assertTrue(details["repeated_content_blocks"])

    def test_book_report_structure_rejects_heading_noise_and_precision(self):
        from app.services.research.publication.report_writer import (
            _report_structure_contract,
        )

        body = (
            "# 深度学习路线 📚\n\n"
            "## 先说结论\n《Python深度学习》是必读。\n\n---\n\n"
            "### 《Python深度学习》\n评分 9.4 分，建议学习 4-6 周。\n\n"
            "## 对比\n| 书名 | 结论 |\n| --- | --- |\n"
            "| 《Python深度学习》 | 首选 [1] |\n\n"
            "## 阅读路线\n再次阅读《Python深度学习》，最后复习《Python深度学习》。"
            + "补充说明。" * 120
        )

        ok, details = _report_structure_contract(
            body,
            objective="推荐深度学习书籍并安排阅读顺序",
            evidence=[],
        )

        self.assertFalse(ok)
        self.assertIn("unsupported_precision_or_consensus", details["reasons"])

    def test_verified_book_may_be_repeated_after_one_bound_citation(self):
        from app.services.research.publication.report_writer import (
            _book_candidate_contract_satisfied,
        )

        evidence = [
            {
                "source_id": "s1",
                "source_title": "深度学习 (豆瓣)",
                "source_url": "https://book.douban.com/subject/27087503/",
                "claim": "《深度学习》系统介绍神经网络与深度学习理论。",
            }
        ]
        body = (
            "建议把《深度学习》放在路线后半段。\n\n"
            "| 书名 | 定位 |\n| --- | --- |\n"
            "| 《深度学习》 | 系统理解神经网络理论 [1] |"
        )

        self.assertTrue(
            _book_candidate_contract_satisfied(
                body,
                objective="推荐深度学习书籍并安排阅读顺序",
                evidence=evidence,
            )
        )

    def test_unverified_book_still_fails_candidate_contract(self):
        from app.services.research.publication.report_writer import (
            _book_candidate_contract_satisfied,
        )

        evidence = [
            {
                "source_id": "s1",
                "source_title": "深度学习 (豆瓣)",
                "source_url": "https://book.douban.com/subject/27087503/",
                "claim": "《深度学习》系统介绍神经网络与深度学习理论。",
            }
        ]

        self.assertFalse(
            _book_candidate_contract_satisfied(
                "推荐《并不存在的深度学习书》作为第一本 [1]。",
                objective="推荐深度学习书籍并安排阅读顺序",
                evidence=evidence,
            )
        )

    def test_catalog_claim_title_alias_is_accepted(self):
        from app.services.research.publication.report_writer import (
            _book_candidate_contract_satisfied,
        )

        evidence = [
            {
                "source_id": "s1",
                "source_title": "Deep Learning with Python (豆瓣)",
                "source_url": "https://book.douban.com/subject/30293801/",
                "claim": "《Deep Learning with Python》中文译名为《Python深度学习》。",
            }
        ]

        self.assertTrue(
            _book_candidate_contract_satisfied(
                "建议先读《Python深度学习》，建立代码直觉 [1]。",
                objective="推荐深度学习书籍并安排阅读顺序",
                evidence=evidence,
            )
        )

    def test_unique_edition_variant_is_rewritten_to_catalog_title(self):
        from app.services.research.publication.report_writer import (
            _book_candidate_contract_satisfied,
            _canonicalize_verified_book_titles,
        )

        evidence = [
            {
                "source_id": "s1",
                "source_title": "Deep Learning with Python, Third Edition (豆瓣)",
                "source_url": "https://book.douban.com/subject/37210135/",
                "claim": "《Deep Learning with Python, Third Edition》以 Python 代码讲解深度学习。",
            }
        ]
        body, rewrites = _canonicalize_verified_book_titles(
            "先读《Deep Learning with Python, 3rd Ed.》建立实践直觉 [1]。",
            objective="推荐深度学习书籍并安排阅读顺序",
            evidence=evidence,
        )

        self.assertIn("《Deep Learning with Python, Third Edition》", body)
        self.assertEqual(len(rewrites), 1)
        self.assertTrue(
            _book_candidate_contract_satisfied(
                body,
                objective="推荐深度学习书籍并安排阅读顺序",
                evidence=evidence,
            )
        )

    def test_missing_verified_book_is_appended_with_its_own_citation(self):
        from app.services.research.publication.report_writer import (
            _ensure_verified_candidate_coverage,
        )

        evidence = [
            {
                "source_id": "s1",
                "source_title": "深度学习 (豆瓣)",
                "source_url": "https://book.douban.com/subject/27087503/",
                "claim": "《深度学习》系统介绍神经网络与深度学习理论。",
            },
            {
                "source_id": "s2",
                "source_title": "动手学深度学习 (豆瓣)",
                "source_url": "https://book.douban.com/subject/34991536/",
                "claim": "《动手学深度学习》结合 PyTorch 代码与神经网络实践。",
            },
        ]
        body = "首选 **《深度学习》**，适合系统理解理论基础。 [1]"

        rendered = _ensure_verified_candidate_coverage(
            body,
            objective="有什么深度学习书籍推荐",
            evidence=evidence,
        )

        self.assertEqual(rendered.count("《深度学习》"), 1)
        self.assertIn("《动手学深度学习》", rendered)
        self.assertIn("[2]", rendered)

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
        self.assertEqual(rendered.count("## 参考来源"), 1)
        self.assertNotIn("## 研究结论", rendered)

    def test_recommendation_evidence_column_is_normalized(self):
        from app.services.research.publication.report_writer import (
            _normalize_recommendation_evidence_column,
        )

        body = (
            "## 对比与取舍\n\n"
            "| 书名 | 核心取舍 | 依据 |\n"
            "| --- | --- | --- |\n"
            "| 《小狗钱钱》 | 偏故事化 [2][3] | |\n\n"
            "## 怎么选\n\n按目标选择。"
        )
        normalized = _normalize_recommendation_evidence_column(body)

        self.assertIn("| 《小狗钱钱》 | 偏故事化 | [2] [3] |", normalized)

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


class ReportWriterSafetyTests(unittest.TestCase):
    class _SequenceModel:
        def __init__(self, responses):
            self.responses = list(responses)
            self.prompts = []

        async def ainvoke(self, prompt):
            self.prompts.append(prompt)
            index = min(len(self.prompts) - 1, len(self.responses) - 1)
            return type("Response", (), {"content": self.responses[index]})()

    class _TimeoutModel:
        async def ainvoke(self, _prompt):
            raise TimeoutError()

    class _PartialStreamingModel:
        async def astream(self, _prompt):
            from langchain_core.messages import AIMessageChunk

            yield AIMessageChunk(content="# 已完成部分\n\n这是超时前已经收到的可靠正文 [1]。")
            raise TimeoutError()

    class _BothInvocationModesModel:
        def __init__(self):
            self.ainvoke_called = False
            self.astream_called = False

        async def ainvoke(self, _prompt):
            self.ainvoke_called = True
            return type("Response", (), {"content": "最终答案"})()

        async def astream(self, _prompt):
            self.astream_called = True
            yield type("Chunk", (), {"content": "不应使用"})()

    def _report(self, *, objective="推荐一些 AI 入门书"):
        from app.services.research.report import (
            ResearchReport,
            ResearchReportSource,
        )
        from app.services.research.verifier import (
            ClaimAdmissionDecision,
            VerifierAdmissionResult,
        )

        run_id = uuid.uuid4()
        evidence_id = uuid.uuid4()
        claim = "《人工智能：一种现代方法》被该来源列入人工智能入门推荐书单。"
        decision = ClaimAdmissionDecision(
            claim=claim,
            status="admitted",
            evidence_ids=[evidence_id],
            quality="medium",
            reason_codes=["supported_by_evidence"],
            provenance_valid=True,
            content_quality=1.0,
            query_relevance=1.0,
            corroborated=True,
            publishable=True,
        )
        verification = VerifierAdmissionResult(
            run_id=run_id,
            decisions=[decision],
            admitted_claims=[decision],
            ready_for_final_answer=True,
        )
        return ResearchReport(
            run_id=run_id,
            user_id=uuid.uuid4(),
            objective=objective,
            run_status="active",
            report_status="verified",
            verified_claims=[decision],
            sources=[
                ResearchReportSource(
                    evidence_id=evidence_id,
                    source_title="人工智能：一种现代方法 (豆瓣)",
                    source_url="https://book.douban.com/subject/1/",
                    quality="medium",
                    relevance=5,
                    research_round=1,
                    claim=claim,
                )
            ],
            verification=verification,
        )

    def _write(self, responses, *, objective="推荐一些 AI 入门书"):
        from app.services.research.publication.report_writer import (
            write_research_report,
        )

        model = self._SequenceModel(responses)
        with mock.patch(
            "app.infra.llm.get_llm",
            return_value=model,
        ), mock.patch(
            "app.services.research.publication.report_writer.report_completed_step",
            new=mock.AsyncMock(),
        ), mock.patch(
            "app.services.research.publication.report_writer.report_model_completion",
            new=mock.AsyncMock(),
        ), mock.patch(
            "app.services.research.publication.report_writer.record_model_failure",
        ), mock.patch(
            "app.services.research.publication.report_writer.record_model_success",
        ):
            result = asyncio.run(
                write_research_report(
                    self._report(objective=objective),
                    model_id="writer-model",
                )
            )
        return result, model

    def test_plain_string_internal_reasoning_is_not_published(self):
        leaked = (
            "我需要先理解用户需求：用户想要人工智能入门书籍。\n"
            "接下来我将分析证据、规划结构并检查约束。材料 [1] 可以使用，"
            "但我还需要决定先写哪些内容以及如何组织最终回复。"
        )
        result, model = self._write([leaked, leaked])

        self.assertEqual(len(model.prompts), 2)
        self.assertEqual(result.status, "fallback")
        self.assertEqual(result.provider, "deterministic")
        self.assertNotIn("我需要先理解用户需求", result.report_markdown)
        self.assertTrue(
            all(
                item["error"] == "internal_reasoning_detected"
                for item in result.metadata["attempts"]
            )
        )

    def test_streaming_timeout_preserves_received_report_text(self):
        from app.services.research.publication.report_writer import (
            _invoke_report_model,
            _message_text,
        )

        response, timed_out = asyncio.run(
            _invoke_report_model(
                self._PartialStreamingModel(),
                "prompt",
                timeout_seconds=1,
            )
        )

        self.assertTrue(timed_out)
        self.assertIn("超时前已经收到", _message_text(response))

    def test_publication_prefers_bounded_non_stream_invocation(self):
        from app.services.research.publication.report_writer import (
            _invoke_report_model,
            _message_text,
        )

        model = self._BothInvocationModesModel()
        response, timed_out = asyncio.run(
            _invoke_report_model(model, "prompt", timeout_seconds=1)
        )

        self.assertFalse(timed_out)
        self.assertTrue(model.ainvoke_called)
        self.assertFalse(model.astream_called)
        self.assertEqual(_message_text(response), "最终答案")

    def test_timeout_is_not_misreported_as_missing_markdown(self):
        from app.services.research.publication.report_writer import (
            write_research_report,
        )

        with mock.patch(
            "app.infra.llm.get_llm",
            return_value=self._TimeoutModel(),
        ), mock.patch(
            "app.services.research.publication.report_writer.report_completed_step",
            new=mock.AsyncMock(),
        ), mock.patch(
            "app.services.research.publication.report_writer.report_model_completion",
            new=mock.AsyncMock(),
        ), mock.patch(
            "app.services.research.publication.report_writer.record_model_failure",
        ):
            result = asyncio.run(
                write_research_report(
                    self._report(),
                    model_id="writer-model",
                )
            )

        self.assertEqual(result.status, "fallback")
        self.assertEqual(result.error, "TimeoutError")
        self.assertEqual(result.metadata["failure_cause"], "timeout")

    def test_thinking_only_response_uses_deterministic_fallback(self):
        thinking_only = [
            {"type": "thinking", "thinking": "planning the report..."},
            {"type": "thinking", "thinking": "checking constraints... [1]"},
        ]
        result, model = self._write([thinking_only, thinking_only])

        self.assertEqual(len(model.prompts), 2)
        self.assertEqual(result.status, "fallback")
        self.assertEqual(result.provider, "deterministic")
        self.assertNotIn("planning the report", result.report_markdown)
        self.assertTrue(
            all(
                item["error"] == "thinking_only_response"
                for item in result.metadata["attempts"]
            )
        )

    def test_render_gate_failure_cannot_be_reported_as_synthesized(self):
        uncited = (
            "这是模型生成但没有任何正文引用的草稿。"
            "它的长度足以通过旧版最低字符门槛，却无法证明其中的推荐来自哪条材料。"
            "发布器必须拒绝它，而不能仅仅因为清洗后的正文非空就标记为成功。"
        )
        result, model = self._write([uncited, uncited])

        self.assertEqual(len(model.prompts), 2)
        self.assertEqual(result.status, "fallback")
        self.assertNotEqual(result.status, "synthesized")
        self.assertNotIn("这是模型生成但没有任何正文引用的草稿", result.report_markdown)
        self.assertTrue(
            all(not item["ok"] for item in result.metadata["attempts"])
        )

    def test_explicit_table_contract_keeps_safe_model_answer_after_repair(self):
        no_table = (
            "我建议先从一本覆盖基础概念的入门书开始，再根据学习目标补充专题读物。"
            "现有资料明确把《人工智能：一种现代方法》列入人工智能入门推荐书单 [1]。"
            "这段正文有有效引用且长度充足，但没有按用户明确要求提供表格。"
        )
        result, model = self._write(
            [no_table, no_table],
            objective="请用表格推荐一些 AI 入门书，并列出适合人群和取舍",
        )

        from app.services.publication_safety import markdown_table_present

        self.assertEqual(len(model.prompts), 2)
        self.assertIn("previous draft was rejected", model.prompts[1])
        self.assertEqual(result.status, "synthesized")
        self.assertEqual(result.provider, "runtime_llm")
        self.assertFalse(markdown_table_present(result.report_markdown))
        self.assertIn("这段正文有有效引用", result.report_markdown)
        self.assertTrue(result.metadata["publication_safe"])
        self.assertFalse(result.metadata["answer_quality_pass"])
        self.assertTrue(
            result.metadata["attempts"][-1]["accepted_with_quality_warning"]
        )
        self.assertEqual(
            result.metadata["published_verified_candidate_count"],
            1,
        )
        self.assertEqual(
            result.metadata["published_verified_candidate_titles"],
            ["人工智能：一种现代方法"],
        )

    def test_book_table_fallback_never_projects_editorial_title_as_book(self):
        from app.services.research.publication.report_writer import (
            _fallback_markdown,
        )
        from app.services.research.report import (
            ResearchReport,
            ResearchReportSource,
        )
        from app.services.research.verifier import (
            ClaimAdmissionDecision,
            VerifierAdmissionResult,
        )

        run_id = uuid.uuid4()
        article_id = uuid.uuid4()
        catalog_id = uuid.uuid4()
        article_title = "人工智能入门书籍推荐零基础新手篇 - 博学谷"
        article_claim = "文章把《深度学习》列为机器学习与神经网络入门读物。"
        catalog_claim = "《深度学习》系统介绍机器学习、神经网络和深度学习基础。"
        decisions = [
            ClaimAdmissionDecision(
                claim=article_claim,
                status="admitted",
                evidence_ids=[article_id],
                quality="medium",
                reason_codes=["supported_by_evidence"],
                provenance_valid=True,
                content_quality=1.0,
                query_relevance=1.0,
                corroborated=False,
                publishable=True,
            ),
            ClaimAdmissionDecision(
                claim=catalog_claim,
                status="admitted",
                evidence_ids=[catalog_id],
                quality="high",
                reason_codes=["supported_by_evidence"],
                provenance_valid=True,
                content_quality=1.0,
                query_relevance=1.0,
                corroborated=True,
                publishable=True,
            ),
        ]
        verification = VerifierAdmissionResult(
            run_id=run_id,
            decisions=decisions,
            admitted_claims=decisions,
            ready_for_final_answer=True,
        )
        report = ResearchReport(
            run_id=run_id,
            user_id=uuid.uuid4(),
            objective=(
                "推荐人工智能入门书，请用 Markdown 表格，列为："
                "书名、作者、适合人群、推荐理由、局限"
            ),
            run_status="active",
            report_status="verified",
            verified_claims=decisions,
            sources=[
                ResearchReportSource(
                    evidence_id=article_id,
                    source_title=article_title,
                    source_url="https://example.com/ai-book-list",
                    quality="medium",
                    relevance=5,
                    research_round=1,
                    claim=article_claim,
                ),
                ResearchReportSource(
                    evidence_id=catalog_id,
                    source_title="深度学习 (豆瓣)",
                    source_url="https://book.douban.com/subject/27087503/",
                    quality="high",
                    relevance=5,
                    research_round=2,
                    claim=catalog_claim,
                ),
            ],
            verification=verification,
        )

        rendered = _fallback_markdown(report, language="zh-CN")

        self.assertIn(
            "| 书名 | 作者 | 适合人群 | 推荐理由 | 局限 |",
            rendered,
        )
        self.assertIn(
            "[《深度学习》](https://book.douban.com/subject/27087503/)",
            rendered,
        )
        self.assertNotIn(article_title, rendered)
        self.assertIn("系统介绍机器学习", rendered)
        self.assertNotIn("列为机器学习与神经网络入门读物", rendered)

    def test_book_table_fallback_reports_insufficient_without_catalog_entity(self):
        report = self._report(
            objective=(
                "推荐人工智能入门书，请用 Markdown 表格，列为："
                "书名、作者、适合人群、推荐理由、局限"
            )
        )
        report.sources[0].source_title = "人工智能入门书单推荐"
        report.sources[0].source_url = "https://example.com/ai-reading-list"

        from app.services.research.publication.report_writer import (
            _fallback_markdown,
        )

        rendered = _fallback_markdown(report, language="zh-CN")

        self.assertIn("暂无可核验书目", rendered)
        self.assertNotIn("人工智能入门书单推荐", rendered)

    def test_bibliographic_metadata_is_not_used_as_recommendation_reason(self):
        from app.services.research.publication.report_writer import (
            _candidate_reason,
        )

        reason = _candidate_reason(
            {"candidate_title": "零基础学机器学习"},
            claim=(
                "《零基础学机器学习》，作者为黄佳，"
                "由人民邮电出版社出版，出版时间为2020年。"
            ),
            zh=True,
        )

        self.assertIn("入门定位", reason)
        self.assertNotIn("出版社", reason)


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
