"""Clarification Gate — blocks ambiguous writes before Admission / Conflict / Postgres.

The gate consumes the LLM-derived facts and their MemoryCandidate projections.
It only blocks when there is a real ambiguity signal:
- the LLM semantic interpreter asked for confirmation;
- the model did not resolve a concrete object/state (domain=general is allowed);
- a preference/state/feedback fact lacks a concrete object/state;
- a semantic extraction has very low confidence.

While blocked, nothing is persisted; the chat layer surfaces the question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.memory.contracts import MemoryCandidate
from app.services.memory.write_contracts import ResolvedMemoryFact

_BROAD_VALUES = frozenset(
    {
        "喜欢",
        "不喜欢",
        "讨厌",
        "想要",
        "想看",
        "想读",
        "感兴趣",
        "不感兴趣",
        "like",
        "dislike",
        "want",
        "avoid",
        "read",
        "ok",
        "好",
        "是的",
        "对",
    }
)


@dataclass
class ClarificationGateResult:
    blocked: bool
    clarification_question: str = ""
    reason_codes: list[str] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)


class ClarificationGate:
    """Fail-closed clarity check before any durable-memory write."""

    async def evaluate(
        self,
        facts: list[ResolvedMemoryFact],
        candidates: list[MemoryCandidate],
    ) -> ClarificationGateResult:
        if not facts or not candidates:
            return ClarificationGateResult(blocked=False)

        annotations: list[dict[str, Any]] = []
        for index, fact in enumerate(facts):
            candidate = (
                candidates[index] if index < len(candidates) else candidates[-1]
            )
            domain = candidate.domain or "general"
            kind = candidate.kind or "fact"
            annotations.append(
                {
                    "fact_index": index,
                    "domain": domain,
                    "kind": kind,
                    "value": fact.legacy_value,
                    "category": fact.category,
                    "state_key": fact.state_key,
                    "extraction_confidence": fact.extraction_confidence,
                }
            )

            user_state = candidate.metadata.get("user_state") or {}
            llm_question = str(user_state.get("confirmation_question") or "").strip()
            if llm_question:
                return ClarificationGateResult(
                    blocked=True,
                    clarification_question=llm_question,
                    reason_codes=["clarification_gate_llm_confirmation"],
                    annotations=annotations,
                )

            state_value = fact.state_value or {}
            if (
                kind == "fact"
                and isinstance(state_value, dict)
                and "entity" in state_value
                and not str(state_value.get("entity_type") or "").strip()
            ):
                return ClarificationGateResult(
                    blocked=True,
                    clarification_question=(
                        f"你提到的“{candidate.value}”是书、人、宠物、物品、"
                        "地点、账号还是项目？请明确对象类型。"
                    ),
                    reason_codes=["clarification_gate_entity_type_missing"],
                    annotations=annotations,
                )



            if kind in {"preference", "state", "feedback"} and not fact.state_value:
                if _is_broad(fact.legacy_value):
                    return ClarificationGateResult(
                        blocked=True,
                        clarification_question=(
                            f"“{candidate.value}”还缺少具体对象或状态，"
                            "请补充完整（例如：喜欢/在看/已读哪本书、"
                            "和谁的什么约定）。"
                        ),
                        reason_codes=["clarification_gate_missing_object"],
                        annotations=annotations,
                    )

            if (
                fact.extraction_method == "semantic"
                and fact.extraction_confidence < 0.5
            ):
                return ClarificationGateResult(
                    blocked=True,
                    clarification_question=(
                        "这条信息我还不能确定，请再明确一下你希望我记住的内容。"
                    ),
                    reason_codes=["clarification_gate_low_confidence"],
                    annotations=annotations,
                )

        return ClarificationGateResult(blocked=False, annotations=annotations)


def _is_broad(value: str) -> bool:
    token = str(value or "").strip().lower()
    if not token or len(token) < 2:
        return True
    return token in _BROAD_VALUES


__all__ = ["ClarificationGate", "ClarificationGateResult"]
