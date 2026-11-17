"""Architecture guard: the unified Memory Write Gateway is the only path into
durable memory. Future changes that reintroduce an old chain must fail here.

1. version_runtime consumes Controller assertions and MUST NOT run a second semantic compiler.
2. MemoryVersionStore.commit is called ONLY from write_gateway.py.
3. Direct provider writes require domain/kind (and entity_id for entity facts).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1] / "app"


def _iter_py_files():
    return list(BACKEND.rglob("*.py"))


def _rel(path: Path) -> str:
    return str(path.relative_to(BACKEND)).replace("\\", "/")


class MemoryArchitectureGuardTests(unittest.TestCase):
    def test_all_non_conversation_provenance_kinds_are_store_admitted(self):
        from app.services.memory.version_contracts import PROVENANCE_SOURCE_KINDS
        from app.services.memory.version_store import _PROVENANCE_KINDS

        self.assertEqual(
            _PROVENANCE_KINDS,
            PROVENANCE_SOURCE_KINDS - {"user_message"},
        )

    def test_version_runtime_uses_gateway_not_old_chain(self):
        src = (BACKEND / "services" / "memory" / "version_runtime.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("MemoryWriteGateway", src)
        self.assertIn("compiled_turn_from_assertions", src)
        self.assertIn("forget_targets", src)
        self.assertNotIn("TurnFactCompiler", src)
        self.assertNotIn("MemoryCanonicalizer().canonicalize(", src)
        self.assertNotIn("MemoryCanonicalizer().resolve_targets(", src)
        self.assertNotIn("MemoryVersionStore(session).commit(", src)

    def test_management_and_chat_forward_explicit_provenance(self):
        admin = (BACKEND / "api" / "v1" / "memory.py").read_text(
            encoding="utf-8-sig"
        )
        runtime = (
            BACKEND / "services" / "memory" / "version_runtime.py"
        ).read_text(encoding="utf-8-sig")
        gateway = (
            BACKEND / "services" / "memory" / "write_gateway.py"
        ).read_text(encoding="utf-8-sig")
        self.assertGreaterEqual(admin.count('source_kind="admin_action"'), 2)
        self.assertIn("gateway.forget_targets", admin)
        self.assertNotIn("gateway.forget(\n", admin)
        self.assertIn('source_kind="user_message"', runtime)
        self.assertIn("source_kind=source_kind", gateway)

    def test_version_store_commit_only_from_gateway(self):
        offenders: list[str] = []
        for path in _iter_py_files():
            if path.name == "write_gateway.py":
                continue
            src = path.read_text(encoding="utf-8-sig")
            if "store.commit(" in src or ".commit(MemoryVersionCommitCommand" in src:
                offenders.append(_rel(path))
        self.assertEqual(
            offenders, [],
            f"direct VersionStore.commit callers must be removed: {offenders}",
        )

    def test_old_canonicalizer_has_no_write_authority(self):
        allowed = {
            "api/v1/memory.py",  # admin adapter; writes via gateway
            "services/agent_core/shadow.py",  # read-side shadow analysis
            "services/memory/effect_eval.py",  # offline effect evaluation
        }
        offenders: list[str] = []
        for path in _iter_py_files():
            src = path.read_text(encoding="utf-8-sig")
            if "MemoryCanonicalizer().canonicalize(" in src:
                rel = _rel(path)
                if rel not in allowed:
                    offenders.append(rel)
        self.assertEqual(offenders, [], f"canonicalizer write authority leaked: {offenders}")

    def test_provider_rejects_bypass_rows_without_classification(self):
        from app.services.memory.contracts import MemoryEvent
        from app.services.memory.providers.postgres import PostgresMemoryProvider

        user_id = uuid4()
        with self.assertRaises(ValueError):
            PostgresMemoryProvider._assert_commit_ready(
                MemoryEvent(
                    type="entity", subject="pet", value="咪咪",
                    user_id=user_id, source="tool",
                    state_value={"entity": "咪咪", "name": "咪咪"},
                )
            )
        with self.assertRaises(ValueError):
            PostgresMemoryProvider._assert_commit_ready(
                MemoryEvent(
                    type="entity", subject="pet", value="咪咪",
                    domain="relationship", kind="fact",
                    user_id=user_id, source="tool",
                    state_value={"entity": "咪咪", "name": "咪咪"},
                )
            )
        PostgresMemoryProvider._assert_commit_ready(
            MemoryEvent(
                type="entity", subject="pet", value="咪咪",
                domain="relationship", kind="fact", entity_id=uuid4(),
                user_id=user_id, source="tool",
                state_value={"entity": "咪咪", "name": "咪咪", "entity_id": str(uuid4())},
            )
        )


    def test_reading_derived_memory_goes_through_gateway(self):
        src = (BACKEND / "services" / "books" / "reading_service.py").read_text(encoding="utf-8-sig")
        self.assertIn("MemoryWriteGateway", src)
        self.assertNotIn("orchestrator.remember_candidate", src)
        self.assertNotIn("PostgresMemoryProvider", src)

    def test_production_entry_does_not_import_legacy_chain(self):
        src = (BACKEND / "services" / "memory" / "version_runtime.py").read_text(encoding="utf-8-sig")
        self.assertNotIn("write_coordinator", src)
        self.assertNotIn("MemoryCommitter", src)
        self.assertNotIn("provider.remember", src)

    def test_importing_current_runtime_does_not_load_legacy_memory_owners(self):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "import app.services.memory.version_runtime; "
                    "blocked={'app.services.memory.orchestrator',"
                    "'app.services.memory.write_coordinator',"
                    "'app.services.memory.committer'}; "
                    "print(sorted(blocked & set(sys.modules)))"
                ),
            ],
            cwd=BACKEND.parent,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(probe.stdout.strip(), "[]")

    def test_full_production_import_graph_cannot_reach_legacy_baselines(self):
        from scripts.verify_r8_legacy_exit import verify_structure

        result = verify_structure()
        self.assertEqual(result["status"], "passed")
        self.assertGreaterEqual(result["reachable_files"], 1)

    def test_single_gateway_per_domain(self):
        gateways: list[str] = []
        for path in _iter_py_files():
            src = path.read_text(encoding="utf-8-sig")
            if ("class MemoryWriteGateway(" in src or "class MemoryReadGateway(" in src
                    or "class ReadingService(" in src):
                gateways.append(_rel(path))
        expected = {
            "services/memory/write_gateway.py",
            "services/memory/read_gateway.py",
            "services/books/reading_service.py",
        }
        self.assertTrue(set(gateways) <= expected, f"unexpected gateways: {gateways}")

    def test_business_layer_does_not_import_store_or_provider_directly(self):
        allowed_store = {
            "services/memory/__init__.py",
            "services/memory/read_gateway.py",
            "services/memory/write_gateway.py",
        }
        allowed_provider = {
            "services/memory/committer.py",  # deprecated compatibility only
            "services/memory/orchestrator.py",  # deprecated compatibility only
            "services/memory/providers/__init__.py",
            "services/memory/read_gateway.py",
            "services/memory/user_state.py",  # internal record projection
        }
        store_module = "app.services.memory.version_store"
        provider_module = "app.services.memory.providers.postgres"
        store_offenders: list[str] = []
        provider_offenders: list[str] = []
        for path in _iter_py_files():
            rel = _rel(path)
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            if store_module in imported and rel not in allowed_store:
                store_offenders.append(rel)
            if provider_module in imported and rel not in allowed_provider:
                provider_offenders.append(rel)
        self.assertEqual(store_offenders, [], f"direct Store imports: {store_offenders}")
        self.assertEqual(provider_offenders, [], f"direct Provider imports: {provider_offenders}")

    def test_production_surfaces_cannot_import_deprecated_baselines(self):
        production_files = [
            *list((BACKEND / "services" / "agent_core").rglob("*.py")),
            *list((BACKEND / "services" / "external_capabilities").rglob("*.py")),
            *list((BACKEND / "api").rglob("*.py")),
            BACKEND / "services" / "agent_runtime" / "runtime.py",
            BACKEND / "services" / "chat.py",
            BACKEND / "services" / "books" / "reading_service.py",
            BACKEND / "services" / "books" / "recommendation_service.py",
        ]
        forbidden_modules = {
            "app.services.agent_runtime.legacy_compatibility",
            "app.services.agent_runtime.legacy_operations",
            "app.services.memory.write_coordinator",
            "app.services.memory.committer",
            "app.services.planning.compiler",
            "app.services.routing.capabilities",
        }
        forbidden_symbols = {
            "process_memory_write_request",
            "MemoryWriteCoordinator",
            "MemoryCommitter",
        }
        offenders: list[str] = []
        for path in production_files:
            rel = _rel(path)
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
            imports = set()
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module)
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Name):
                    names.add(node.id)
            if imports & forbidden_modules or names & forbidden_symbols:
                offenders.append(rel)
        self.assertEqual(offenders, [], f"deprecated production imports: {offenders}")

    def test_core_business_tables_have_no_raw_sql_writes_in_app(self):
        core_tables = (
            "memory_events",
            "user_book_shelf",
            "recommendation_events",
            "book_interactions",
        )
        pattern = re.compile(
            r"\b(?:insert\s+into|update|delete\s+from)\s+(?:public\.)?(?:"
            + "|".join(core_tables)
            + r")\b",
            re.IGNORECASE,
        )
        offenders: list[str] = []
        for path in _iter_py_files():
            rel = _rel(path)
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if pattern.search(node.value):
                        offenders.append(rel)
                        break
        self.assertEqual(offenders, [], f"raw SQL writes to core tables: {offenders}")

    def test_store_and_provider_do_not_import_semantic_or_llm_layers(self):
        lower_layer_files = [
            BACKEND / "services" / "memory" / "version_store.py",
            BACKEND / "services" / "memory" / "providers" / "postgres.py",
        ]
        forbidden_prefixes = (
            "app.infra.llm",
            "app.services.memory.canonicalizer",
            "app.services.memory.semantic_interpreter",
            "app.services.memory.turn_compiler",
        )
        offenders: list[str] = []
        for path in lower_layer_files:
            rel = _rel(path)
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
            modules = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            if any(
                module.startswith(prefix)
                for module in modules
                for prefix in forbidden_prefixes
            ):
                offenders.append(rel)
        self.assertEqual(offenders, [], f"semantic authority leaked downward: {offenders}")

    def test_bookshelf_read_uses_reading_owner_not_memory_fallback(self):
        runtime = (
            BACKEND / "services" / "books" / "read_runtime.py"
        ).read_text(encoding="utf-8-sig")
        capabilities = (
            BACKEND / "services" / "agent_core" / "capabilities.py"
        ).read_text(encoding="utf-8-sig")
        self.assertIn("ReadingService", runtime)
        self.assertNotIn("MemoryReadGateway", runtime)
        self.assertNotIn("MemoryVersionStore", runtime)
        self.assertIn('"bookshelf_read"', capabilities)
        self.assertIn("Do not use search_memory or ", capabilities)
        self.assertIn("book_search as a fallback", capabilities)
    def test_book_runtime_adapter_uses_single_recommendation_owner(self):
        src = (BACKEND / "services" / "external_capabilities" / "book.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("RecommendationService", src)
        self.assertNotIn("RecommendationProjector", src)
        self.assertNotIn("SearchGateway", src)
if __name__ == "__main__":
    unittest.main()
