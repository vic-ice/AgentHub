"""Compatibility entry point for the canonical Memory Gateway verification.

The retired process_memory_write_request precommit pipeline is intentionally
not executable from this verifier anymore.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts.verify_memory_flow import main


if __name__ == "__main__":
    main()
