from __future__ import annotations

import unittest
import uuid

from app.services.research.contracts import ResearchEvidence
from app.services.research.verifier import (
    ClaimForVerification,
    ResearchVerifier,
    VerifierAdmissionInput,
)


class ResearchVerifierThresholdTests(unittest.TestCase):
    def _input(self, *, per_claim: int | None = None) -> VerifierAdmissionInput:
        run_id = uuid.uuid4()
        evidence_id = uuid.uuid4()
        claim = "《深度学习》系统介绍机器学习、神经网络与深度学习基础。"
        budget = {
            "min_independent_sources": 2,
            "required_evidence_quality": "medium",
        }
        if per_claim is not None:
            budget["min_independent_sources_per_claim"] = per_claim
        return VerifierAdmissionInput(
            run_id=run_id,
            objective="推荐人工智能与机器学习入门书",
            candidate_claims=[
                ClaimForVerification(
                    claim=claim,
                    evidence_ids=[evidence_id],
                    quality="medium",
                )
            ],
            evidence=[
                ResearchEvidence(
                    id=evidence_id,
                    run_id=run_id,
                    source_type="web",
                    source_title="深度学习 (豆瓣)",
                    source_url="https://book.douban.com/subject/27087503/",
                    claim=claim,
                    excerpt=claim,
                    quality="medium",
                    relevance=5,
                )
            ],
            budget=budget,
        )

    def test_global_source_threshold_does_not_apply_to_each_claim(self) -> None:
        result = ResearchVerifier().verify(self._input())

        self.assertEqual(len(result.admitted_claims), 1)
        self.assertEqual(result.admitted_claims[0].status, "admitted")
        self.assertTrue(result.ready_for_final_answer)

    def test_explicit_per_claim_threshold_still_requires_corroboration(self) -> None:
        result = ResearchVerifier().verify(self._input(per_claim=2))

        self.assertEqual(len(result.uncertain_claims), 1)
        self.assertIn(
            "insufficient_independent_sources",
            result.uncertain_claims[0].reason_codes,
        )
        self.assertFalse(result.ready_for_final_answer)


if __name__ == "__main__":
    unittest.main()
