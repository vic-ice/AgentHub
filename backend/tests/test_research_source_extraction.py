from __future__ import annotations

import unittest

from app.services.research.source_extraction import (
    extract_research_source_records,
)


class ResearchSourceExtractionTests(unittest.TestCase):
    def test_copyright_year_is_not_admitted_as_research_evidence(self) -> None:
        result = extract_research_source_records(
            query="2026年值得读的书",
            subquestion="寻找阅读建议",
            documents=[
                {
                    "source_title": "任意作品 (豆瓣)",
                    "source_url": "https://book.douban.com/subject/123/",
                    "content": "0 © 2005－2026 douban.com, all rights reserved.",
                }
            ],
            provider_source="source_visit",
        )
        self.assertEqual(result.source_records, [])

    def test_rss_subscription_footer_is_not_admitted(self) -> None:
        result = extract_research_source_records(
            query="推荐类似的书",
            subquestion="核对候选书",
            documents=[
                {
                    "source_title": "候选书 (豆瓣)",
                    "source_url": "https://book.douban.com/subject/123/",
                    "content": "订阅关于候选书的评论: feed: rss 2.0。",
                }
            ],
            provider_source="source_visit",
        )
        self.assertEqual(result.source_records, [])

    def test_person_pronoun_without_antecedent_is_not_an_atomic_claim(self) -> None:
        result = extract_research_source_records(
            query="核对某书作者与内容",
            subquestion="查找作者信息",
            documents=[
                {
                    "source_title": "某书 (豆瓣)",
                    "source_url": "https://book.douban.com/subject/123/",
                    "content": "她与作者一起创作了这本书，帮助读者改善财务状况。",
                }
            ],
            provider_source="source_visit",
        )
        self.assertEqual(result.source_records, [])

    def test_visited_page_navigation_is_not_evidence(self) -> None:
        result = extract_research_source_records(
            query="2026年推荐书",
            subquestion="核对候选书",
            documents=[
                {
                    "source_title": "某书 (豆瓣)",
                    "source_url": "https://book.douban.com/subject/123/",
                    "content": "0 全新发布 × 豆瓣 扫码直接下载 iPhone Android。",
                }
            ],
            provider_source="source_visit",
        )
        self.assertEqual(result.source_records, [])

    def test_source_visit_status_maps_to_research_step_contract(self) -> None:
        from app.services.research.deep_research_runner import (
            _research_visit_step_status,
        )

        self.assertEqual(
            _research_visit_step_status("no_extractable_claims", has_document=True),
            "completed",
        )
        self.assertEqual(
            _research_visit_step_status("no_extractable_claims", has_document=False),
            "empty_result",
        )

    def test_quoted_book_objective_filters_unrelated_subject_hits(self) -> None:
        from app.services.external_search.contracts import SearchHit
        from app.services.research.deep_research_runner import _book_entity_hits

        hits = [
            SearchHit(
                title="失控 (豆瓣)",
                url="https://book.douban.com/subject//1/?from=tag_all",
                provider="test",
            ),
            SearchHit(
                title="失控的书评",
                url="https://book.douban.com/subject/1/reviews",
                provider="test",
            ),
            SearchHit(
                title="科学与假说 (豆瓣)",
                url="https://book.douban.com/subject/2/",
                provider="test",
            ),
        ]
        selected = _book_entity_hits(hits, objective="核对《失控》的作者与出版信息")
        self.assertEqual([item.title for item in selected], ["失控 (豆瓣)"])
        self.assertEqual(
            selected[0].url,
            "https://book.douban.com/subject/1/",
        )

    def test_visited_book_page_yields_metadata_and_neutral_theme_facts(self) -> None:
        result = extract_research_source_records(
            query="《任意作品》 图书 作者 核心主题 中文版出版信息",
            subquestion="核对作者、主题与出版信息",
            documents=[
                {
                    "source_title": "任意作品 (豆瓣)",
                    "source_url": "https://book.douban.com/subject/123/",
                    "content": (
                        "登录/注册 任意作品 作者 : [美] Marshall B.Rosenberg "
                        "译者 : 某译者 出版社: 某出版社 出版年: 2021-1 "
                        "豆瓣评分 8.4。内容简介 任意作品介绍了一种沟通方法。"
                        "任意作品的四个要素包括观察、感受、需要和请求。"
                        "猜你喜欢《其他甲》《其他乙》《其他丙》。"
                    ),
                    "metadata": {"source_visit": {"contract_version": "v1"}},
                }
            ],
            provider_source="source_visit",
            max_records_per_document=5,
        )
        claims = [item.claim for item in result.source_records]
        self.assertTrue(any("Marshall B.Rosenberg" in item for item in claims))
        self.assertTrue(any("某出版社" in item and "2021-1" in item for item in claims))
        self.assertTrue(any("观察、感受、需要和请求" in item for item in claims))
        self.assertFalse(any(item.startswith("《其他") for item in claims))

    def test_search_snippet_personal_review_is_rejected(self) -> None:
        result = extract_research_source_records(
            query="《任意作品》 图书 作者 核心主题",
            subquestion="核对核心主题",
            documents=[
                {
                    "source_title": "任意作品 (豆瓣)",
                    "source_url": "https://book.douban.com/subject/123/",
                    "content": "这本书让我想起来一种沟通困境，这是我的阅读感悟。",
                }
            ],
            provider_source="web_search",
        )
        self.assertEqual(result.source_records, [])

    def test_institutional_booklist_yields_one_source_backed_candidate_per_book(self) -> None:
        result = extract_research_source_records(
            query="今年值得阅读的图书 推荐书单 出版社 编辑精选",
            subquestion="寻找沟通、成长和财商方向的候选书",
            documents=[
                {
                    "source_title": "某机构年度推荐书单",
                    "source_url": "https://example.org/reading-list",
                    "content": (
                        "《沟通候选》 作者：甲。"
                        "《财商候选》帮助读者理解储蓄与长期决策。"
                        "《成长候选》入选本年度阅读清单。"
                    ),
                    "quality": "medium",
                }
            ],
            provider_source="web_search",
            max_records_per_document=5,
        )
        claims = [item.claim for item in result.source_records]
        self.assertEqual(len(claims), 3)
        self.assertTrue(all(item.startswith("《") for item in claims))
        self.assertTrue(all("推荐书单" in item or "候选" in item for item in claims))


if __name__ == "__main__":
    unittest.main()
