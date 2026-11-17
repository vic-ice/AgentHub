"""Broad compatibility gate for the current authoritative architecture.

This filename used to execute the retired LangGraph/MemoryOrchestrator
baseline. It now verifies only the production Agent Core, Memory Gateway,
RecommendationService adapter and Deep Research workflow.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import argparse
import asyncio

from scripts.init_database import _init_postgres
from scripts.verify_agent_core_contracts import _run as verify_agent_core
from scripts.verify_memory_version_chain_flow import _main_async as verify_memory
from scripts.verify_r4_book_capability import _main as verify_book
from scripts.verify_r4_research_capability import _main as verify_research


async def _main() -> None:
    await verify_agent_core()
    await verify_memory()
    assert await verify_book() == 0
    assert await verify_research() == 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-migration", action="store_true")
    args = parser.parse_args()
    if not args.skip_migration:
        _init_postgres()
    asyncio.run(_main())
    print("authoritative architecture acceptance flow passed")


if __name__ == "__main__":
    main()
