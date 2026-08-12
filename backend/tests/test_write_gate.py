from __future__ import annotations

import unittest

from app.services.memory.write_gate import (
    validate_forget_write,
    validate_remember_write,
)


class RememberWriteGateTests(unittest.TestCase):
    def _assertions(self, *items: dict) -> list[dict]:
        return list(items)

    def test_valid_named_possession_passes(self) -> None:
        ok, reason = validate_remember_write(
            "我有一个小猪，叫xiaoyan",
            self._assertions(
                {
                    "subject": "self",
                    "predicate": "has",
                    "value": {"entity": "小猪"},
                    "qualifiers": {},
                },
                {
                    "subject": "小猪",
                    "predicate": "entity_name",
                    "value": {"entity": "小猪", "name": "xiaoyan"},
                    "qualifiers": {},
                },
            ),
        )
        self.assertTrue(ok, reason)

    def test_question_source_is_rejected(self) -> None:
        ok, reason = validate_remember_write(
            "我有小猪吗",
            self._assertions(
                {
                    "subject": "self",
                    "predicate": "has",
                    "value": {"entity": "小猪"},
                    "qualifiers": {},
                }
            ),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "memory_write_question_source")

    def test_negation_source_is_rejected(self) -> None:
        ok, reason = validate_remember_write(
            "别记住我叫黄仁",
            self._assertions(
                {
                    "subject": "self",
                    "predicate": "name",
                    "value": {"name": "黄仁"},
                    "qualifiers": {},
                }
            ),
        )
        self.assertFalse(ok)

    def test_uncertain_source_is_rejected(self) -> None:
        ok, reason = validate_remember_write(
            "我觉得我有小猪",
            self._assertions(
                {
                    "subject": "self",
                    "predicate": "has",
                    "value": {"entity": "小猪"},
                    "qualifiers": {},
                }
            ),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "memory_write_uncertain_source")

    def test_metalinguistic_source_is_rejected(self) -> None:
        ok, reason = validate_remember_write(
            "代码示例：我有小猪",
            self._assertions(
                {
                    "subject": "self",
                    "predicate": "has",
                    "value": {"entity": "小猪"},
                    "qualifiers": {},
                }
            ),
        )
        self.assertFalse(ok)

    def test_entity_with_question_particle_is_rejected(self) -> None:
        ok, reason = validate_remember_write(
            "我有小猪",
            self._assertions(
                {
                    "subject": "self",
                    "predicate": "has",
                    "value": {"entity": "小猪吗"},
                    "qualifiers": {},
                }
            ),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "entity_contains_question_particle")

    def test_entity_equals_name_is_rejected(self) -> None:
        ok, reason = validate_remember_write(
            "我有一个小猪，叫xiaoyan",
            self._assertions(
                {
                    "subject": "self",
                    "predicate": "entity_name",
                    "value": {"entity": "xiaoyan", "name": "xiaoyan"},
                    "qualifiers": {},
                }
            ),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "entity_equals_name")

    def test_missing_entity_is_rejected(self) -> None:
        ok, reason = validate_remember_write(
            "我有一个小猪",
            self._assertions(
                {
                    "subject": "self",
                    "predicate": "has",
                    "value": {},
                    "qualifiers": {},
                }
            ),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_entity")


    def test_naming_pattern_without_entity_name_is_rejected(self) -> None:
        ok, reason = validate_remember_write(
            "我有一个小猪，叫xiaoyan",
            self._assertions(
                {
                    "subject": "self",
                    "predicate": "has",
                    "value": {"entity": "xiaoyan", "entity_type": "pet"},
                    "qualifiers": {},
                }
            ),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "naming_pattern_missing_entity_name")

    def test_self_name_write_unaffected_by_naming_rule(self) -> None:
        ok, reason = validate_remember_write(
            "我叫黄仁",
            self._assertions(
                {
                    "subject": "self",
                    "predicate": "name",
                    "value": {"name": "黄仁"},
                    "qualifiers": {},
                }
            ),
        )
        self.assertTrue(ok, reason)


class ForgetWriteGateTests(unittest.TestCase):
    def test_forget_allowed(self) -> None:
        ok, reason = validate_forget_write(
            "忘掉我叫冰露"
        )
        self.assertTrue(ok, reason)

    def test_contradicted_forget_is_rejected(self) -> None:
        ok, reason = validate_forget_write(
            "别忘了我叫黄仁"
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
