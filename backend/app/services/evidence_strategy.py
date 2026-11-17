"""Model-authored evidence strategy shared by Search and Deep Research.

The schema intentionally describes evidence roles instead of subject domains.
The model decides which roles matter for the user's decision; deterministic
code only validates, executes, groups and preserves them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class EvidenceFacet(BaseModel):
    """One question the evidence portfolio must answer."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(default="", max_length=240)
    query_terms: list[str] = Field(default_factory=list, max_length=8)
    preferred_source_types: list[str] = Field(default_factory=list, max_length=8)
    importance: Literal["high", "medium", "low"] = "medium"
    candidate_specific: bool = True
    required_for_recommendation: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        aliases = {
            "facet": "name",
            "title": "name",
            "description": "purpose",
            "question": "purpose",
            "keywords": "query_terms",
            "queries": "query_terms",
            "source_types": "preferred_source_types",
            "source_roles": "preferred_source_types",
            "candidate_level": "candidate_specific",
            "required": "required_for_recommendation",
        }
        for source, target in aliases.items():
            if target not in normalized and source in normalized:
                normalized[target] = normalized[source]
        return normalized

    @field_validator("name", "purpose", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return " ".join(str(value or "").split()).strip()

    @field_validator("query_terms", "preferred_source_types", mode="before")
    @classmethod
    def clean_lists(cls, value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value] if value else []
        cleaned = [" ".join(str(item or "").split()).strip() for item in values]
        return list(dict.fromkeys(item[:100] for item in cleaned if item))[:8]

    @field_validator("importance", mode="before")
    @classmethod
    def normalize_importance(cls, value: Any) -> str:
        text = str(value or "").strip().casefold()
        if text in {"high", "critical", "required", "primary", "1"}:
            return "high"
        if text in {"low", "optional", "tertiary", "3"}:
            return "low"
        return "medium"


class EvidenceStrategy(BaseModel):
    """A model-selected portfolio tailored to the actual user decision."""

    model_config = ConfigDict(extra="ignore")

    rationale: str = Field(default="", max_length=400)
    facets: list[EvidenceFacet] = Field(default_factory=list, max_length=8)
    comparison_questions: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def normalize_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "facets" not in normalized:
            normalized["facets"] = (
                normalized.get("evidence_facets")
                or normalized.get("requirements")
                or normalized.get("evidence_requirements")
                or []
            )
        if "comparison_questions" not in normalized:
            normalized["comparison_questions"] = (
                normalized.get("questions")
                or normalized.get("comparison_dimensions")
                or []
            )
        return normalized

    @field_validator("rationale", mode="before")
    @classmethod
    def clean_rationale(cls, value: Any) -> str:
        return " ".join(str(value or "").split()).strip()

    @field_validator("comparison_questions", mode="before")
    @classmethod
    def clean_questions(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned = [" ".join(str(item or "").split()).strip() for item in value]
        return list(dict.fromkeys(item[:240] for item in cleaned if item))[:8]

    def active_facets(self, *, limit: int = 4) -> list[EvidenceFacet]:
        rank = {"high": 0, "medium": 1, "low": 2}
        return sorted(
            self.facets,
            key=lambda item: (
                rank.get(item.importance, 1),
                not item.required_for_recommendation,
            ),
        )[: max(1, limit)]


__all__ = ["EvidenceFacet", "EvidenceStrategy"]
