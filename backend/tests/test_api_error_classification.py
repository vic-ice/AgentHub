from __future__ import annotations

import unittest

from app.api.errors import classify_llm_error, is_llm_related_error


class ApiErrorClassificationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
