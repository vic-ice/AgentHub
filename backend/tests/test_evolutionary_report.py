from __future__ import annotations

import unittest

from app.services.agent_runtime.contracts import ActionReceipt
from app.services.research.projection import project_evolving_report


def _receipt(
    *,
    operation: str,
    output: dict,
    business_input: dict | None = None,
    status: str = "completed",
) -> ActionReceipt:
    return ActionReceipt(
        action_id=f"action-{operation}-{id(output)}",
        capability="research",
        operation=operation,
        status=status,
        business_input=business_input or {},
        output=output,
        admitted=True,
    )


def _admitted_evidence_result(
    *,
    claim: str,
    source_url: str,
    quality: str = "high",
) -> dict:
    return {
        "steps": [
            {
                "step_type": "add_evidence",
                "status": "completed",
                "output": {
                    "evidence_admission": {
                        "allowed": True,
                        "evidence": {
                            "claim": claim,
                            "source_url": source_url,
                            "source_title": "来源",
                            "quality": quality,
                        },
                    }
                },
            }
        ],
        "evidence": [
            {
                "claim": claim,
                "source_url": source_url,
                "quality": quality,
            }
        ],
    }


class EvolvingReportProjectionTests(unittest.TestCase):
    def test_no_research_receipts_returns_none(self) -> None:
        self.assertIsNone(
            project_evolving_report(
                [
                    _receipt(
                        operation="conversation_read",
                        output={"answer": "hello"},
                    )
                ]
            )
        )

    def test_projects_objective_facts_gaps_and_focus(self) -> None:
        receipts = [
            _receipt(
                operation="start_research",
                output={"run": {"objective": "推荐类似《失控》的书"}},
                business_input={"objective": "推荐类似《失控》的书"},
            ),
            _receipt(
                operation="acquire_research_sources",
                output={
                    "round_index": 1,
                    "executed": True,
                    "task": {"query": "推荐类似《失控》的书 评分 书评"},
                },
            ),
            _receipt(
                operation="add_evidence",
                output={
                    "results": [
                        _admitted_evidence_result(
                            claim="《失控》作者是凯文·凯利",
                            source_url="https://a.example/book",
                        )
                    ]
                },
            ),
            _receipt(
                operation="evaluate_research_gaps",
                output={
                    "gaps": ["missing_review_evidence"],
                    "gap_descriptions": ["未找到可核验评分或评论依据。"],
                    "satisfied": False,
                    "should_continue": True,
                    "stop_reason": "continue",
                    "next_actions": ["plan_next_search"],
                    "exhausted_queries": ["推荐类似《失控》的书 评分 书评"],
                },
            ),
        ]
        report = project_evolving_report(receipts)
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.objective, "推荐类似《失控》的书")
        self.assertEqual(report.step_count, 1)
        self.assertEqual(report.status, "running")
        self.assertEqual(len(report.confirmed_facts), 1)
        self.assertEqual(
            report.confirmed_facts[0].content,
            "《失控》作者是凯文·凯利",
        )
        self.assertEqual(
            report.confirmed_facts[0].source,
            "https://a.example/book",
        )
        self.assertEqual(report.confirmed_facts[0].confidence, "high")
        self.assertEqual(
            report.open_questions,
            ["未找到可核验评分或评论依据。"],
        )
        self.assertEqual(report.information_gaps, ["missing_review_evidence"])
        self.assertIn("plan_next_search", report.current_focus)
        self.assertEqual(
            report.exhausted_queries,
            ["推荐类似《失控》的书 评分 书评"],
        )

    def test_facts_are_deduplicated_by_claim(self) -> None:
        receipts = [
            _receipt(
                operation="start_research",
                output={"run": {"objective": "研究目标"}},
            ),
            _receipt(
                operation="add_evidence",
                output={
                    "results": [
                        _admitted_evidence_result(
                            claim="同一事实",
                            source_url="https://a.example/1",
                        )
                    ]
                },
            ),
            _receipt(
                operation="add_evidence",
                output={
                    "results": [
                        _admitted_evidence_result(
                            claim="同一事实",
                            source_url="https://b.example/2",
                        )
                    ]
                },
            ),
        ]
        report = project_evolving_report(receipts)
        assert report is not None
        self.assertEqual(len(report.confirmed_facts), 1)

    def test_conflicts_come_from_report_receipt(self) -> None:
        receipts = [
            _receipt(
                operation="start_research",
                output={"run": {"objective": "研究目标"}},
            ),
            _receipt(
                operation="build_research_report",
                output={"conflicts": ["来源A与来源B描述不一致"]},
            ),
        ]
        report = project_evolving_report(receipts)
        assert report is not None
        self.assertEqual(
            report.conflicts,
            ["来源A与来源B描述不一致"],
        )

    def test_confirmed_facts_capped_at_twenty(self) -> None:
        receipts = [
            _receipt(
                operation="start_research",
                output={"run": {"objective": "研究目标"}},
            )
        ]
        for index in range(25):
            receipts.append(
                _receipt(
                    operation="add_evidence",
                    output={
                        "results": [
                            _admitted_evidence_result(
                                claim=f"事实 {index}",
                                source_url=f"https://a.example/{index}",
                            )
                        ]
                    },
                )
            )
        report = project_evolving_report(receipts)
        assert report is not None
        self.assertEqual(len(report.confirmed_facts), 20)


if __name__ == "__main__":
    unittest.main()
