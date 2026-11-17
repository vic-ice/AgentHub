from __future__ import annotations

import unittest

from app.services.research.search_policy import build_research_search_request


class ResearchSearchPolicyTests(unittest.TestCase):
    def test_book_objective_defaults_to_douban(self) -> None:
        request = build_research_search_request(
            "深度搜索类似《失控》《写给管理者的睡前故事》风格的书籍推荐"
        )
        self.assertIn("book", request.requirements)
        self.assertIn("book.douban.com", request.include_domains)
        self.assertIn("https://book.douban.com/subject/", request.include_url_prefixes)
        self.assertIn("site:book.douban.com/subject/", request.query)

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
                self.assertIn("book.douban.com", request.include_domains)

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
