from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.memory.effect_eval import (  # noqa: E402
    MemoryEffectEvaluator,
    PRODUCTION_STRATEGIES,
    load_memory_effect_cases,
)


DEFAULT_FIXTURE = (
    BACKEND_DIR / "tests" / "fixtures" / "memory_effect_eval_v1.jsonl"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="Path to the memory effect JSONL fixture.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full JSON report.",
    )
    args = parser.parse_args()

    dataset_version, cases = load_memory_effect_cases(args.fixture)
    report = MemoryEffectEvaluator().run(
        cases,
        dataset_version=dataset_version,
    )

    print(f"memory effect eval: {report.dataset_version}")
    print(f"cases={report.case_count}")
    print("summaries:")
    for summary in report.summaries:
        print(
            "  "
            f"{summary.dimension}/{summary.strategy}: "
            f"{summary.passed}/{summary.total} "
            f"weighted={summary.weighted_score}/{summary.weighted_total} "
            f"rate={summary.pass_rate:.3f}"
        )
    print("winners:")
    for dimension, strategy in sorted(report.winners.items()):
        print(f"  {dimension}: {strategy}")
    print("quality gates:")
    for gate in report.quality_gates:
        status = "passed" if gate.passed else "failed"
        detail = "" if gate.passed else " " + ",".join(gate.failures)
        print(f"  {gate.gate}: {status}{detail}")
    if report.failure_families:
        print("failure families:")
        for family in report.failure_families:
            print(
                "  "
                f"{family.strategy}/{family.family}: "
                f"{family.total} "
                + ",".join(family.case_ids)
            )

    failed_production = [
        result
        for result in report.results
        if result.strategy in PRODUCTION_STRATEGIES and not result.passed
    ]
    if failed_production:
        print("production failures:")
        for result in failed_production:
            print(
                "  "
                f"{result.dimension}/{result.strategy}/{result.case_id}: "
                + ",".join(result.failure_codes)
            )
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    if not report.production_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
