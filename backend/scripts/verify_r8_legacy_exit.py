"""Verify R8 quarantine boundaries and optional final legacy exit."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.infra.config import Settings, get_settings


ENTRY_FILES = (
    "app/main.py",
    "app/services/chat.py",
    "app/services/streaming.py",
    "app/services/agent_core/chat_entry.py",
    "app/services/agent_core/gateway.py",
    "app/services/agent_core/compiler.py",
    "app/services/agent_core/turn_loop.py",
    "app/services/agent_runtime/runtime.py",
)
FORBIDDEN_IMPORTS = (
    "app.services.agent_core.legacy_chat_runtime",
    "app.services.agent_runtime.coordinator",
    "app.services.agent_runtime.planner",
    "app.services.agent_runtime.legacy_admission",
    "app.services.agent_runtime.legacy_availability",
    "app.services.agent_runtime.legacy_compatibility",
    "app.services.agent_runtime.legacy_operations",
    "app.services.fast_path",
    "app.services.routing.decision_maker",
    "app.services.routing.funnel",
    "app.services.memory.orchestrator",
)
FORBIDDEN_RUNTIME_CONSTANTS = {
    "process_memory_write_request",
    "recall_recent_conversation",
    "search_memory",
}


def verify_structure() -> dict[str, object]:
    scanned_imports = 0
    reachable_files: set[Path] = set()
    pending = [BACKEND_ROOT / relative for relative in ENTRY_FILES]
    reachable_forbidden: dict[str, list[str]] = {}
    while pending:
        path = pending.pop()
        path = path.resolve()
        if path in reachable_files or not path.exists():
            continue
        reachable_files.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        imports = tuple(_top_level_imports(tree))
        scanned_imports += len(imports)
        violations = [
            name
            for name in imports
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in FORBIDDEN_IMPORTS
            )
        ]
        if violations:
            reachable_forbidden[str(path.relative_to(BACKEND_ROOT))] = violations
        for name in imports:
            imported_path = _local_module_path(name)
            if imported_path is not None and imported_path not in reachable_files:
                pending.append(imported_path)

    if reachable_forbidden:
        raise AssertionError(
            "production transitive import graph reaches retired authority: "
            + json.dumps(reachable_forbidden, ensure_ascii=False, sort_keys=True)
        )

    for relative in ENTRY_FILES:
        path = BACKEND_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        imports = tuple(_imports(tree))
        violations = [
            name
            for name in imports
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in FORBIDDEN_IMPORTS
            )
        ]
        if violations:
            raise AssertionError(f"{relative} imports retired authority: {violations}")

    chat = _read("app/services/chat.py")
    streaming = _read("app/services/streaming.py")
    for name, source in (("chat", chat), ("streaming", streaming)):
        for retired in (
            "LegacyChatRuntimeBridge",
            "prepare_legacy_runtime_turn",
            "prepare_runtime_turn",
        ):
            if retired in source:
                raise AssertionError(
                    f"{name} still references retired runtime token {retired}"
                )
    if "AGENT_STREAM_V1" in streaming:
        raise AssertionError("Live stream still has a legacy path selector")

    startup = _read("app/main.py")
    for retired in ("init_agent", "get_agent", "preload_templates"):
        if retired in startup:
            raise AssertionError(
                f"application startup still references {retired}"
            )

    runtime_path = BACKEND_ROOT / "app/services/agent_runtime/runtime.py"
    runtime_tree = ast.parse(
        runtime_path.read_text(encoding="utf-8"),
        filename=str(runtime_path),
    )
    runtime_constants = {
        node.value
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    forbidden_constants = sorted(FORBIDDEN_RUNTIME_CONSTANTS & runtime_constants)
    if forbidden_constants:
        raise AssertionError(
            "SystemRuntime still owns retired operations: "
            + ",".join(forbidden_constants)
        )
    runtime_source = runtime_path.read_text(encoding="utf-8")
    for token in (
        "RuntimeCompatibilityPort",
        "DisabledRuntimeCompatibility",
        "runtime_operation_not_registered",
    ):
        if token not in runtime_source:
            raise AssertionError(f"SystemRuntime boundary missing {token}")

    if (
        BACKEND_ROOT / "app/services/agent_core/legacy_chat_runtime.py"
    ).exists():
        raise AssertionError("legacy runtime bridge module still exists")
    compatibility_source = _read(
        "app/services/agent_runtime/legacy_compatibility.py"
    )
    if (
        "_ALLOWED_OPERATIONS" not in compatibility_source
        or "legacy_operation_not_registered" not in compatibility_source
    ):
        raise AssertionError("legacy compatibility is not operation-allowlisted")

    fields = Settings.model_fields
    if fields["AGENT_LEGACY_RUNTIME_FALLBACK"].default is not False:
        raise AssertionError("legacy runtime fallback must default off")
    return {
        "status": "passed",
        "entry_files": len(ENTRY_FILES),
        "reachable_files": len(reachable_files),
        "imports_scanned": scanned_imports,
        "system_runtime_retired_operations": 0,
        "bridge_count": 0,
        "release_gate_credit": False,
    }


def verify_release_exit() -> dict[str, object]:
    settings = get_settings()
    blockers = []
    if settings.AGENT_LEGACY_RUNTIME_FALLBACK:
        blockers.append("AGENT_LEGACY_RUNTIME_FALLBACK")
    if settings.AGENT_LEGACY_MEMORY_WRITE_COMPAT:
        blockers.append("AGENT_LEGACY_MEMORY_WRITE_COMPAT")
    if settings.AGENT_LEGACY_HISTORY_READ_FALLBACK:
        blockers.append("AGENT_LEGACY_HISTORY_READ_FALLBACK")
    if blockers:
        raise RuntimeError("legacy_exit_blocked:" + ",".join(blockers))
    return {
        "status": "passed",
        "legacy_flags": "off",
        "release_gate_credit": False,
    }


def _read(relative: str) -> str:
    return (BACKEND_ROOT / relative).read_text(encoding="utf-8")


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield from _import_names(node)


def _top_level_imports(tree: ast.AST):
    """Yield imports executed during module initialization.

    Imports inside functions are adapters invoked only by an explicit caller;
    treating them as eager edges would incorrectly make quarantined legacy
    compatibility code production-reachable.
    """

    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.names: list[str] = []

        def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
            self.names.extend(_import_names(node))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
            self.names.extend(_import_names(node))
            if node.module:
                for alias in node.names:
                    candidate = f"{node.module}.{alias.name}"
                    if alias.name != "*" and _local_module_path(candidate) is not None:
                        self.names.append(candidate)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            return

        def visit_AsyncFunctionDef(  # noqa: N802
            self, node: ast.AsyncFunctionDef
        ) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            return

    collector = Collector()
    collector.visit(tree)
    yield from collector.names


def _local_module_path(module: str) -> Path | None:
    if module != "app" and not module.startswith("app."):
        return None
    relative = Path(*module.split("."))
    module_file = BACKEND_ROOT / relative.with_suffix(".py")
    if module_file.exists():
        return module_file.resolve()
    package_file = BACKEND_ROOT / relative / "__init__.py"
    if package_file.exists():
        return package_file.resolve()
    return None


def _import_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    return [module]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-exited", action="store_true")
    args = parser.parse_args()
    result = verify_structure()
    if args.require_exited:
        result["release_exit"] = verify_release_exit()
    else:
        result["release_exit"] = {
            "status": "blocked",
            "reason": "online_and_observation_gates_pending",
            "release_gate_credit": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
