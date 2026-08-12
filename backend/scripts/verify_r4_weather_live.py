"""Run one clean-source LongCat weather canary with isolated phases."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(BACKEND_DIR / ".env")

from app.services.agent_core.evidence_artifact import EvidenceArtifactWriter
from app.services.agent_core.evidence_source import GitSourceStateReader
from app.services.external_capabilities.availability import (
    ExternalCapabilityAvailability,
)
from app.services.external_capabilities.canary import (
    CapabilityCanarySpec,
    _failure_code,
)
from app.services.external_capabilities.live_canary_runner import (
    run_configured_capability_canary,
)
from app.services.external_capabilities.runtime import (
    ExternalCapabilityRuntime,
)
from app.services.external_capabilities.weather import WeatherRuntimeAdapter


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--location", default="杭州")
    parser.add_argument("--date", default="tomorrow")
    parser.add_argument("--timeout-seconds", type=float, default=60)
    return parser.parse_args()


async def _main(arguments: argparse.Namespace) -> int:
    writer = EvidenceArtifactWriter()
    writer.require_available(arguments.output)
    source = GitSourceStateReader().require_release_state(
        REPOSITORY_ROOT,
        expected_commit_sha=arguments.commit_sha,
    )
    availability = ExternalCapabilityAvailability(weather_get=True)
    evidence = await run_configured_capability_canary(
        source=source,
        model_id=arguments.model_id,
        timeout_seconds=arguments.timeout_seconds,
        spec=CapabilityCanarySpec(
            capability="weather_get",
            operation="weather_get_v1",
            user_message=(
                f"{arguments.location}{arguments.date}天气怎么样？"
            ),
            arguments={
                "location": arguments.location,
                "date": arguments.date,
                "units": "metric",
                "language": "zh-CN",
            },
            expected_result_mode="weather_evidence",
        ),
        availability=availability,
        external_runtime=ExternalCapabilityRuntime(
            availability=availability,
            adapters=[WeatherRuntimeAdapter()],
        ),
    )
    writer.write_json_new(arguments.output, evidence)
    print(
        json.dumps(
            evidence.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
        )
    )
    return 0 if evidence.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_arguments())))
