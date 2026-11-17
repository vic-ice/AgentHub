from __future__ import annotations

import unittest

from app.services.research.content_quality_gate import (
    check_content_garbage,
    garbage_status,
)


class ContentQualityGateTests(unittest.TestCase):
    def test_login_wall_detected(self) -> None:
        text = "本文为会员专享内容，请登录后查看完整文章。" + "正文。" * 40
        is_garbage, reason = check_content_garbage(text)
        self.assertTrue(is_garbage)
        self.assertEqual(reason, "login_wall")

    def test_english_login_wall_detected(self) -> None:
        text = "This is premium content. Please log in to continue reading." + (
            " body" * 40
        )
        is_garbage, reason = check_content_garbage(text)
        self.assertTrue(is_garbage)
        self.assertEqual(reason, "login_wall")

    def test_too_short_detected_with_min_chars(self) -> None:
        is_garbage, reason = check_content_garbage("太短")
        self.assertTrue(is_garbage)
        self.assertEqual(reason, "too_short")

    def test_short_text_allowed_when_min_chars_disabled(self) -> None:
        is_garbage, reason = check_content_garbage(
            "短摘要也可以是有用的信息",
            min_chars=None,
        )
        self.assertFalse(is_garbage)
        self.assertEqual(reason, "")

    def test_ad_page_detected_by_url_ratio(self) -> None:
        text = " ".join(
            [
                "https://ads.example/click/1",
                "https://ads.example/click/2",
                "https://ads.example/click/3",
                "https://ads.example/click/4",
                "正文内容很少",
            ]
        )
        is_garbage, reason = check_content_garbage(text, min_chars=None)
        self.assertTrue(is_garbage)
        self.assertEqual(reason, "ad_page")

    def test_not_found_detected(self) -> None:
        text = "您访问的页面不存在，请返回首页继续浏览。" + "介绍。" * 40
        is_garbage, reason = check_content_garbage(text)
        self.assertTrue(is_garbage)
        self.assertEqual(reason, "not_found")

    def test_clean_content_passes(self) -> None:
        text = (
            "这本书讲述系统论与复杂科学，作者在书中梳理了控制论、"
            "信息论与自组织的演变脉络。"
        ) * 5
        is_garbage, reason = check_content_garbage(text)
        self.assertFalse(is_garbage)
        self.assertEqual(reason, "")

    def test_garbage_status_normalization(self) -> None:
        self.assertEqual(garbage_status("login_wall"), "garbage_login_wall")
        self.assertEqual(garbage_status("unknown"), "garbage_content")


if __name__ == "__main__":
    unittest.main()
