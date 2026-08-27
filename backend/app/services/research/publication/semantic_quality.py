"""Independent semantic acceptance review for a user-facing research answer."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.research.publication.packet import ResearchPublicationPacket


SEMANTIC_QUALITY_TIMEOUT_SECONDS = 30


class SemanticQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool = False
    passed: bool = False
    directness: int = Field(default=0, ge=0, le=5)
    user_need_coverage: int = Field(default=0, ge=0, le=5)
    evidence_use: int = Field(default=0, ge=0, le=5)
    synthesis_depth: int = Field(default=0, ge=0, le=5)
    decision_usefulness: int = Field(default=0, ge=0, le=5)
    missing_user_needs: list[str] = Field(default_factory=list)
    unsupported_or_overstated: list[str] = Field(default_factory=list)
    repair_instructions: list[str] = Field(default_factory=list)
    provider: str = "unavailable"
    error: str = ""

    @field_validator(
        "missing_user_needs",
        "unsupported_or_overstated",
        "repair_instructions",
        mode="before",
    )
    @classmethod
    def clean_lists(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned = [" ".join(str(item or "").split()).strip() for item in value]
        return list(dict.fromkeys(item[:240] for item in cleaned if item))[:8]


async def evaluate_semantic_answer(
    *,
    model_id: str,
    objective: str,
    packet: ResearchPublicationPacket,
    answer: str,
) -> SemanticQualityResult:
    """Judge semantic fidelity and usefulness, never prose style alone."""

    if not model_id or not packet.evidence_strategy.facets:
        return SemanticQualityResult(error="semantic strategy unavailable")
    from app.infra.llm import get_llm

    schema = {
        "passed": True,
        "directness": 1,
        "user_need_coverage": 1,
        "evidence_use": 1,
        "synthesis_depth": 1,
        "decision_usefulness": 1,
        "missing_user_needs": [],
        "unsupported_or_overstated": [],
        "repair_instructions": [],
    }
    payload = {
        "original_user_request": objective,
        "user_answer_brief": packet.user_answer_brief.model_dump(mode="json"),
        "evidence_strategy": packet.evidence_strategy.model_dump(mode="json"),
        "candidate_dossiers": [
            item.model_dump(mode="json") for item in packet.candidate_dossiers
        ],
        "cross_cutting_facets": [
            item.model_dump(mode="json") for item in packet.cross_cutting_facets
        ],
        "evidence_index": [
            item.model_dump(mode="json") for item in packet.evidence_index
        ],
        "draft_answer": answer,
    }
    prompt = (
        "You are the independent semantic acceptance reviewer for a final "
        "research answer. Return one JSON object only. Compare the original "
        "user request, its model-authored evidence strategy, the complete "
        "semantic dossier, and the draft. Judge whether the draft actually "
        "answers the user's decision; preserves the important supported "
        "information; uses different evidence roles for their intended "
        "purpose; explains implications and trade-offs; and is deep enough "
        "for the requested answer_depth. Do not reward headings, tables, "
        "length, or citation count by themselves. Do not require every field "
        "to be repeated. Penalize an evidence digest with no synthesis, a "
        "thin answer that ignores supported facets, unsupported certainty, "
        "or generic advice that the dossier did not establish. For recommendation "
        "tasks, the candidate portfolio is a model-owned decision thesis, not a "
        "web-search allowlist: require the draft to meaningfully cover at least "
        "target_candidate_count candidates when that target is nonzero. Do not "
        "penalize a calibrated recommendation merely because its dossier has sparse "
        "external evidence; do penalize precise or changing factual claims that lack "
        "support. The answer should make evidence coverage affect confidence and "
        "detail, not erase useful choice diversity. Set passed=true "
        "only when all five scores are at least 4 and there is no material "
        "unsupported claim. Repair instructions must name the missing meaning "
        "to add or the overstatement to remove, never prescribe a fixed layout.\n\n"
        f"JSON schema: {json.dumps(schema, ensure_ascii=False)}\n\n"
        f"Review input: {json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        model = get_llm(model_id, thinking_mode=False)
        if hasattr(model, "bind"):
            model = model.bind(max_tokens=900)
        async with asyncio.timeout(SEMANTIC_QUALITY_TIMEOUT_SECONDS):
            response = await model.ainvoke(prompt)
        text = _message_text(response)
        data = _first_json_object(text)
        if data is None:
            raise ValueError("semantic reviewer returned no JSON object")
        result = SemanticQualityResult.model_validate(
            {
                **data,
                "available": True,
                "provider": "runtime_llm",
            }
        )
        computed_pass = (
            all(
                score >= 4
                for score in (
                    result.directness,
                    result.user_need_coverage,
                    result.evidence_use,
                    result.synthesis_depth,
                    result.decision_usefulness,
                )
            )
            and not result.unsupported_or_overstated
        )
        return result.model_copy(update={"passed": bool(result.passed and computed_pass)})
    except Exception as exc:
        return SemanticQualityResult(error=(str(exc) or exc.__class__.__name__)[:300])


def _message_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, dict)
            else str(item)
            for item in content
        )
    return str(content or "")


def _first_json_object(text: str) -> dict[str, Any] | None:
    source = str(text or "")
    for start in range(len(source)):
        if source[start] != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(source)):
            char = source[end]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(source[start : end + 1])
                    except json.JSONDecodeError:
                        break
                    return value if isinstance(value, dict) else None
    return None


__all__ = ["SemanticQualityResult", "evaluate_semantic_answer"]
