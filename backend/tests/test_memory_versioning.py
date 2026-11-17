from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from pydantic import ValidationError

from app.services.memory import (
    ForgetMemoryTargetProposal,
    MemoryAssertionProposal,
    MemoryCanonicalizer,
    MemoryVersionRecord,
    SearchMemoryRequest,
    VersionedMemorySchemaRegistry,
)
from app.services.memory.providers.postgres import _query_terms
from app.services.memory.vector_recall import recall_memory_keys
from app.services.memory.version_search import VersionedMemorySearch


def _assertion(
    *,
    predicate: str,
    value: dict,
    evidence: str,
    qualifiers: dict | None = None,
) -> MemoryAssertionProposal:
    return MemoryAssertionProposal(
        subject="self",
        predicate=predicate,
        value=value,
        qualifiers=qualifiers or {},
        evidence_quote=evidence,
    )


def _name_version(
    *,
    version_no: int,
    name: str,
    valid_from: datetime,
    previous_version_id=None,
    operation: str = "create",
) -> MemoryVersionRecord:
    return MemoryVersionRecord(
        id=uuid4(),
        user_id=uuid4(),
        chain_id=uuid4(),
        schema_key="identity.self_reported_name",
        memory_key="identity.self_reported_name:self",
        version_no=version_no,
        operation=operation,
        previous_version_id=previous_version_id,
        source_event_id=uuid4(),
        receipt_id=f"receipt-{version_no}",
        canonical_hash="0" * 64,
        schema_version=1,
        subject="self",
        predicate="name",
        value={"name": name},
        qualifiers={},
        evidence_quote=f"I am {name}",
        valid_from=valid_from,
    )


def _fact_version(
    *,
    schema_key: str,
    memory_key: str,
    predicate: str,
    value: dict,
    evidence: str,
    valid_from: datetime,
    qualifiers: dict | None = None,
) -> MemoryVersionRecord:
    return MemoryVersionRecord(
        id=uuid4(),
        user_id=uuid4(),
        chain_id=uuid4(),
        schema_key=schema_key,
        memory_key=memory_key,
        version_no=1,
        operation="create",
        source_event_id=uuid4(),
        receipt_id=f"receipt-{memory_key}",
        canonical_hash="1" * 64,
        schema_version=1,
        subject="self",
        predicate=predicate,
        value=value,
        qualifiers=qualifiers or {},
        evidence_quote=evidence,
        valid_from=valid_from,
    )


class MemoryCanonicalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.canonicalizer = MemoryCanonicalizer()

    def test_registry_contains_only_controlled_v1_schemas(self) -> None:
        self.assertEqual(
            VersionedMemorySchemaRegistry().schema_keys,
            (
                "identity.self_reported_name",
                "identity.preferred_address",
                "identity.alias",
                "preference.entity",
                "relationship.entity",
                "possession.entity",
                "instruction.behavior",
                "feedback.outcome",
                "temporary.state",
            ),
        )

    def test_predicate_variants_classify_into_categories(self) -> None:
        registry = VersionedMemorySchemaRegistry()
        self.assertEqual(
            registry.resolve_predicate("养").schema_key,
            "possession.entity",
        )
        self.assertEqual(
            registry.resolve_predicate("我的物品").schema_key,
            "possession.entity",
        )
        self.assertEqual(
            registry.resolve_predicate("名字").schema_key,
            "identity.self_reported_name",
        )
        self.assertEqual(
            registry.resolve_predicate("想要").schema_key,
            "preference.entity",
        )

    def test_structured_entity_name_write_succeeds(self) -> None:
        result = self.canonicalizer.canonicalize(
            [
                MemoryAssertionProposal(
                    subject="xiaoyan",
                    predicate="entity_name",
                    value={"entity": "piglet", "name": "xiaoyan"},
                    qualifiers={},
                    evidence_quote="The piglet is named xiaoyan.",
                )
            ],
            source_text="The piglet is named xiaoyan.",
        )
        self.assertEqual(result.status, "ready", result.reason_codes)
        self.assertTrue(
            any(fact.schema_key == "entity.name" for fact in result.facts),
            result.reason_codes,
        )

    def test_possession_entities_form_independent_chains(self) -> None:
        cat = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="has",
                    value={"entity": "猫"},
                    qualifiers={"entity_type": "pet"},
                    evidence="我有一只猫",
                )
            ],
            source_text="我有一只猫",
        )
        dog = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="养",
                    value={"entity": "狗"},
                    qualifiers={"entity_type": "pet"},
                    evidence="我还有一只狗",
                )
            ],
            source_text="我还有一只狗",
        )
        same_cat = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="have",
                    value={"entity": "猫"},
                    qualifiers={"entity_type": "pet"},
                    evidence="我养了一只猫",
                )
            ],
            source_text="我养了一只猫",
        )
        self.assertEqual(cat.status, "ready")
        self.assertEqual(dog.status, "ready")
        self.assertEqual(same_cat.status, "ready")
        self.assertEqual(cat.facts[0].schema_key, "possession.entity")
        self.assertNotEqual(cat.facts[0].memory_key, dog.facts[0].memory_key)
        self.assertEqual(
            cat.facts[0].memory_key,
            same_cat.facts[0].memory_key,
        )
        self.assertEqual(
            cat.facts[0].canonical_hash,
            same_cat.facts[0].canonical_hash,
        )

    def test_named_pet_variants_share_entity_identity(self) -> None:
        named_pet = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="has",
                    value={"entity": "小白猫"},
                    qualifiers={
                        "entity_type": "pet",
                        "species": "cat",
                        "entity_role": "pet_name",
                    },
                    evidence="我有一只小白猫",
                )
            ],
            source_text="我有一只小白猫",
        )
        same_pet = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="pet",
                    value={"entity": "小白"},
                    qualifiers={
                        "entity_type": "pet",
                        "species": "cat",
                        "entity_role": "pet_name",
                    },
                    evidence="我的猫叫小白",
                )
            ],
            source_text="我的猫叫小白",
        )

        self.assertEqual(named_pet.status, "ready")
        self.assertEqual(same_pet.status, "ready")
        self.assertEqual(named_pet.facts[0].value["entity"], "小白")
        self.assertEqual(named_pet.facts[0].qualifiers["species"], "cat")
        self.assertEqual(
            named_pet.facts[0].memory_key,
            same_pet.facts[0].memory_key,
        )
        self.assertEqual(
            named_pet.facts[0].canonical_hash,
            same_pet.facts[0].canonical_hash,
        )

    def test_descriptive_pet_terms_are_not_over_normalized(self) -> None:
        descriptive = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="has",
                    value={"entity": "橘猫"},
                    evidence="我有一只橘猫",
                )
            ],
            source_text="我有一只橘猫",
        )
        title = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="likes",
                    value={"entity": "机器猫", "polarity": "like"},
                    evidence="我喜欢机器猫",
                )
            ],
            source_text="我喜欢机器猫",
        )

        self.assertEqual(descriptive.status, "ready")
        self.assertEqual(descriptive.facts[0].value["entity"], "橘猫")
        self.assertNotIn("species", descriptive.facts[0].qualifiers)
        self.assertEqual(title.status, "ready")
        self.assertEqual(title.facts[0].value["entity"], "机器猫")
        self.assertNotIn("species", title.facts[0].qualifiers)

    def test_forget_target_uses_entity_normalization(self) -> None:
        target_by_suffix = self.canonicalizer.resolve_targets(
            [
                ForgetMemoryTargetProposal(
                    subject="self",
                    predicate="pet",
                    identity={"entity": "小白猫"},
                    qualifiers={
                        "entity_type": "pet",
                        "species": "cat",
                        "entity_role": "pet_name",
                    },
                    evidence_quote="忘记小白猫",
                )
            ],
            source_text="忘记小白猫",
        )
        target_by_name = self.canonicalizer.resolve_targets(
            [
                ForgetMemoryTargetProposal(
                    subject="self",
                    predicate="has",
                    identity={"entity": "小白"},
                    qualifiers={
                        "entity_type": "pet",
                        "species": "cat",
                        "entity_role": "pet_name",
                    },
                    evidence_quote="忘记小白",
                )
            ],
            source_text="忘记小白",
        )

        self.assertEqual(target_by_suffix.status, "ready")
        self.assertEqual(target_by_name.status, "ready")
        self.assertEqual(
            target_by_suffix.targets[0].memory_key,
            target_by_name.targets[0].memory_key,
        )

    def test_entity_type_is_optional_qualifier(self) -> None:
        result = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="likes",
                    value={"entity": "科幻小说", "polarity": "like"},
                    evidence="我喜欢科幻小说",
                )
            ],
            source_text="我喜欢科幻小说",
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.facts[0].schema_key, "preference.entity")

    def test_unknown_predicate_still_clarifies_when_unclassifiable(
        self,
    ) -> None:
        result = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="invented_schema",
                    value={"value": "x"},
                    evidence="记住 x",
                )
            ],
            source_text="记住 x",
        )
        self.assertEqual(result.status, "clarification_required")
        self.assertIn("unknown_memory_predicate", result.reason_codes)
        self.assertIn("invented_schema", result.clarification_question)

    def test_history_scopes_select_expected_versions(self) -> None:
        v1 = _name_version(
            version_no=1,
            name="小红",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        v2 = _name_version(
            version_no=2,
            name="小白",
            valid_from=datetime(2026, 2, 1, tzinfo=timezone.utc),
            previous_version_id=v1.id,
            operation="correct",
        )
        v3 = _name_version(
            version_no=3,
            name="小红",
            valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
            previous_version_id=v2.id,
            operation="correct",
        )
        v1.superseded_by = v2.id
        v2.superseded_by = v3.id
        records = [v1, v2, v3]

        previous = VersionedMemorySearch().search(
            records,
            SearchMemoryRequest(query="", predicate="name", scope="previous"),
        )
        self.assertEqual(
            [item.value["name"] for item in previous.memories],
            ["小白"],
        )
        previous_with_query = VersionedMemorySearch().search(
            records,
            SearchMemoryRequest(
                query="之前叫什么",
                predicate="name",
                scope="previous",
            ),
        )
        self.assertEqual(
            [item.value["name"] for item in previous_with_query.memories],
            ["小白"],
        )
        current_with_noise_query = VersionedMemorySearch().search(
            records,
            SearchMemoryRequest(
                query="之前叫什么",
                predicate="name",
                scope="current",
            ),
        )
        self.assertEqual(current_with_noise_query.status, "empty")
        earliest = VersionedMemorySearch().search(
            records,
            SearchMemoryRequest(query="", predicate="name", scope="earliest"),
        )
        self.assertEqual(
            [item.value["name"] for item in earliest.memories],
            ["小红"],
        )
        timeline = VersionedMemorySearch().search(
            records,
            SearchMemoryRequest(query="", predicate="name", scope="timeline"),
        )
        self.assertEqual(len(timeline.memories), 3)
        self.assertEqual(timeline.scope, "timeline")

    def test_current_search_uses_schema_hints_and_cjk_terms(self) -> None:
        pet = _fact_version(
            schema_key="possession.entity",
            memory_key="possession.entity:pet-xiaobai",
            predicate="has",
            value={"entity": "小白"},
            qualifiers={"entity_type": "pet"},
            evidence="我养了一只猫叫小白",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        preference = _fact_version(
            schema_key="preference.entity",
            memory_key="preference.entity:sci-fi",
            predicate="likes",
            value={"entity": "科幻小说", "polarity": "like"},
            evidence="我喜欢科幻小说",
            valid_from=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        by_schema_hint = VersionedMemorySearch().search(
            [preference, pet],
            SearchMemoryRequest(query="我的宠物", scope="current"),
        )
        self.assertEqual(
            [item.memory_key for item in by_schema_hint.memories],
            ["possession.entity:pet-xiaobai"],
        )

        by_entity_overlap = VersionedMemorySearch().search(
            [preference, pet],
            SearchMemoryRequest(query="小白猫", scope="current"),
        )
        self.assertEqual(
            [item.memory_key for item in by_entity_overlap.memories],
            ["possession.entity:pet-xiaobai"],
        )

    def test_current_search_accepts_semantic_memory_key_hints(self) -> None:
        pet = _fact_version(
            schema_key="possession.entity",
            memory_key="possession.entity:pet-xiaobai",
            predicate="has",
            value={"entity": "小白"},
            qualifiers={"entity_type": "pet", "species": "cat"},
            evidence="我养了一只猫叫小白",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        preference = _fact_version(
            schema_key="preference.entity",
            memory_key="preference.entity:sci-fi",
            predicate="likes",
            value={"entity": "科幻小说", "polarity": "like"},
            evidence="我喜欢科幻小说",
            valid_from=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        without_semantic = VersionedMemorySearch().search(
            [preference, pet],
            SearchMemoryRequest(query="照顾它时要注意什么", scope="current"),
        )
        with_semantic = VersionedMemorySearch().search(
            [preference, pet],
            SearchMemoryRequest(query="照顾它时要注意什么", scope="current"),
            semantic_memory_keys=("possession.entity:pet-xiaobai",),
        )
        scoped_semantic = VersionedMemorySearch().search(
            [preference, pet],
            SearchMemoryRequest(
                query="照顾它时要注意什么",
                predicate="has",
                scope="current",
            ),
            semantic_memory_keys=("possession.entity:pet-xiaobai",),
        )

        self.assertEqual(without_semantic.status, "empty")
        self.assertEqual(with_semantic.status, "empty")
        self.assertEqual(
            [item.memory_key for item in scoped_semantic.memories],
            ["possession.entity:pet-xiaobai"],
        )

    def test_legacy_query_terms_expand_cjk_entity_aliases(self) -> None:
        terms = _query_terms("我的小白猫")

        self.assertIn("小白猫", terms)
        self.assertIn("小白", terms)
        self.assertIn("白猫", terms)

    def test_model_assertion_rejects_storage_identity(self) -> None:
        with self.assertRaises(ValidationError):
            MemoryAssertionProposal.model_validate(
                {
                    "subject": "self",
                    "predicate": "name",
                    "value": {"name": "冰露"},
                    "qualifiers": {},
                    "evidence_quote": "I am 冰露",
                    "memory_key": "model-must-not-choose",
                }
            )

    def test_name_corrections_share_key_but_change_hash(self) -> None:
        first = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="name",
                    value={"name": "冰露"},
                    evidence="I am 冰露",
                )
            ],
            source_text="I am 冰露",
        )
        second = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="self_reported_name",
                    value={"name": "小露"},
                    evidence="I am 小露",
                )
            ],
            source_text="I am 小露",
        )
        self.assertEqual(first.status, "ready")
        self.assertEqual(second.status, "ready")
        self.assertEqual(first.facts[0].memory_key, second.facts[0].memory_key)
        self.assertNotEqual(
            first.facts[0].canonical_hash,
            second.facts[0].canonical_hash,
        )

    def test_preference_polarity_changes_one_entity_chain(self) -> None:
        like = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="likes",
                    value={"entity": "科幻", "polarity": "like"},
                    qualifiers={"entity_type": "book_genre"},
                    evidence="我喜欢科幻",
                )
            ],
            source_text="我喜欢科幻",
        )
        avoid = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="dislikes",
                    value={"entity": "科幻", "polarity": "avoid"},
                    qualifiers={"entity_type": "book_genre"},
                    evidence="我现在想避开科幻",
                )
            ],
            source_text="我现在想避开科幻",
        )
        self.assertEqual(like.facts[0].memory_key, avoid.facts[0].memory_key)
        self.assertNotEqual(
            like.facts[0].canonical_hash,
            avoid.facts[0].canonical_hash,
        )

    def test_unknown_predicate_requires_clarification(self) -> None:
        result = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="invented_schema",
                    value={"value": "x"},
                    evidence="记住 x",
                )
            ],
            source_text="记住 x",
        )
        self.assertEqual(result.status, "clarification_required")

    def test_evidence_must_match_user_source(self) -> None:
        result = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="name",
                    value={"name": "冰露"},
                    evidence="I am 冰露",
                )
            ],
            source_text="I am 小露",
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn(
            "evidence_not_found_in_user_source",
            result.reason_codes,
        )

    def test_credentials_are_never_canonicalized(self) -> None:
        result = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="instruction",
                    value={
                        "instruction": "use this",
                        "credentials": {"api_key": "sk-secret-123456789"},
                    },
                    evidence="记住 api_key=sk-secret-123456789",
                )
            ],
            source_text="记住 api_key=sk-secret-123456789",
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn("secret_or_credential_forbidden", result.reason_codes)

    def test_conflicting_batch_is_all_or_nothing(self) -> None:
        result = self.canonicalizer.canonicalize(
            [
                _assertion(
                    predicate="name",
                    value={"name": "冰露"},
                    evidence="我是冰露",
                ),
                _assertion(
                    predicate="name",
                    value={"name": "小露"},
                    evidence="也是小露",
                ),
            ],
            source_text="我是冰露，也是小露",
        )
        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.facts, [])

    def test_singleton_forget_target_never_needs_model_memory_key(self) -> None:
        result = self.canonicalizer.resolve_targets(
            [
                ForgetMemoryTargetProposal(
                    subject="self",
                    predicate="name",
                    evidence_quote="忘记我的名字",
                )
            ],
            source_text="忘记我的名字",
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(
            result.targets[0].memory_key,
            "identity.self_reported_name:self",
        )

    def test_multi_forget_target_requires_identity(self) -> None:
        result = self.canonicalizer.resolve_targets(
            [
                ForgetMemoryTargetProposal(
                    subject="self",
                    predicate="preference",
                    evidence_quote="忘记这个偏好",
                )
            ],
            source_text="忘记这个偏好",
        )
        self.assertEqual(result.status, "clarification_required")


class MemoryVectorRecallTests(unittest.IsolatedAsyncioTestCase):
    async def test_vector_recall_returns_ranked_user_memory_keys(self) -> None:
        user_id = uuid4()
        other_user_id = uuid4()
        store = Mock()
        store.search = AsyncMock(
            return_value=[
                {
                    "score": 0.91,
                    "payload": {
                        "memory_key": "possession.entity:pet-xiaobai",
                        "user_id": str(user_id),
                    },
                },
                {
                    "score": 0.80,
                    "payload": {
                        "memory_key": "possession.entity:pet-xiaobai",
                        "user_id": str(user_id),
                    },
                },
                {
                    "score": 0.79,
                    "payload": {
                        "memory_key": "preference.entity:tea",
                        "user_id": str(other_user_id),
                    },
                },
                {
                    "score": 0.44,
                    "payload": {
                        "memory_key": "preference.entity:sci-fi",
                        "user_id": str(user_id),
                    },
                },
            ]
        )
        runtime = Mock()
        runtime.get_active_store.return_value = store

        with patch(
            "app.services.memory.vector_recall.get_embedding_space_runtime",
            return_value=runtime,
        ):
            keys = await recall_memory_keys(
                user_id=user_id,
                query="照顾它时要注意什么",
            )

        self.assertEqual(keys, ("possession.entity:pet-xiaobai",))
        runtime.get_active_store.assert_called_once_with("memory")
        store.search.assert_awaited_once_with(
            collection_name=f"memory:{user_id}",
            query_text="照顾它时要注意什么",
            limit=16,
        )

    async def test_vector_recall_fails_open_when_unavailable(self) -> None:
        with patch(
            "app.services.memory.vector_recall.get_embedding_space_runtime",
            side_effect=RuntimeError("not ready"),
        ):
            keys = await recall_memory_keys(
                user_id=uuid4(),
                query="anything",
            )

        self.assertEqual(keys, ())


if __name__ == "__main__":
    unittest.main()
