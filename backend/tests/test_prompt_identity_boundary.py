from __future__ import annotations

import unittest

from app.services.agent_core.prompt_composer import CORE_HARNESS


class PromptIdentityBoundaryTests(unittest.TestCase):
    def test_harness_defines_role_and_source_boundaries(self) -> None:
        normalized = " ".join(CORE_HARNESS.split())
        self.assertIn("Keep source roles separate", normalized)
        self.assertIn("System messages define your runtime role", normalized)
        self.assertIn("User messages define the current request", normalized)
        self.assertIn("Assistant messages are conversation history only", normalized)
        self.assertIn("Resolve each question's target", normalized)

    def test_harness_does_not_lock_identity_to_surface_examples(self) -> None:
        self.assertNotIn("你是谁", CORE_HARNESS)
        self.assertNotIn("我是谁", CORE_HARNESS)
        self.assertNotIn("我叫什么", CORE_HARNESS)


if __name__ == "__main__":
    unittest.main()

