from __future__ import annotations

import unittest
from datetime import datetime, timezone
from uuid import uuid4

from app.services.memory.canonicalizer import MemoryCanonicalizer
from app.services.memory.version_contracts import (
    MemoryAssertionProposal,
    MemoryVersionRecord,
    SearchMemoryRequest,
)
from app.services.memory.version_search import VersionedMemorySearch


USER = uuid4()


def _proposal(
    subject: str,
    predicate: str,
    value: dict,
    evidence: str,
) -> MemoryAssertionProposal:
    return MemoryAssertionProposal(
        subject=subject,
        predicate=predicate,
        value=value,
        qualifiers={},
        evidence_quote=evidence,
    )


class EntityFactCanonicalizationTests(unittest.TestCase):
    def test_entity_subject_preference_canonicalizes(self) -> None:
        result = MemoryCanonicalizer().canonicalize(
            [
                _proposal(
                    "小猪",
                    "likes",
                    {"entity": "游泳", "polarity": "like"},
                    "小猪喜欢游泳",
                )
            ],
            source_text="小猪喜欢游泳",
        )
        self.assertEqual(result.status, "ready")
        fact = result.facts[0]
        self.assertEqual(fact.subject, "小猪")
        self.assertEqual(fact.schema_key, "preference.entity")
        self.assertEqual(fact.value, {"entity": "游泳", "polarity": "like"})

    def test_entity_and_self_facts_get_distinct_keys(self) -> None:
        entity = MemoryCanonicalizer().canonicalize(
            [
                _proposal(
                    "小猪",
                    "likes",
                    {"entity": "游泳", "polarity": "like"},
                    "小猪喜欢游泳",
                )
            ],
            source_text="小猪喜欢游泳",
        ).facts[0]
        self_fact = MemoryCanonicalizer().canonicalize(
            [
                _proposal(
                    "self",
                    "likes",
                    {"entity": "游泳", "polarity": "like"},
                    "我喜欢游泳",
                )
            ],
            source_text="我喜欢游泳",
        ).facts[0]
        self.assertNotEqual(entity.memory_key, self_fact.memory_key)

    def test_entity_subject_possession_canonicalizes(self) -> None:
        result = MemoryCanonicalizer().canonicalize(
            [
                _proposal(
                    "小猪",
                    "has",
                    {"entity": "玩具"},
                    "小猪有一个玩具",
                )
            ],
            source_text="小猪有一个玩具",
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.facts[0].subject, "小猪")




class CompiledAssertionOwnershipTests(unittest.TestCase):
    def test_self_preference_keeps_user_as_subject_and_value_entity_as_target(self) -> None:
        from app.services.memory.turn_compiler import compiled_turn_from_assertions

        compiled = compiled_turn_from_assertions(
            [
                MemoryAssertionProposal(
                    subject="self",
                    predicate="likes",
                    value={"entity": "喜羊羊", "polarity": "like"},
                    evidence_quote="我喜欢看喜羊羊",
                    domain="personal",
                    kind="preference",
                    entity_type="other",
                )
            ],
            raw_text="我喜欢看喜羊羊",
        )

        fact = compiled.facts[0]
        self.assertEqual(fact.subject, "self")
        self.assertEqual(fact.entity, "喜羊羊")
        self.assertEqual(fact.attributes["entity"], "喜羊羊")

    def test_named_entity_keeps_actor_and_preference_target_distinct(self) -> None:
        from app.services.memory.turn_compiler import compiled_turn_from_assertions

        compiled = compiled_turn_from_assertions(
            [
                MemoryAssertionProposal(
                    subject="小猪",
                    predicate="likes",
                    value={"entity": "游泳", "polarity": "like"},
                    evidence_quote="小猪喜欢游泳",
                    domain="personal",
                    kind="preference",
                    entity_type="other",
                )
            ],
            raw_text="小猪喜欢游泳",
        )

        fact = compiled.facts[0]
        self.assertEqual(fact.subject, "小猪")
        self.assertEqual(fact.entity, "小猪")
        self.assertEqual(fact.attributes["entity"], "游泳")
class EntityFactRenderTests(unittest.TestCase):
    def test_mutation_receipt_uses_entity_subject(self) -> None:
        from app.services.agent_core.publication.memory_renderer import (
            render_memory_mutation,
        )

        output = {
            "mutations": [
                {
                    "status": "created",
                    "version": {
                        "schema_key": "preference.entity",
                        "subject": "小猪",
                        "value": {"entity": "游泳", "polarity": "like"},
                        "qualifiers": {},
                        "evidence_quote": "小猪喜欢游泳",
                    },
                }
            ]
        }
        self.assertIn(
            "小猪喜欢游泳",
            render_memory_mutation(output),
        )

    def test_mutation_receipt_keeps_self_style(self) -> None:
        from app.services.agent_core.publication.memory_renderer import (
            render_memory_mutation,
        )

        output = {
            "mutations": [
                {
                    "status": "created",
                    "version": {
                        "schema_key": "preference.entity",
                        "subject": "self",
                        "value": {"entity": "日记", "polarity": "like"},
                        "qualifiers": {},
                        "evidence_quote": "我喜欢日记",
                    },
                }
            ]
        }
        self.assertIn(
            "你喜欢日记",
            render_memory_mutation(output),
        )


class EntityFactSearchTests(unittest.TestCase):
    def _record(self) -> MemoryVersionRecord:
        return MemoryVersionRecord(
            id=uuid4(),
            user_id=USER,
            chain_id=uuid4(),
            schema_key="preference.entity",
            memory_key="preference.entity:entity",
            version_no=1,
            operation="create",
            source_event_id=uuid4(),
            receipt_id="r",
            canonical_hash="h",
            schema_version=1,
            subject="小猪",
            predicate="preference.entity",
            value={"entity": "游泳", "polarity": "like"},
            qualifiers={},
            evidence_quote="小猪喜欢游泳",
            valid_from=datetime.now(timezone.utc),
        )

    def test_query_by_entity_subject_finds_fact(self) -> None:
        request = SearchMemoryRequest(
            query="小猪",
            predicate="prefer",
            scope="current",
        )
        result = VersionedMemorySearch().search(
            [self._record()],
            request,
        )
        self.assertTrue(result.memories)


if __name__ == "__main__":
    unittest.main()
