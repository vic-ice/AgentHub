from __future__ import annotations

import types
import unittest

from app.services.agent_core.contracts import PublishedAnswer
from app.services.agent_core.trusted_stream import _failure_message


class TrustedStreamFailureMessageTests(unittest.TestCase):
    def test_passes_through_real_failure_content(self) -> None:
        answer = PublishedAnswer(
            status="failed",
            content=(
                "模型服务暂时不可用：当前模型的额度已用尽或触发限流。"
                "请切换模型或稍后重试。"
            ),
            receipt_backed=False,
        )
        self.assertEqual(
            _failure_message(answer),
            "模型服务暂时不可用：当前模型的额度已用尽或触发限流。"
            "请切换模型或稍后重试。",
        )

    def test_keeps_arbitrary_trusted_failure_text(self) -> None:
        answer = PublishedAnswer(
            status="failed",
            content="本轮未能完成：外部检索超时。请稍后重试或更换问法。",
            receipt_backed=False,
        )
        self.assertEqual(
            _failure_message(answer),
            "本轮未能完成：外部检索超时。请稍后重试或更换问法。",
        )

    def test_falls_back_when_content_empty(self) -> None:
        answer = PublishedAnswer(
            status="failed",
            content="   ",
            receipt_backed=False,
        )
        self.assertEqual(
            _failure_message(answer),
            "本轮未能安全完成，请稍后重试。",
        )

    def test_falls_back_when_content_missing(self) -> None:
        answer = types.SimpleNamespace(status="failed", content=None)
        self.assertEqual(
            _failure_message(answer),
            "本轮未能安全完成，请稍后重试。",
        )


if __name__ == "__main__":
    unittest.main()
