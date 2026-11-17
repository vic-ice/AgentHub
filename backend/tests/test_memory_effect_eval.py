from __future__ import annotations

import unittest
from pathlib import Path

from app.services.memory.effect_eval import (
    MemoryEffectEvaluator,
    PRODUCTION_STRATEGIES,
    load_memory_effect_cases,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "memory_effect_eval_v1.jsonl"


class MemoryEffectEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset_version, cls.cases = load_memory_effect_cases(FIXTURE)
        cls.report = MemoryEffectEvaluator().run(
            cls.cases,
            dataset_version=cls.dataset_version,
        )

    def test_dataset_has_multi_dimensional_quality_coverage(self) -> None:
        self.assertEqual(self.dataset_version, "memory-effect-eval-v1")
        self.assertGreaterEqual(len(self.cases), 40)
        dimensions = {case.dimension for case in self.cases}
        self.assertEqual(dimensions, {"route", "write", "forget", "query"})
        tags = {tag for case in self.cases for tag in case.tags}
        for tag in (
            "safety",
            "over_normalization",
            "open_entity",
            "semantic_guard",
            "history",
            "prohibition",
            "metalinguistic",
            "temporary_state",
            "forget",
        ):
            self.assertIn(tag, tags)

    def test_production_strategies_pass_the_effect_gate(self) -> None:
        self.assertTrue(self.report.production_passed)
        self.assertTrue(
            all(gate.passed for gate in self.report.quality_gates),
            self.report.quality_gates,
        )
        production_failures = [
            result
            for result in self.report.results
            if result.strategy in PRODUCTION_STRATEGIES and not result.passed
        ]
        self.assertEqual(production_failures, [])

    def test_guardrail_cases_cover_write_read_and_forget_confusion(self) -> None:
        passed = {
            (result.strategy, result.case_id)
            for result in self.report.results
            if result.passed
        }
        for item in (
            ("rule_router", "route_do_not_forget_name"),
            ("rule_router", "route_remember_cue_not_overblocked"),
            ("current_canonicalizer", "write_do_not_store_name_rejected"),
            ("current_canonicalizer", "write_remember_cue_allowed"),
            ("current_canonicalizer", "write_do_not_forget_value_allowed"),
            ("current_forget_resolver", "forget_do_not_forget_rejected"),
            ("current_forget_resolver", "forget_stop_remember_allowed"),
            ("guarded_semantic", "query_prohibition_no_memory_leak"),
            ("guarded_semantic", "query_metalinguistic_no_memory_leak"),
        ):
            self.assertIn(item, passed)

    def test_ablation_candidates_expose_real_tradeoffs(self) -> None:
        aggressive_write_failures = {
            result.case_id
            for result in self.report.results
            if result.strategy == "aggressive_entity_normalizer"
            and not result.passed
        }
        self.assertIn("write_descriptive_cat_not_pet_name", aggressive_write_failures)
        self.assertIn("write_media_title_not_pet", aggressive_write_failures)
        self.assertIn("write_org_cat_eye_not_pet", aggressive_write_failures)

        aggressive_query_failures = {
            result.case_id
            for result in self.report.results
            if result.strategy == "aggressive_semantic"
            and not result.passed
        }
        self.assertIn("query_semantic_no_predicate_guard", aggressive_query_failures)

        lexical_failures = {
            result.case_id
            for result in self.report.results
            if result.strategy == "lexical_only" and not result.passed
        }
        self.assertIn("query_semantic_with_predicate", lexical_failures)

    def test_failures_are_grouped_by_root_cause_family(self) -> None:
        families = {
            (item.strategy, item.family): set(item.case_ids)
            for item in self.report.failure_families
        }
        self.assertIn(
            "write_descriptive_cat_not_pet_name",
            families[("aggressive_entity_normalizer", "over_normalization")],
        )
        self.assertIn(
            "query_semantic_no_predicate_guard",
            families[("aggressive_semantic", "false_recall")],
        )
        self.assertIn(
            "query_semantic_with_predicate",
            families[("lexical_only", "missed_recall")],
        )


if __name__ == "__main__":
    unittest.main()
