from __future__ import annotations

import unittest

from app.services.agent_core.contracts import PublishedAnswer
from app.services.agent_core.publication.stream import TrustedStreamSequencer
from app.services.execution_progress import (
    ExecutionProgressCollector,
    attach_progress_to_answer,
    bind_execution_progress,
    extract_model_usage,
    report_model_completion,
    summarize_step_result,
)


class _UsageMessage:
    usage_metadata = {
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
        "input_token_details": {"cache_read": 80},
        "output_token_details": {"reasoning": 15},
    }
    response_metadata = {}
    additional_kwargs = {}


class ExecutionProgressTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_provider_usage_details_without_token_estimation(self) -> None:
        usage = extract_model_usage(_UsageMessage())

        self.assertEqual(usage.input_tokens, 120)
        self.assertEqual(usage.output_tokens, 45)
        self.assertEqual(usage.total_tokens, 165)
        self.assertEqual(usage.cached_tokens, 80)
        self.assertEqual(usage.reasoning_tokens, 15)

    def test_summarizes_domain_results_instead_of_raw_status(self) -> None:
        detail = summarize_step_result({
            "status": "completed",
            "total": 4,
            "items": [
                {"book_title": "失控"},
                {"book_title": "小狗钱钱"},
                {"book_title": "非暴力沟通"},
                {"book_title": "必然"},
            ],
        })

        self.assertIn("失控", detail)
        self.assertIn("4 项", detail)
        self.assertNotIn("status", detail)

    def test_summarizes_shelf_write_before_empty_memory_mutations(self) -> None:
        detail = summarize_step_result({
            "status": "committed",
            "mutations": [],
            "shelf_entries": [
                {
                    "title": "Python深度学习 (第2版)",
                    "reading_status": "reading",
                    "evaluation": "liked",
                    "note": "学习python",
                }
            ],
        })

        self.assertIn("已同步书架", detail)
        self.assertIn("Python深度学习", detail)
        self.assertNotIn("0 项更新", detail)

    def test_summarizes_multi_source_book_coverage_for_users(self) -> None:
        detail = summarize_step_result({
            "result_mode": "book_evidence",
            "status": "ok",
            "candidate_count": 4,
            "items": [
                {
                    "title": "候选甲",
                    "evidence_sources": [
                        {"url": "https://book.example/a"},
                        {"url": "https://review.example/a"},
                    ],
                },
                {
                    "title": "候选乙",
                    "evidence_sources": [
                        {"url": "https://book.example/b"},
                    ],
                },
            ],
            "coverage": [
                {"theme": "沟通", "candidate_count": 2, "status": "complete"},
                {"theme": "财商", "candidate_count": 2, "status": "complete"},
            ],
            "metadata": {
                "enrichment": [
                    {"providers": ["tavily", "ddgs"]},
                    {"providers": ["ddgs"]},
                ]
            },
        })

        self.assertIn("已筛选 4 本候选", detail)
        self.assertIn("3 个公开页面", detail)
        self.assertIn("2 个检索来源", detail)
        self.assertIn("沟通 2 本", detail)
        self.assertIn("财商 2 本", detail)
        self.assertNotIn("tavily", detail)

    async def test_completed_model_step_is_collected_and_published(self) -> None:
        callback_steps = []
        collector = ExecutionProgressCollector(
            business_type="chat",
            callback=callback_steps.append,
        )

        with bind_execution_progress(collector):
            step = await report_model_completion(
                _UsageMessage(),
                title="model completed",
                detail="controller decided",
                model_name="test-model",
                duration_ms=23,
            )

        self.assertIsNotNone(step)
        self.assertEqual(len(callback_steps), 1)
        self.assertEqual(collector.usage_summary().total_tokens, 165)
        answer = attach_progress_to_answer(
            PublishedAnswer(status="completed", content="ok"),
            collector,
        )
        payload = answer.custom_data["execution_progress"]
        self.assertEqual(payload["business_type"], "chat")
        self.assertEqual(payload["steps"][0]["model_name"], "test-model")
        self.assertEqual(payload["usage_summary"]["cached_tokens"], 80)

    async def test_step_event_keeps_monotonic_stream_sequence(self) -> None:
        collector = ExecutionProgressCollector(business_type="research")
        step = await collector.complete(
            kind="research",
            status="completed",
            title="round completed",
        )
        sequencer = TrustedStreamSequencer(request_id="request-progress")

        started = sequencer.turn_started()
        progress = sequencer.step_completed(step)

        self.assertEqual(started.sequence, 1)
        self.assertEqual(progress.sequence, 2)
        self.assertEqual(progress.type, "step.completed")
        self.assertEqual(progress.content["step"]["business_type"], "research")

    async def test_waiting_action_is_replaced_by_its_terminal_result(self) -> None:
        callback_steps = []
        collector = ExecutionProgressCollector(
            business_type="chat",
            callback=callback_steps.append,
        )

        await collector.complete(
            kind="action",
            status="waiting",
            title="查找图书",
            detail="正在交叉检索",
            step_id="action:book",
        )
        await collector.complete(
            kind="action",
            status="completed",
            title="查找图书",
            detail="已筛选 5 本候选",
            step_id="action:book",
        )

        self.assertEqual(len(collector.steps), 1)
        self.assertEqual(collector.steps[0].status, "completed")
        self.assertEqual(collector.steps[0].detail, "已筛选 5 本候选")
        self.assertEqual(len(callback_steps), 2)


if __name__ == "__main__":
    unittest.main()
