"""Verify the R4 read-only book contract without database or provider I/O."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.agent_core.capabilities import CapabilityRegistry
from app.services.agent_core.compiler import WorkflowCompiler
from app.services.agent_core.contracts import (
    ControllerOutput,
    ControllerToolCall,
)
from app.services.agent_core.proposal_validator import ProposalValidator
from app.services.agent_runtime.contracts import ExecutionContext
from app.services.agent_runtime.runtime import SystemRuntime
from app.services.external_capabilities.availability import (
    ExternalCapabilityAvailability,
)
from app.services.external_capabilities.book import BookSearchRuntimeAdapter
from app.services.external_capabilities.runtime import (
    ExternalCapabilityRuntime,
)
from app.services.external_capabilities.contracts import (
    BookEvidence,
    ExternalEvidenceSource,
)


class _RecommendationServiceFixture:
    async def search(self, request, *, user_id):
        del user_id
        return BookEvidence(
            status="ok",
            query=request.query,
            candidate_count=1,
            sources=[
                ExternalEvidenceSource(
                    title="《天气之书》",
                    url="https://books.example/weather",
                    snippet="作者：示例作者；适合儿童阅读。",
                )
            ],
            metadata={"owner": "RecommendationService"},
        )


async def _main() -> int:
    enabled = ExternalCapabilityAvailability(book_search=True)
    registry = CapabilityRegistry(availability=enabled)
    spec = registry.get("book_search")
    assert spec is not None and spec.side_effect is False
    properties = set(spec.input_model.model_json_schema()["properties"])
    assert properties == {
        "query",
        "mode",
        "limit",
        "response_depth",
        "language",
        "themes",
        "genres",
        "authors",
        "audience",
        "reference_titles",
        "excluded_titles",
        "publication_year_from",
        "publication_year_to",
    }
    schema_properties = spec.input_model.model_json_schema()["properties"]
    assert set(schema_properties["response_depth"]["enum"]) == {
        "quick",
        "balanced",
        "deep",
    }
    for field in ("themes", "reference_titles", "excluded_titles"):
        assert schema_properties[field]["type"] == "array"
    assert properties.isdisjoint(
        {
            "user_id",
            "thread_id",
            "provider",
            "allow_additional_search",
            "cache",
            "book_id",
        }
    )

    output = ControllerOutput(
        mode="capability_proposals",
        tool_calls=[
            ControllerToolCall(
                call_id="books",
                name="book_search",
                arguments={
                    "query": "适合八岁孩子的中文气象科普书",
                    "limit": 5,
                    "language": "zh-CN",
                    "genres": ["科普"],
                    "audience": "8岁儿童",
                },
            )
        ],
    )
    batch = ProposalValidator(registry).validate(output)
    plan = WorkflowCompiler().compile(
        batch,
        goal="推荐适合八岁孩子的中文气象科普书。",
    )
    assert [item.operation for item in plan.actions] == ["book_search_v1"]
    receipt = await SystemRuntime(
        external_runtime=ExternalCapabilityRuntime(
            availability=enabled,
            adapters=[
                BookSearchRuntimeAdapter(
                    recommendation_service=_RecommendationServiceFixture()
                )
            ],
        )
    ).execute(
        plan,
        context=ExecutionContext(
            user_id="00000000-0000-0000-0000-000000000001",
            thread_id="00000000-0000-0000-0000-000000000002",
            request_id="r4-book-fixture",
        ),
    )
    assert receipt.status == "completed"
    serialized = json.dumps(receipt.actions[0].output, ensure_ascii=False)
    for token in (
        "fixture-provider",
        "RAW_FIXTURE_BODY",
        "must-not-write",
    ):
        assert token not in serialized
    print(
        json.dumps(
            {
                "contract": "r4-book-v1",
                "read_only": True,
                "cache_writes": 0,
                "private_runtime_operation": "book_search_v1",
                "receipt_sanitized": True,
                "status": "passed",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

