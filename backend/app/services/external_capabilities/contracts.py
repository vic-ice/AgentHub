from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.services.evidence_strategy import EvidenceStrategy


class ExternalCapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeatherGetInput(ExternalCapabilityInput):
    location: str = Field(min_length=1, max_length=120)
    date: str = Field(
        default="today",
        min_length=1,
        max_length=32,
        description="today, tomorrow, or an ISO calendar date",
    )
    units: Literal["metric", "imperial"] = "metric"
    language: str = Field(default="zh-CN", max_length=24)

    @field_validator("location", "date", "language", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return " ".join(str(value or "").split())


class WebSearchInput(ExternalCapabilityInput):
    query: str = Field(min_length=1, max_length=300)
    max_results: int = Field(default=5, ge=1, le=10)
    detail: Literal["standard", "deep"] = "standard"
    time_range: Literal["day", "week", "month", "year"] | None = None
    include_domains: list[str] = Field(default_factory=list, max_length=20)
    exclude_domains: list[str] = Field(default_factory=list, max_length=20)
    language: str = Field(default="", max_length=24)
    category: Literal["general", "news"] = "general"

    @field_validator("query", "language", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return " ".join(str(value or "").split())

    @field_validator("include_domains", "exclude_domains", mode="before")
    @classmethod
    def normalize_domains(cls, value: Any) -> list[str]:
        values = value.split(",") if isinstance(value, str) else value or []
        cleaned = [
            str(item or "")
            .strip()
            .lower()
            .removeprefix("https://")
            .removeprefix("http://")
            .split("/", 1)[0]
            for item in values
        ]
        return list(dict.fromkeys(item for item in cleaned if item))


class BookSearchInput(ExternalCapabilityInput):
    query: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "Required external-catalog discovery or lookup query; this is not "
            "a query for the user's own Shelf."
        ),
    )
    mode: Literal["recommendation", "lookup"] = Field(
        default="recommendation",
        description=(
            "recommendation discovers new books; lookup retrieves a specifically requested book"
        ),
    )
    limit: int = Field(default=5, ge=1, le=10)
    response_depth: Literal["quick", "balanced", "deep"] = Field(
        default="balanced",
        description=(
            "Presentation depth understood in the same semantic pass. Use deep "
            "when the user explicitly asks for a thorough comparison, detailed "
            "reasons, or an in-depth answer; balanced is the normal default."
        ),
    )
    language: str = Field(default="", max_length=24)
    themes: list[str] = Field(
        default_factory=list,
        max_length=6,
        description=(
            "Only subjects or moods explicitly stated by the user. Never infer "
            "themes from comparison titles, and leave this empty when the user "
            "only supplied example books."
        ),
    )
    theme_match: Literal["any", "all"] = Field(
        default="any",
        description=(
            "How admitted candidates must relate to themes. Use all only when "
            "the user requires every returned book to cover every theme; use "
            "any for alternatives, broad discovery, or balanced theme coverage."
        ),
    )
    genres: list[str] = Field(default_factory=list, max_length=10)
    authors: list[str] = Field(default_factory=list, max_length=10)
    audience: str = Field(
        default="",
        max_length=120,
        description=(
            "Target reader explicitly stated by the user, such as an age, life "
            "stage or profession. Leave empty when no audience was stated."
        ),
    )
    reference_titles: list[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Books supplied by the user as comparison anchors. They shape "
            "discovery but are never recommendation candidates."
        ),
    )
    candidate_titles: list[str] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "Model-proposed recommendation leads to verify against public book "
            "pages. Never include reference_titles or excluded_titles here."
        ),
    )
    evidence_strategy: EvidenceStrategy | None = Field(
        default=None,
        description=(
            "Model-authored evidence portfolio for this particular decision. "
            "Choose facets and source roles from the user's real goal; do not "
            "apply a fixed template based on genre."
        ),
    )
    excluded_titles: list[str] = Field(
        default_factory=list,
        max_length=40,
        description=(
            "Books that must not be returned as new recommendations. "
            "Reference titles are automatically included here."
        ),
    )
    publication_year_from: int | None = Field(
        default=None,
        ge=1000,
        le=2200,
    )
    publication_year_to: int | None = Field(
        default=None,
        ge=1000,
        le=2200,
    )

    @field_validator("query", "language", "audience", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return " ".join(str(value or "").split())

    @field_validator("evidence_strategy", mode="before")
    @classmethod
    def normalize_evidence_strategy(cls, value: Any) -> Any:
        # Some OpenAI-compatible providers serialize nested tool arguments as
        # a JSON string even though the advertised schema is an object.
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return value
            return parsed
        return value

    @field_validator(
        "genres",
        "themes",
        "authors",
        "reference_titles",
        "candidate_titles",
        "excluded_titles",
        mode="before",
    )
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value] if value else []
        cleaned = [" ".join(str(item or "").split()) for item in values]
        return list(dict.fromkeys(item for item in cleaned if item))

    @model_validator(mode="after")
    def validate_year_range(self) -> "BookSearchInput":
        if (
            self.publication_year_from is not None
            and self.publication_year_to is not None
            and self.publication_year_from > self.publication_year_to
        ):
            raise ValueError("publication year range is reversed")
        references = list(dict.fromkeys(self.reference_titles))
        exclusions = list(
            dict.fromkeys([*references, *self.excluded_titles])
        )[:40]
        self.reference_titles = references
        self.excluded_titles = exclusions
        self.candidate_titles = [
            title
            for title in self.candidate_titles
            if title not in exclusions
        ][:10]
        return self


class ResearchStartInput(ExternalCapabilityInput):
    objective: str = Field(min_length=1, max_length=2_000)
    mode: Literal["deep_search", "deep_research"] = "deep_search"
    subquestions: list[str] = Field(default_factory=list, max_length=8)
    constraints: list[str] = Field(default_factory=list, max_length=10)
    max_sources: int = Field(default=8, ge=3, le=20)
    max_rounds: int = Field(default=2, ge=1, le=3)
    time_range: Literal["day", "week", "month", "year"] | None = None
    language: str = Field(default="", max_length=24)

    @field_validator("objective", "language", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return " ".join(str(value or "").split())

    @field_validator("subquestions", "constraints", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value] if value else []
        cleaned = [" ".join(str(item or "").split()) for item in values]
        return list(dict.fromkeys(item for item in cleaned if item))


class ExternalEvidenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=300)
    url: str = Field(min_length=1, max_length=2_000)
    snippet: str = Field(default="", max_length=1_000)
    published_date: str = Field(default="", max_length=64)
    evidence_facet: str = Field(default="", max_length=80)
    source_role: str = Field(default="", max_length=100)


class BookEvidenceFacet(BaseModel):
    """Evidence for one model-selected decision facet of a candidate."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(default="", max_length=240)
    sources: list[ExternalEvidenceSource] = Field(default_factory=list, max_length=6)


class BookThemeCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: str = Field(min_length=1, max_length=120)
    candidate_count: int = Field(default=0, ge=0, le=10)
    status: Literal["complete", "partial", "missing"] = "missing"


class BookRecommendationItem(BaseModel):
    """One recommendation candidate enriched with source-backed context."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    authors: list[str] = Field(default_factory=list, max_length=10)
    theme: str = Field(default="", max_length=120)
    summary: str = Field(default="", max_length=2_000)
    catalog_url: str = Field(default="", max_length=2_000)
    cover_url: str = Field(default="", max_length=2_000)
    published_date: str = Field(default="", max_length=64)
    evidence_sources: list[ExternalEvidenceSource] = Field(
        default_factory=list,
        max_length=18,
    )
    evidence_facets: list[BookEvidenceFacet] = Field(default_factory=list, max_length=8)
    evidence_provider_count: int = Field(default=0, ge=0, le=3)


class WeatherEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_mode: Literal["weather_evidence"] = "weather_evidence"
    status: Literal["ok", "empty_result", "unavailable"]
    location: str
    date: str
    units: Literal["metric", "imperial"]
    sources: list[ExternalEvidenceSource] = Field(
        default_factory=list,
        max_length=3,
    )
    error: str = Field(default="", max_length=120)


class WebEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_mode: Literal["web_evidence"] = "web_evidence"
    status: Literal["ok", "empty_result", "unavailable"]
    query: str
    sources: list[ExternalEvidenceSource] = Field(
        default_factory=list,
        max_length=10,
    )
    error: str = Field(default="", max_length=120)


class BookEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_mode: Literal["book_evidence"] = "book_evidence"
    status: Literal["ok", "empty_result", "unavailable"]
    query: str
    sources: list[ExternalEvidenceSource] = Field(
        default_factory=list,
        max_length=10,
    )
    error: str = Field(default="", max_length=120)
    candidate_count: int = Field(default=0, ge=0, le=10)
    response_depth: Literal["quick", "balanced", "deep"] = "balanced"
    theme_match: Literal["any", "all"] = "any"
    items: list[BookRecommendationItem] = Field(
        default_factory=list,
        max_length=10,
    )
    coverage: list[BookThemeCoverage] = Field(
        default_factory=list,
        max_length=6,
    )
    filters_applied: list[str] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=10)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchReportEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_mode: Literal[
        "research_report_evidence"
    ] = "research_report_evidence"
    status: Literal["ok", "empty_result", "unavailable"]
    objective: str
    findings: list[str] = Field(default_factory=list, max_length=20)
    sources: list[ExternalEvidenceSource] = Field(
        default_factory=list,
        max_length=20,
    )
    limitations: list[str] = Field(default_factory=list, max_length=10)
    error: str = Field(default="", max_length=120)


__all__ = [
    "BookEvidence",
    "BookEvidenceFacet",
    "BookRecommendationItem",
    "BookSearchInput",
    "BookThemeCoverage",
    "ExternalCapabilityInput",
    "ExternalEvidenceSource",
    "ResearchReportEvidence",
    "ResearchStartInput",
    "WebEvidence",
    "WebSearchInput",
    "WeatherEvidence",
    "WeatherGetInput",
]
