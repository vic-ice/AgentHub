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


if __name__ == "__main__":
    unittest.main()
