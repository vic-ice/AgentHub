from __future__ import annotations

import unittest

from app.services.research.search_policy import build_research_search_request
from app.services.research.search_policy import (
    is_book_recommendation_request,
    requested_book_count,
)


class ResearchSearchPolicyTests(unittest.TestCase):
    def test_book_recommendation_discovers_candidates_without_subject_lock(self) -> None:
        request = build_research_search_request(
            "深度搜索类似《失控》《写给管理者的睡前故事》风格的书籍推荐"
        )
        self.assertIn("book", request.requirements)
        self.assertIn("book_recommendation", request.requirements)
        self.assertNotIn("book.douban.com", request.include_domains)
        self.assertEqual(request.include_url_prefixes, [])
        self.assertIn("推荐书单", request.query)

    def test_book_similar_style_variants_match(self) -> None:
        for objective in (
            "像失控这种书有哪些",
            "类似《失控》的书推荐",
            "同类型的书有哪些",
            "找一些这种风格的书",
            "哪些旧书值得读",
        ):
            with self.subTest(objective=objective):
                request = build_research_search_request(objective)
                self.assertIn("book", request.requirements)
                self.assertIn("book_recommendation", request.requirements)
                self.assertNotIn("book.douban.com", request.include_domains)

    def test_natural_book_recommendation_with_bare_book_word_is_recognized(self) -> None:
        request = build_research_search_request(
            "今年有什么值得读的书吗，想看沟通和财商方向"
        )
        self.assertIn("book", request.requirements)
        self.assertIn("book_recommendation", request.requirements)
        self.assertNotIn("book.douban.com", request.include_domains)

    def test_long_book_request_with_count_and_output_schema_is_recognized(self) -> None:
        request = build_research_search_request(
            "请推荐至少4本适合零基础读者的人工智能入门书。"
            "请按书名、作者、推荐理由、适合人群、来源链接的5列表格输出，"
            "不要推荐虚构书目。"
        )

        self.assertIn("book", request.requirements)
        self.assertIn("book_recommendation", request.requirements)
        self.assertNotIn("book.douban.com", request.include_domains)
        self.assertEqual(requested_book_count(request.objective), 4)

    def test_book_punctuation_and_conjunction_variants_share_one_intent(self) -> None:
        for objective in (
            "推荐人工智能入门书，并按表格输出",
            "推荐人工智能入门书并按表格输出",
            "推荐人工智能入门书；每本说明作者",
            "请列出书名和作者，推荐几本人工智能读物",
            "近期值得阅读的个人成长图书",
        ):
            with self.subTest(objective=objective):
                request = build_research_search_request(objective)
                self.assertIn("book", request.requirements)
                self.assertIn("book_recommendation", request.requirements)
                self.assertTrue(is_book_recommendation_request(objective))

    def test_quoted_book_with_bibliographic_fields_uses_book_sources(self) -> None:
        request = build_research_search_request(
            "深度研究《任意作品》的作者、核心主题和出版信息"
        )
        self.assertIn("book", request.requirements)
        self.assertIn("https://book.douban.com/subject/", request.include_url_prefixes)

    def test_non_book_question_stays_general(self) -> None:
        request = build_research_search_request("图书馆几点开门")
        self.assertNotIn("book", request.requirements)

    def test_explicit_source_domain_is_respected(self) -> None:
        request = build_research_search_request(
            "推荐几本好书",
            constraints=[
                {
                    "field": "source_domain",
                    "operator": "in",
                    "value": ["example.com"],
                }
            ],
        )
        self.assertIn("example.com", request.include_domains)
        self.assertNotIn("book.douban.com", request.include_domains)


if __name__ == "__main__":
    unittest.main()
