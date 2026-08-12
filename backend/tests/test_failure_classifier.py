from __future__ import annotations

import unittest

from app.services.agent_runtime.failure_classifier import (
    classify_capability_failure,
)


class FailureClassifierTests(unittest.TestCase):
    def test_ok_status_is_ok(self) -> None:
        self.assertEqual(
            classify_capability_failure(status="ok"),
            "ok",
        )

    def test_timeout_is_retry(self) -> None:
        self.assertEqual(
            classify_capability_failure(status="timeout"),
            "retry",
        )
        self.assertEqual(
            classify_capability_failure(
                status="failed",
                error_type="connection timeout",
            ),
            "retry",
        )

    def test_empty_result_is_skip(self) -> None:
        self.assertEqual(
            classify_capability_failure(status="empty_result"),
            "skip",
        )

    def test_garbage_is_skip(self) -> None:
        self.assertEqual(
            classify_capability_failure(status="garbage_login_wall"),
            "skip",
        )

    def test_unavailable_is_degrade(self) -> None:
        self.assertEqual(
            classify_capability_failure(status="unavailable"),
            "degrade",
        )

    def test_hard_error_is_abandon(self) -> None:
        self.assertEqual(
            classify_capability_failure(status="hard_error"),
            "abandon",
        )
        self.assertEqual(
            classify_capability_failure(status="blocked"),
            "abandon",
        )


if __name__ == "__main__":
    unittest.main()
