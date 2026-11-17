from __future__ import annotations

import ast
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
RUNNER_PATH = (
    BACKEND_DIR / "app/services/research/deep_research_runner.py"
)


def _tree() -> ast.Module:
    return ast.parse(
        RUNNER_PATH.read_text(encoding="utf-8"),
        filename=str(RUNNER_PATH),
    )


def _async_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    ]
    if len(functions) != 1:
        raise AssertionError(f"expected one async function {name}, got {len(functions)}")
    return functions[0]


def _called_names(node: ast.AST) -> list[str]:
    return [
        child.func.id
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    ]


class ResearchArchitectureTests(unittest.TestCase):
    def test_deep_research_owner_uses_federated_search(self) -> None:
        search_round = _async_function(_tree(), "_search_round")
        requests = [
            node
            for node in ast.walk(search_round)
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

    def test_real_topic_discovery_precedes_model_candidate_verification(self) -> None:
        runner = _async_function(_tree(), "run_deep_research")
        broad = next(
            node
            for node in ast.walk(runner)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "broad_discovery_query"
                for target in node.targets
            )
        )
        pending = next(
            node
            for node in ast.walk(runner)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "pending_subquestions"
        )
        self.assertIsNotNone(pending.value)
        pending_names = {
            node.id for node in ast.walk(pending.value) if isinstance(node, ast.Name)
        }
        self.assertIn("broad_discovery_query", pending_names)
        self.assertLess(broad.lineno, pending.lineno)

    def test_search_round_reads_bodies_for_book_and_general_research(self) -> None:
        search_round = _async_function(_tree(), "_search_round")
        visit_branches: dict[str, ast.If] = {}
        for node in ast.walk(search_round):
            if not isinstance(node, ast.If):
                continue
            condition_names = {
                child.id
                for child in ast.walk(node.test)
                if isinstance(child, ast.Name)
            }
            if "book_subject_mode" in condition_names:
                visit_branches[ast.unparse(node.test)] = node

        self.assertGreaterEqual(len(visit_branches), 2)
        book_branch = next(
            node
            for condition, node in visit_branches.items()
            if condition == "book_subject_mode"
        )
        general_branch = next(
            node
            for condition, node in visit_branches.items()
            if "not book_subject_mode" in condition
        )
        self.assertIn("fetch_research_source_document", _called_names(book_branch))
        self.assertIn("fetch_research_source_document", _called_names(general_branch))


if __name__ == "__main__":
    unittest.main()
