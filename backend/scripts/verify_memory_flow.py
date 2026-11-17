"""Compatibility entry point for the canonical Memory Gateway verification.

The former script executed MemoryOrchestrator directly. That write baseline
is retired; keep this filename only for CI/operator compatibility.
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
from scripts.verify_memory_version_chain_flow import _main_async


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-migration", action="store_true")
    args = parser.parse_args()
    if not args.skip_migration:
        _init_postgres()
    evidence = asyncio.run(_main_async())
    print("memory flow compatibility gate passed via MemoryWriteGateway")
    print(evidence)


if __name__ == "__main__":
    main()
