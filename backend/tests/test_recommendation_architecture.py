from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


class RecommendationArchitectureTests(unittest.TestCase):
    def test_books_api_uses_domain_owners_not_crud_or_cache(self) -> None:
        path = APP / "api" / "v1" / "books.py"
        imports = _imports(path)
        self.assertIn(
            "app.services.books.recommendation_service",
            imports,
        )
        self.assertIn("app.services.books.reading_service", imports)
        self.assertNotIn("app.crud.book", imports)
        self.assertNotIn("app.services.book_search", imports)

    def test_legacy_book_tool_is_only_an_owner_adapter(self) -> None:
        path = APP / "agents" / "tools" / "books.py"
        source = path.read_text(encoding="utf-8")
        imports = _imports(path)
        self.assertIn(
            "app.services.books.recommendation_service",
            imports,
        )
        self.assertNotIn("app.services.book_search", imports)
        self.assertNotIn("build_personalized_recommendation_constraints", source)
        self.assertNotIn("RecommendationProjector(session)", source)

    def test_only_recommendation_owner_calls_cache_fusion(self) -> None:
        callers = []
        for path in APP.rglob("*.py"):
            if path == APP / "services" / "book_search.py":
                continue
            if "search_and_cache_books_with_status" in path.read_text(
                encoding="utf-8"
            ):
                callers.append(path.relative_to(APP).as_posix())
        self.assertEqual(
            callers,
            ["services/books/recommendation_service.py"],
        )

    def test_current_chat_surface_does_not_import_legacy_baselines(self) -> None:
        production = [
            APP / "main.py",
            APP / "services" / "chat.py",
            APP / "services" / "agent_core" / "chat_entry.py",
            APP / "services" / "agent_core" / "gateway.py",
            APP / "services" / "agent_core" / "harness.py",
            APP / "services" / "agent_runtime" / "runtime.py",
        ]
        forbidden = {
            "app.agents.tools.books",
            "app.services.fast_path",
            "app.services.memory.write_coordinator",
            "app.services.agent_runtime.legacy_compatibility",
            "app.services.routing.funnel",
        }
        violations = {
            path.relative_to(APP).as_posix(): sorted(_imports(path) & forbidden)
            for path in production
            if _imports(path) & forbidden
        }
        self.assertEqual(violations, {})

    def test_production_startup_does_not_initialize_legacy_routing(self) -> None:
        path = APP / "main.py"
        source = path.read_text(encoding="utf-8")
        imports = _imports(path)
        self.assertFalse(
            any(name == "app.services.routing" or name.startswith("app.services.routing.")
                for name in imports)
        )
        self.assertNotIn("schedule_routing_semantic_warmup", source)

    def test_production_book_adapter_enables_owner_internal_enrichment(self) -> None:
        path = APP / "services" / "external_capabilities" / "book.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        owner_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RecommendationService"
        ]
        self.assertEqual(len(owner_calls), 1)
        keyword_values = {
            keyword.arg: keyword.value
            for keyword in owner_calls[0].keywords
            if keyword.arg
        }
        enabled = keyword_values.get("enable_external_enrichment")
        self.assertIsInstance(enabled, ast.Constant)
        self.assertIs(enabled.value, True)

    def test_recommendation_owner_uses_federated_search_for_content_evidence(self) -> None:
        path = APP / "services" / "books" / "recommendation_service.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        requests = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SearchRequest"
        ]
        self.assertEqual(len(requests), 1)
        keywords = {
            keyword.arg: keyword.value
            for keyword in requests[0].keywords
            if keyword.arg
        }
        strategy = keywords.get("strategy")
        self.assertIsInstance(strategy, ast.Constant)
        self.assertEqual(strategy.value, "federated")
        self.assertIn("provider_budget", keywords)

    def test_ordinary_recommendation_has_no_nested_research_controller(self) -> None:
        path = APP / "services" / "books" / "recommendation_service.py"
        source = path.read_text(encoding="utf-8")
        imports = _imports(path)
        forbidden_imports = {
            name
            for name in imports
            if name in {
                "app.services.books.goal_research",
                "app.services.research.query_frontier",
            }
        }
        self.assertEqual(forbidden_imports, set())
        for symbol in (
            "GoalResearch",
            "QueryFrontier",
            "run_goal_research",
        ):
            self.assertNotIn(symbol, source)


if __name__ == "__main__":
    unittest.main()
