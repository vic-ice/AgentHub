from __future__ import annotations

import unittest

from app.services.research.evidence_quality import assess_evidence_candidate
from app.services.research.evidence_quality import classify_source, source_quality


class ResearchEvidenceQualityTests(unittest.TestCase):
    def test_social_content_hosts_are_low_quality_leads(self) -> None:
        for url in (
            "https://www.instagram.com/p/example",
            "https://www.toutiao.com/article/123",
            "https://www.sina.cn/news/detail/123.html",
            "https://x.com/example/status/1",
        ):
            with self.subTest(url=url):
                source_class = classify_source(url)
                self.assertEqual(source_class, "social")
                self.assertEqual(source_quality(source_class), "low")

    def test_document_rehost_and_video_snippets_are_not_publishable_facts(self) -> None:
        for url in (
            "https://www.youtube.com/watch?v=example",
            "https://www.scribd.com/document/123/example",
            "https://someone.github.io/post",
        ):
            with self.subTest(url=url):
                assessment = assess_evidence_candidate(
                    claim="《任意作品》的作者和出版信息在这里有一段介绍。",
                    query="《任意作品》的作者和出版信息",
                    source_title="用户上传或转述内容",
                    source_url=url,
                )
                self.assertFalse(assessment.publishable)
                self.assertIn("low_trust_source", assessment.reason_codes)

    def test_personal_book_review_and_truncated_author_are_not_facts(self) -> None:
        for claim, reason in (
            (
                "这本书让我想起来一种沟通困境，这是我的阅读感悟。",
                "personal_review_excerpt",
            ),
            (
                "这篇书评可能有关键情节透露 《任意作品》 作者：【美】Marshall B.",
                "truncated_fact_fragment",
            ),
        ):
            with self.subTest(claim=claim):
                assessment = assess_evidence_candidate(
                    claim=claim,
                    query="《任意作品》的作者、核心主题和出版信息",
                    source_title="任意作品 (豆瓣)",
                    source_url="https://book.douban.com/subject/123/",
                )
                self.assertFalse(assessment.publishable)
                self.assertIn(reason, assessment.reason_codes)

    def test_source_ui_artifact_is_not_publishable(self) -> None:
        assessment = assess_evidence_candidate(
            claim="( 查看原文 回复 125 赞 引自第4页 任意作品的第一个要素是观察。",
            query="《任意作品》的核心主题",
            source_title="任意作品 (豆瓣)",
            source_url="https://book.douban.com/subject/123/",
        )
        self.assertFalse(assessment.publishable)
        self.assertIn("source_ui_artifact", assessment.reason_codes)


if __name__ == "__main__":
    unittest.main()
