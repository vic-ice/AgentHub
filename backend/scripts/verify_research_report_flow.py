"""Compatibility gate for the authoritative Deep Research workflow."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import argparse
import asyncio

from scripts.verify_r4_research_capability import _main


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-migration", action="store_true")
    parser.parse_args()
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
