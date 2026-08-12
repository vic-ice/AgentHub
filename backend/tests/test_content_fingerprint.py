from __future__ import annotations

import unittest

from app.services.research.dedup import (
    ShingleJaccardDetector,
    dedup_by_content,
    jaccard_similarity,
    shingles,
)


class ContentFingerprintTests(unittest.TestCase):
    def test_jaccard_of_identical_text_is_one(self) -> None:
        text = "同一篇文章的正文内容"
        self.assertEqual(
            jaccard_similarity(
                shingles(text),
                shingles(text),
            ),
            1.0,
        )

    def test_near_duplicate_detected_across_different_urls(self) -> None:
        detector = ShingleJaccardDetector(threshold=0.8)
        body = (
            "这是一篇财经报道的正文，包含公司季度营收增长的具体数据，"
            "以及管理层对未来的展望与风险提示。"
        )
        repost = (
            "【转载】这是一篇财经报道的正文，包含公司季度营收增长的"
            "具体数据，以及管理层对未来的展望与风险提示。"
        )
        is_dup, _matched = detector.is_near_duplicate(
            repost,
            [body],
        )
        self.assertTrue(is_dup)

    def test_dedup_by_content_keeps_first_original(self) -> None:
        items = [
            {"url": "https://a.example/1", "text": "同一正文内容完全相同"},
            {"url": "https://b.example/2", "text": "同一正文内容完全相同"},
            {"url": "https://c.example/3", "text": "完全不同的另一篇文章内容"},
        ]
        kept = dedup_by_content(items, text_of=lambda item: item["text"])
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0]["url"], "https://a.example/1")
        self.assertEqual(kept[1]["url"], "https://c.example/3")

    def test_high_threshold_keeps_near_duplicates(self) -> None:
        items = [
            {"id": 1, "text": "内容基本一致的一篇文章"},
            {"id": 2, "text": "内容基本一致的一篇文章（小改）"},
        ]
        kept = dedup_by_content(
            items,
            text_of=lambda item: item["text"],
            detector=ShingleJaccardDetector(threshold=1.0),
        )
        self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main()
