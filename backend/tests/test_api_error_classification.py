from __future__ import annotations

import unittest

from app.api.errors import (
    build_error_payload,
    classify_llm_error,
    is_llm_related_error,
    resolve_request_id,
)
from app.utils.logging import request_id_context


class ApiErrorClassificationTests(unittest.TestCase):
    def test_domain_type_error_is_not_mislabeled_as_llm_authentication(self) -> None:
        exc = TypeError("'url' is an invalid keyword argument for Book")
        self.assertFalse(is_llm_related_error(exc))
        self.assertEqual(classify_llm_error(exc), "unknown")

    def test_database_integrity_error_text_is_not_llm_error(self) -> None:
        exc = Exception(
            "sqlalchemy.exc.IntegrityError: foreign key constraint failed; "
            "parameters: user_id"
        )
        self.assertFalse(is_llm_related_error(exc))
        self.assertEqual(classify_llm_error(exc), "unknown")

    def test_connection_error_is_llm_connection(self) -> None:
        exc = Exception("Connection error: connect call failed")
        self.assertTrue(is_llm_related_error(exc))
        self.assertEqual(classify_llm_error(exc), "llm_connection")

    def test_error_payload_contains_stable_diagnostics(self) -> None:
        token = request_id_context.set("req-ui-test")
        try:
            payload = build_error_payload(
                detail="Book not found",
                error_type="http_exception",
                error_code="http_404",
                stage="http",
            )
        finally:
            request_id_context.reset(token)

        self.assertEqual(payload["request_id"], "req-ui-test")
        self.assertEqual(payload["error_code"], "http_404")
        self.assertEqual(payload["stage"], "http")
        self.assertFalse(payload["retryable"])

    def test_request_id_rejects_log_injection(self) -> None:
        self.assertEqual(resolve_request_id("client-123"), "client-123")
        generated = resolve_request_id("bad\nforged-log")
        self.assertTrue(generated.startswith("http-"))
        self.assertNotIn("\n", generated)


if __name__ == "__main__":
    unittest.main()
