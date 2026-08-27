from __future__ import annotations

import unittest

from app.services.publication_safety import (
    requested_table_columns,
    table_contract_satisfied,
)


class PublicationSafetyTableContractTests(unittest.TestCase):
    REQUEST = (
        "请按书名、作者、推荐理由、适合人群、来源链接的5列表格输出，"
        "不要推荐虚构书目。"
    )

    def test_parses_columns_from_natural_chinese_table_request(self) -> None:
        self.assertEqual(
            requested_table_columns(self.REQUEST),
            ["书名", "作者", "推荐理由", "适合人群", "来源链接"],
        )

    def test_rejects_table_that_drops_requested_columns(self) -> None:
        answer = (
            "| 书名 | 推荐理由 | 证据强度 | 来源 |\n"
            "| --- | --- | --- | --- |\n"
            "| 示例 | 示例 | medium | [来源](https://example.com) |"
        )

        self.assertFalse(
            table_contract_satisfied(answer, request=self.REQUEST)
        )

    def test_accepts_exact_requested_table_columns(self) -> None:
        answer = (
            "| 书名 | 作者 | 推荐理由 | 适合人群 | 来源链接 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 示例 | 作者 | 理由 | 初学者 | [来源](https://example.com) |"
        )

        self.assertTrue(
            table_contract_satisfied(answer, request=self.REQUEST)
        )


if __name__ == "__main__":
    unittest.main()
