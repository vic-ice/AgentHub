from __future__ import annotations

import unittest

from app.services.research.text_cleaner import (
    clean_source_title,
    clean_text_for_context,
)


class TextCleanerTests(unittest.TestCase):
    def test_strips_html_entities_and_boilerplate(self):
        html = (
            "<div>\u4e3b\u5185\u5bb9 &amp; \u5f15\u7528</div>\n"
            "\u4e0a\u4e00\u7bc7\uff1axxx\n"
            "\u7248\u6743\u6240\u6709 \u00a9 2026\n"
            "\u76f8\u5173\u9605\u8bfb\n"
            "\u6b63\u6587\u7b2c\u4e00\u6bb5\u3002\n"
        )
        out = clean_text_for_context(html)
        self.assertNotIn("\u7248\u6743", out)
        self.assertNotIn("\u4e0a\u4e00\u7bc7", out)
        self.assertNotIn("&amp;", out)
        self.assertNotIn("<div>", out)
        self.assertIn("\u6b63\u6587\u7b2c\u4e00\u6bb5", out)

    def test_title_suffix_and_qa_noise_removed(self):
        self.assertEqual(
            clean_source_title(
                "2024\u5e74\u4eba\u5de5\u667a\u80fd\u7545\u9500\u56fe\u4e66TOP10 | \u5b89\u5168\u5185\u53c2"
            ),
            "2024\u5e74\u4eba\u5de5\u667a\u80fd\u7545\u9500\u56fe\u4e66TOP10",
        )
        self.assertEqual(
            clean_source_title(
                "\u6709\u4ec0\u4e48\u63a8\u8350\u7684\u673a\u5668\u5b66\u4e60\u5165\u95e8\u4e66\u7c4d- \u95ee\u7b54- \u7535\u5b50\u5de5\u7a0b\u4e16\u754c"
            ),
            "\u6709\u4ec0\u4e48\u63a8\u8350\u7684\u673a\u5668\u5b66\u4e60\u5165\u95e8\u4e66\u7c4d",
        )

    def test_empty_and_junk_are_harmless(self):
        self.assertEqual(clean_text_for_context(None), "")
        self.assertEqual(clean_source_title(" | \u00b7 -"), "")


if __name__ == "__main__":
    unittest.main()
