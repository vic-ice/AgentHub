from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from app.services.evidence_strategy import EvidenceFacet, EvidenceStrategy
from app.services.external_capabilities.contracts import BookSearchInput
from app.services.research.deep_research_runner import _catalog_companion_tasks
from app.services.research.loop.contracts import ResearchLoopBudget
from app.services.research.publication.packet import (
    build_research_publication_packet,
)
from app.services.research.publication.report_writer import (
    _book_candidate_contract_result,
    _candidate_bound_publication_evidence,
    _canonicalize_verified_book_titles,
    _generate_report_once,
    _verified_book_candidate_rows,
    build_research_presentation_contract,
)
from app.services.research.publication.semantic_quality import (
    evaluate_semantic_answer,
)
from app.services.user_answer_contracts import build_user_answer_brief


class AdaptiveEvidenceStrategyTests(unittest.TestCase):
    def test_strategy_is_open_ended_not_a_genre_enum(self) -> None:
        technical = EvidenceStrategy(
            facets=[
                EvidenceFacet(
                    name="可运行实践资源",
                    query_terms=["repository", "notebook"],
                    preferred_source_types=["official repository", "maintained tutorial"],
                    importance="high",
                    required_for_recommendation=True,
                )
            ]
        )
        reflective = EvidenceStrategy(
            facets=[
                EvidenceFacet(
                    name="读者长期实践反馈",
                    query_terms=["长期实践", "负面评价"],
                    preferred_source_types=["reader review", "critical review"],
                    importance="high",
                    required_for_recommendation=True,
                )
            ]
        )

        self.assertNotEqual(
            technical.facets[0].name,
            reflective.facets[0].name,
        )
        self.assertIn("official repository", technical.facets[0].preferred_source_types)
        self.assertIn("critical review", reflective.facets[0].preferred_source_types)

    def test_model_shape_aliases_are_normalized_without_domain_inference(self) -> None:
        strategy = EvidenceStrategy.model_validate(
            {
                "evidence_facets": [
                    {
                        "facet": "配套实现",
                        "description": "判断能否直接动手",
                        "keywords": ["notebook"],
                        "source_types": ["repository"],
                        "importance": "critical",
                        "candidate_level": True,
                        "required": True,
                        "model_specific_note": "ignored",
                    }
                ],
                "questions": ["哪个候选更容易形成作品"],
            }
        )

        self.assertEqual(strategy.facets[0].name, "配套实现")
        self.assertEqual(strategy.facets[0].query_terms, ["notebook"])
        self.assertEqual(
            strategy.facets[0].preferred_source_types,
            ["repository"],
        )
        self.assertEqual(strategy.facets[0].importance, "high")
        self.assertEqual(
            strategy.comparison_questions,
            ["哪个候选更容易形成作品"],
        )

    def test_book_search_accepts_provider_serialized_nested_strategy(self) -> None:
        request = BookSearchInput(
            query="推荐技术书",
            evidence_strategy=json.dumps(
                {
                    "facets": [
                        {
                            "name": "配套代码",
                            "purpose": "验证能否实践",
                            "query_terms": ["repository"],
                            "preferred_source_types": ["official repository"],
                            "importance": "high",
                            "candidate_specific": True,
                            "required_for_recommendation": True,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        )

        self.assertEqual(request.evidence_strategy.facets[0].name, "配套代码")

    def test_deep_research_executes_model_facets_as_parallel_tasks(self) -> None:
        strategy = EvidenceStrategy(
            facets=[
                EvidenceFacet(
                    name="配套实践",
                    purpose="判断是否能从阅读过渡到动手",
                    query_terms=["code", "exercise"],
                    preferred_source_types=["repository", "official docs"],
                    importance="high",
                    candidate_specific=True,
                ),
                EvidenceFacet(
                    name="知识时效",
                    purpose="判断内容是否仍适用于当前工具链",
                    query_terms=["current edition"],
                    preferred_source_types=["maintainer documentation"],
                    importance="high",
                    candidate_specific=False,
                ),
            ]
        )
        tasks = _catalog_companion_tasks(
            objective="推荐深度学习实战书",
            round_index=2,
            budget=ResearchLoopBudget(max_search_rounds=4),
            evidence_strategy=strategy,
            candidate_titles=["候选甲", "候选乙"],
            used_queries=set(),
        )

        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].metadata["evidence_facet"], "配套实践")
        self.assertEqual(
            tasks[0].metadata["evidence_candidate_titles"],
            ["候选甲", "候选乙"],
        )
        self.assertNotIn("candidate_titles", tasks[0].metadata)
        self.assertIn("repository", tasks[0].query)
        self.assertEqual(tasks[1].metadata["evidence_facet"], "知识时效")
        self.assertEqual(tasks[1].metadata["evidence_candidate_titles"], [])

    def test_evidence_strategy_compiles_to_short_fair_parallel_queries(self) -> None:
        strategy = EvidenceStrategy(
            facets=[
                EvidenceFacet(
                    name="代码可运行性",
                    purpose="判断代码在当前环境能否运行以及仓库是否持续维护",
                    query_terms=["repository", "code examples", "issues", "Python 3.12"],
                    preferred_source_types=["official GitHub repository", "issue tracker"],
                    importance="high",
                    candidate_specific=True,
                ),
                EvidenceFacet(
                    name="内容定位",
                    purpose="比较每本书覆盖的学习阶段",
                    query_terms=["table of contents", "syllabus"],
                    preferred_source_types=["publisher page"],
                    importance="high",
                    candidate_specific=True,
                ),
                EvidenceFacet(
                    name="读者反馈",
                    purpose="比较真实读者的难度与实用性反馈",
                    query_terms=["reader review", "difficulty"],
                    preferred_source_types=["community review"],
                    importance="medium",
                    candidate_specific=True,
                ),
            ]
        )
        titles = [f"候选{i}" for i in range(1, 11)]

        tasks = _catalog_companion_tasks(
            objective="为有 Python 基础的读者推荐机器学习项目书",
            round_index=2,
            budget=ResearchLoopBudget(max_search_rounds=4),
            evidence_strategy=strategy,
            candidate_titles=titles,
            used_queries=set(),
        )

        self.assertEqual(len(tasks), 4)
        self.assertEqual(
            {task.metadata["evidence_facet"] for task in tasks},
            {"代码可运行性", "内容定位", "读者反馈"},
        )
        self.assertTrue(all(len(task.query) <= 180 for task in tasks))
        self.assertTrue(
            all(
                len(task.metadata["evidence_candidate_titles"]) <= 2
                for task in tasks
            )
        )
        self.assertTrue(all("判断代码在当前环境" not in task.query for task in tasks))

    def test_publication_packet_preserves_candidate_facet_coverage(self) -> None:
        strategy = EvidenceStrategy(
            facets=[
                EvidenceFacet(
                    name="实战资源",
                    purpose="确认配套代码是否可用",
                    importance="high",
                    required_for_recommendation=True,
                ),
                EvidenceFacet(
                    name="局限与争议",
                    purpose="识别不适用条件",
                    importance="high",
                    required_for_recommendation=True,
                ),
            ]
        )
        packet = build_research_publication_packet(
            brief=build_user_answer_brief(
                "推荐两本可动手实践的机器学习书",
                task_type="book_recommendation",
                answer_depth="deep",
            ),
            evidence_strategy=strategy,
            allowed_recommendation_entities=["候选甲", "候选乙"],
            evidence=[
                {
                    "claim": "候选甲提供公开代码仓库",
                    "source_title": "候选甲 repository",
                    "source_url": "https://example.com/a",
                    "candidate_title": "候选甲",
                    "evidence_facet": "实战资源",
                    "source_roles": ["repository"],
                    "quality": "high",
                },
                {
                    "claim": "候选甲的部分依赖版本较旧",
                    "source_title": "候选甲 issue",
                    "source_url": "https://example.com/b",
                    "candidate_title": "候选甲",
                    "evidence_facet": "局限与争议",
                    "source_roles": ["issue tracker"],
                    "quality": "medium",
                },
                {
                    "claim": "课程比较指出两本书的覆盖重点不同",
                    "source_title": "课程书单",
                    "source_url": "https://example.com/c",
                    "evidence_facet": "实战资源",
                    "quality": "medium",
                },
            ],
        )

        first, second = packet.candidate_dossiers
        self.assertEqual(first.all_citation_ids, [1, 2])
        self.assertEqual(first.uncovered_required_facets, [])
        self.assertEqual(
            second.uncovered_required_facets,
            ["实战资源", "局限与争议"],
        )
        self.assertEqual(packet.cross_cutting_facets[0].citation_ids, [3])
        self.assertEqual(packet.evidence_index[0].source_roles, ["repository"])

    def test_packet_separates_model_portfolio_from_external_coverage(self) -> None:
        packet = build_research_publication_packet(
            brief=build_user_answer_brief(
                "推荐三本机器学习书",
                task_type="book_recommendation",
                answer_depth="deep",
            ),
            evidence=[],
            allowed_recommendation_entities=["候选甲", "候选乙", "候选丙"],
            externally_verified_entities=["候选甲"],
            target_candidate_count=3,
            candidate_portfolio=[
                {
                    "title": "候选甲",
                    "portfolio_role": "理论主线",
                    "rationale": "建立统一概念框架",
                },
                {
                    "title": "候选乙",
                    "portfolio_role": "项目实践",
                    "rationale": "把知识转化为代码",
                },
                {
                    "title": "候选丙",
                    "portfolio_role": "查漏补缺",
                    "rationale": "提供另一种解释路径",
                },
            ],
        )

        self.assertEqual(packet.target_candidate_count, 3)
        self.assertEqual(
            [item.external_evidence_status for item in packet.candidate_dossiers],
            ["verified", "not_found", "not_found"],
        )
        self.assertEqual(packet.candidate_dossiers[1].portfolio_role, "项目实践")

    def test_model_planned_candidates_are_not_deleted_by_sparse_web_evidence(self) -> None:
        allowed = ["候选甲", "候选乙", "候选丙"]
        ok, details = _book_candidate_contract_result(
            "推荐《候选甲》《候选乙》和《候选丙》，三者承担不同学习角色。",
            objective="推荐三本技术书",
            evidence=[],
            allowed_candidate_titles=allowed,
            target_candidate_count=3,
        )
        self.assertTrue(ok)
        self.assertEqual(details["missing_candidate_count"], 0)

        too_narrow, narrow_details = _book_candidate_contract_result(
            "只推荐《候选甲》。",
            objective="推荐三本技术书",
            evidence=[],
            allowed_candidate_titles=allowed,
            target_candidate_count=3,
        )
        self.assertFalse(too_narrow)
        self.assertEqual(narrow_details["missing_candidate_count"], 2)

        invented, invented_details = _book_candidate_contract_result(
            "推荐《候选甲》《候选乙》和《陌生书名》。",
            objective="推荐三本技术书",
            evidence=[],
            allowed_candidate_titles=allowed,
            target_candidate_count=3,
        )
        self.assertTrue(invented)
        self.assertEqual(invented_details["unsupported_titles"], ["陌生书名"])
        self.assertFalse(invented_details["portfolio_alignment_pass"])

        open_world, open_details = _book_candidate_contract_result(
            "推荐《模型已知甲》《模型已知乙》和《模型已知丙》。",
            objective="推荐三本技术书",
            evidence=[],
            allowed_candidate_titles=[],
            target_candidate_count=3,
        )
        self.assertTrue(open_world)
        self.assertEqual(open_details["unsupported_titles"], [])

    def test_unambiguous_candidate_short_titles_are_canonicalized_without_opening_allowlist(self) -> None:
        allowed = [
            "Hands-On Machine Learning with Scikit-Learn, Keras, and TensorFlow",
            "机器学习（周志华）",
            "统计学习方法（李航）",
            "Approaching (Almost) Any Machine Learning Problem",
        ]
        body, rewrites = _canonicalize_verified_book_titles(
            (
                "推荐《Hands-On ML》《机器学习》《统计学习方法》和"
                "《Approaching (Almost) Any ML Problem》，"
                "不推荐未经规划的《陌生书名》。"
            ),
            objective="推荐至少四本机器学习书",
            evidence=[],
            allowed_candidate_titles=allowed,
        )

        for title in allowed:
            self.assertIn(f"《{title}》", body)
        self.assertIn("《陌生书名》", body)
        self.assertEqual(len(rewrites), 4)

        ok, details = _book_candidate_contract_result(
            body.replace("，不推荐未经规划的《陌生书名》", ""),
            objective="推荐至少四本机器学习书",
            evidence=[],
            allowed_candidate_titles=allowed,
            target_candidate_count=4,
        )
        self.assertTrue(ok)
        self.assertEqual(details["unsupported_titles"], [])

        expanded, expanded_details = _book_candidate_contract_result(
            body,
            objective="推荐至少四本机器学习书",
            evidence=[],
            allowed_candidate_titles=allowed,
            target_candidate_count=4,
        )
        self.assertTrue(expanded)
        self.assertEqual(expanded_details["unsupported_titles"], ["陌生书名"])
        self.assertFalse(expanded_details["portfolio_alignment_pass"])

    def test_presentation_contract_is_adaptive_not_a_book_template(self) -> None:
        contract = build_research_presentation_contract(
            objective="推荐适合我的技术书并深入说明",
            review={
                "objective_plan": {
                    "task_type": "book_recommendation",
                    "answer_depth": "deep",
                    "decision_dimensions": ["能否形成项目作品"],
                }
            },
            language="zh-CN",
            candidate_count=4,
        )

        self.assertEqual(contract["layout"], "adaptive_user_answer")
        self.assertEqual(contract["decision_dimensions"], ["能否形成项目作品"])
        self.assertEqual(contract["table_columns"], [])
        self.assertEqual(contract["required_sections"], [])
        self.assertGreaterEqual(contract["max_body_chars"], 9000)

    def test_writer_boundary_excludes_unverified_candidate_evidence(self) -> None:
        evidence = [
            {"source_id": "catalog-a", "claim": "候选甲书目"},
            {"source_id": "support-a", "claim": "候选甲配套代码"},
            {"source_id": "unverified-b", "claim": "未核验候选乙"},
        ]
        selected = _candidate_bound_publication_evidence(
            evidence,
            candidate_rows=[
                {
                    "candidate_title": "候选甲",
                    "source_id": "catalog-a",
                    "bound_source_ids": ["catalog-a", "support-a"],
                }
            ],
        )

        self.assertEqual(
            [item["source_id"] for item in selected],
            ["catalog-a", "support-a"],
        )

    def test_verified_catalog_alias_binds_english_resource_evidence(self) -> None:
        rows = _verified_book_candidate_rows(
            objective="推荐深度学习技术书",
            evidence=[
                {
                    "source_id": "catalog",
                    "source_title": "Python深度学习 (豆瓣)",
                    "source_url": "https://book.douban.com/subject/30293801/",
                    "claim": "《Python深度学习》；英文原名《Deep Learning with Python》。",
                    "quality": "high",
                    "relevance": 5,
                },
                {
                    "source_id": "repository",
                    "source_title": "Deep Learning with Python examples",
                    "source_url": "https://github.com/example/deep-learning-with-python",
                    "claim": "《Deep Learning with Python》提供配套示例代码。",
                    "quality": "high",
                    "relevance": 5,
                },
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["bound_source_ids"],
            ["catalog", "repository"],
        )


class SemanticQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_model_draft_survives_soft_quality_rejection(self) -> None:
        packet = build_research_publication_packet(
            brief=build_user_answer_brief(
                "推荐三本机器学习书",
                task_type="book_recommendation",
                answer_depth="deep",
            ),
            evidence=[
                {
                    "claim": "候选甲覆盖入门所需的核心概念。",
                    "source_title": "候选甲介绍",
                    "source_url": "https://example.com/a",
                    "candidate_title": "候选甲",
                    "quality": "high",
                }
            ],
            allowed_recommendation_entities=["候选甲", "候选乙", "候选丙"],
            externally_verified_entities=["候选甲"],
            target_candidate_count=3,
        )
        draft = (
            "## 先说结论\n\n"
            "如果你需要先建立概念地图，可以从《候选甲》开始；"
            "它覆盖了入门所需的核心概念。[1]\n\n"
            "这份建议仍然有明确边界：当前草稿还没有展开另外两个候选，"
            "因此广度和比较深度没有达标，但已经给出了一个安全、可用的起点。"
        )

        class FakeModel:
            def bind(self, **kwargs):
                self.max_tokens = kwargs.get("max_tokens")
                return self

            async def ainvoke(self, prompt):
                self.prompt = prompt
                return type("Response", (), {"content": draft})()

        class FailedQuality:
            available = True
            passed = False

            @staticmethod
            def model_dump(*args, **kwargs):
                return {
                    "available": True,
                    "passed": False,
                    "repair_instructions": ["补齐另外两个允许候选"],
                }

        with (
            mock.patch("app.infra.llm.get_llm", return_value=FakeModel()),
            mock.patch(
                "app.services.research.publication.report_writer.evaluate_semantic_answer",
                new=mock.AsyncMock(return_value=FailedQuality()),
            ),
            mock.patch(
                "app.services.research.publication.report_writer.report_completed_step",
                new=mock.AsyncMock(),
            ),
            mock.patch(
                "app.services.research.publication.report_writer.report_model_completion",
                new=mock.AsyncMock(),
            ),
        ):
            rendered, cited_ids, _, body, attempts = await _generate_report_once(
                model_id="model-id",
                prompt="write",
                objective="推荐三本机器学习书",
                evidence=[
                    {
                        "source_id": "s1",
                        "claim": "候选甲覆盖入门所需的核心概念。",
                        "source_title": "候选甲介绍",
                        "source_url": "https://example.com/a",
                        "candidate_title": "候选甲",
                    }
                ],
                sources=[
                    {
                        "source_title": "候选甲介绍",
                        "source_url": "https://example.com/a",
                    }
                ],
                language="zh-CN",
                presentation_contract={
                    "min_body_chars": 80,
                    "max_body_chars": 9000,
                },
                publication_packet=packet,
            )

        self.assertIn("《候选甲》", rendered)
        self.assertIn("《候选甲》", body)
        self.assertEqual(cited_ids, ["s1"])
        self.assertTrue(attempts[-1]["ok"])
        self.assertTrue(attempts[-1]["accepted_with_quality_warning"])
        self.assertFalse(attempts[-1]["candidate_gate"]["missing_candidate_count"] == 0)

    async def test_semantic_review_requires_all_quality_axes(self) -> None:
        strategy = EvidenceStrategy(
            facets=[
                EvidenceFacet(
                    name="关键适配",
                    required_for_recommendation=True,
                )
            ]
        )
        packet = build_research_publication_packet(
            brief=build_user_answer_brief(
                "推荐一本书",
                task_type="book_recommendation",
                answer_depth="deep",
            ),
            evidence_strategy=strategy,
            evidence=[
                {
                    "claim": "候选甲覆盖目标主题",
                    "source_title": "来源",
                    "source_url": "https://example.com/a",
                    "quality": "high",
                }
            ],
        )

        class FakeModel:
            def bind(self, **kwargs):
                return self

            async def ainvoke(self, prompt):
                self.prompt = prompt
                return type(
                    "Response",
                    (),
                    {
                        "content": json.dumps(
                            {
                                "passed": True,
                                "directness": 5,
                                "user_need_coverage": 5,
                                "evidence_use": 5,
                                "synthesis_depth": 3,
                                "decision_usefulness": 5,
                                "missing_user_needs": ["缺少深入取舍"],
                                "unsupported_or_overstated": [],
                                "repair_instructions": ["补充会改变选择的条件"],
                            },
                            ensure_ascii=False,
                        )
                    },
                )()

        with mock.patch("app.infra.llm.get_llm", return_value=FakeModel()):
            result = await evaluate_semantic_answer(
                model_id="model-id",
                objective="推荐一本书",
                packet=packet,
                answer="候选甲值得考虑。[1]",
            )

        self.assertTrue(result.available)
        self.assertFalse(result.passed)
        self.assertEqual(result.synthesis_depth, 3)
        self.assertEqual(result.repair_instructions, ["补充会改变选择的条件"])


if __name__ == "__main__":
    unittest.main()
