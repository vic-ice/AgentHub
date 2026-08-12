from __future__ import annotations

import unittest

from app.services.research.loop.loop_detector import QueryLoopDetector


BASE = "推荐类似《失控》的图书"


class QueryLoopDetectorTests(unittest.TestCase):
    def test_same_query_is_loop(self) -> None:
        detector = QueryLoopDetector(
            history=[f"{BASE} 评分 书评 推荐 榜单"],
            exclude_prefix=BASE,
        )
        result = detector.check(f"{BASE} 评分 书评 推荐 榜单")
        self.assertTrue(result["is_loop"])
        self.assertIn(BASE, result["matched_query"])

    def test_different_gap_suffix_is_not_loop(self) -> None:
        detector = QueryLoopDetector(
            history=[f"{BASE} 评分 书评 推荐 榜单"],
            exclude_prefix=BASE,
        )
        result = detector.check(f"{BASE} 出版日期 新书 发布 今年")
        self.assertFalse(result["is_loop"])

    def test_empty_variant_is_not_judged(self) -> None:
        detector = QueryLoopDetector(
            history=[BASE],
            exclude_prefix=BASE,
        )
        result = detector.check(BASE)
        self.assertFalse(result["is_loop"])

    def test_hint_mentions_angle_change_for_empty_result(self) -> None:
        detector = QueryLoopDetector(
            history=[f"{BASE} 评分 书评"],
            exclude_prefix=BASE,
        )
        detector.mark_result_quality("empty_result")
        result = detector.check(f"{BASE} 评分 书评")
        self.assertTrue(result["is_loop"])
        self.assertIn("完全不同的角度", result["hint"])

    def test_hint_suggests_direct_url_for_garbage(self) -> None:
        detector = QueryLoopDetector(
            history=[f"{BASE} 评分 书评"],
            exclude_prefix=BASE,
        )
        detector.mark_result_quality("garbage_login_wall")
        result = detector.check(f"{BASE} 评分 书评")
        self.assertTrue(result["is_loop"])
        self.assertIn("可信来源", result["hint"])

    def test_window_evicts_old_queries(self) -> None:
        detector = QueryLoopDetector(
            window=2,
            exclude_prefix=BASE,
        )
        detector.check(f"{BASE} 评分")
        detector.check(f"{BASE} 出版日期")
        detector.check(f"{BASE} 独立书评")
        detector.check(f"{BASE} 权威来源")
        result = detector.check(f"{BASE} 评分")
        self.assertFalse(result["is_loop"])


if __name__ == "__main__":
    unittest.main()
