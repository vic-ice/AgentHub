"""Semantic dossier projected from admitted evidence for final publication."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.services.evidence_strategy import EvidenceStrategy
from app.services.user_answer_contracts import UserAnswerBrief


class PublicationEvidence(BaseModel):
    """One publishable fact and its citation; no research-runtime state."""

    model_config = ConfigDict(extra="forbid")

    citation_id: int
    claim: str
    source_title: str
    source_url: str
    confidence: str = "unknown"
    published_date: str = ""
    candidate_title: str = ""
    evidence_facets: list[str] = Field(default_factory=list)
    source_roles: list[str] = Field(default_factory=list)


class EvidenceFacetDossier(BaseModel):
    """Evidence coverage for one model-selected question."""

    model_config = ConfigDict(extra="forbid")

    name: str
    purpose: str = ""
    citation_ids: list[int] = Field(default_factory=list)


class CandidateEvidenceDossier(BaseModel):
    """A model recommendation thesis plus its external evidence coverage."""

    model_config = ConfigDict(extra="forbid")

    candidate_title: str
    portfolio_role: str = ""
    model_rationale: str = ""
    expected_fit: str = ""
    model_tradeoffs: list[str] = Field(default_factory=list)
    external_evidence_status: str = "not_found"
    facets: list[EvidenceFacetDossier] = Field(default_factory=list)
    all_citation_ids: list[int] = Field(default_factory=list)
    uncovered_required_facets: list[str] = Field(default_factory=list)


class ResearchPublicationPacket(BaseModel):
    """Complete publication input organized around user decisions."""

    model_config = ConfigDict(extra="forbid")

    user_answer_brief: UserAnswerBrief
    evidence_strategy: EvidenceStrategy = Field(default_factory=EvidenceStrategy)
    candidate_dossiers: list[CandidateEvidenceDossier] = Field(default_factory=list)
    cross_cutting_facets: list[EvidenceFacetDossier] = Field(default_factory=list)
    evidence_index: list[PublicationEvidence] = Field(default_factory=list)
    allowed_recommendation_entities: list[str] = Field(default_factory=list)
    externally_verified_entities: list[str] = Field(default_factory=list)
    target_candidate_count: int = 0
    recommendation_epistemic_policy: str = (
        "Candidate selection and comparative judgment may use the model's stable "
        "domain knowledge. External sources verify changing or precise facts and "
        "adjust confidence; missing external evidence is a disclosed gap, not an "
        "automatic reason to delete a model-planned candidate."
    )
    user_relevant_limitations: list[str] = Field(default_factory=list)
    material_conflicts: list[str] = Field(default_factory=list)

    @property
    def evidence(self) -> list[PublicationEvidence]:
        """Compatibility accessor for callers migrating from the flat packet."""

        return self.evidence_index


def build_research_publication_packet(
    *,
    brief: UserAnswerBrief,
    evidence: list[dict[str, Any]],
    evidence_strategy: EvidenceStrategy | dict[str, Any] | None = None,
    allowed_recommendation_entities: list[str] | None = None,
    externally_verified_entities: list[str] | None = None,
    candidate_portfolio: list[Any] | None = None,
    target_candidate_count: int = 0,
    user_relevant_limitations: list[str] | None = None,
    material_conflicts: list[str] | None = None,
) -> ResearchPublicationPacket:
    """Build a loss-aware dossier while leaving synthesis to the writer model."""

    strategy = (
        evidence_strategy
        if isinstance(evidence_strategy, EvidenceStrategy)
        else EvidenceStrategy.model_validate(evidence_strategy or {})
    )
    allowed = list(
        dict.fromkeys(
            " ".join(str(item or "").split()).strip()
            for item in (allowed_recommendation_entities or [])
            if " ".join(str(item or "").split()).strip()
        )
    )[:20]
    verified = list(
        dict.fromkeys(
            " ".join(str(item or "").split()).strip()
            for item in (externally_verified_entities or [])
            if " ".join(str(item or "").split()).strip()
        )
    )[:20]
    profiles: dict[str, dict[str, Any]] = {}
    for raw in candidate_portfolio or []:
        value = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else raw
        if not isinstance(value, dict):
            continue
        title = " ".join(str(value.get("title") or "").split()).strip()
        if title:
            profiles[_key(title)] = value
    purpose_by_facet = {facet.name: facet.purpose for facet in strategy.facets}
    required_facets = {
        facet.name
        for facet in strategy.facets
        if facet.required_for_recommendation
    }

    projected: list[PublicationEvidence] = []
    for item in evidence:
        claim = " ".join(str(item.get("claim") or "").split()).strip()
        source_url = str(item.get("source_url") or "").strip()
        if not claim or not source_url:
            continue
        candidate_title = _candidate_for_evidence(item, allowed=allowed)
        raw_facets = item.get("evidence_facets") or [
            item.get("evidence_facet")
        ]
        facets = list(
            dict.fromkeys(
                " ".join(str(value or "").split()).strip()[:80]
                for value in raw_facets
                if " ".join(str(value or "").split()).strip()
            )
        )
        projected.append(
            PublicationEvidence(
                citation_id=len(projected) + 1,
                claim=claim[:600],
                source_title=" ".join(
                    str(item.get("source_title") or source_url).split()
                )[:200],
                source_url=source_url,
                confidence=str(
                    item.get("quality")
                    or item.get("evidence_status")
                    or "unknown"
                )[:40],
                published_date=str(item.get("published_date") or "")[:40],
                candidate_title=candidate_title,
                evidence_facets=facets,
                source_roles=list(
                    dict.fromkeys(
                        " ".join(str(value or "").split()).strip()[:100]
                        for value in (item.get("source_roles") or [])
                        if " ".join(str(value or "").split()).strip()
                    )
                )[:8],
            )
        )

    candidate_dossiers = [
        _candidate_dossier(
            title,
            evidence=projected,
            purpose_by_facet=purpose_by_facet,
            required_facets=required_facets,
            profile=profiles.get(_key(title), {}),
            externally_verified=any(_key(title) == _key(item) for item in verified),
        )
        for title in allowed
    ]
    cross_cutting = _facet_dossiers(
        [item for item in projected if not item.candidate_title],
        purpose_by_facet=purpose_by_facet,
    )
    return ResearchPublicationPacket(
        user_answer_brief=brief,
        evidence_strategy=strategy,
        candidate_dossiers=candidate_dossiers,
        cross_cutting_facets=cross_cutting,
        evidence_index=projected,
        allowed_recommendation_entities=allowed,
        externally_verified_entities=verified,
        target_candidate_count=min(max(0, int(target_candidate_count or 0)), len(allowed)),
        user_relevant_limitations=_clean_list(
            user_relevant_limitations, limit=8
        ),
        material_conflicts=_clean_list(material_conflicts, limit=8),
    )


def _candidate_dossier(
    title: str,
    *,
    evidence: list[PublicationEvidence],
    purpose_by_facet: dict[str, str],
    required_facets: set[str],
    profile: dict[str, Any],
    externally_verified: bool,
) -> CandidateEvidenceDossier:
    key = _key(title)
    matched = [item for item in evidence if _key(item.candidate_title) == key]
    facets = _facet_dossiers(matched, purpose_by_facet=purpose_by_facet)
    covered = {facet.name for facet in facets if facet.citation_ids}
    return CandidateEvidenceDossier(
        candidate_title=title,
        portfolio_role=_clean_text(profile.get("portfolio_role"), 300),
        model_rationale=_clean_text(profile.get("rationale"), 500),
        expected_fit=_clean_text(profile.get("expected_fit"), 300),
        model_tradeoffs=_clean_list(profile.get("tradeoffs"), limit=4),
        external_evidence_status=(
            "verified" if externally_verified else "partial" if matched else "not_found"
        ),
        facets=facets,
        all_citation_ids=[item.citation_id for item in matched],
        uncovered_required_facets=sorted(required_facets - covered),
    )


def _facet_dossiers(
    evidence: list[PublicationEvidence],
    *,
    purpose_by_facet: dict[str, str],
) -> list[EvidenceFacetDossier]:
    grouped: dict[str, list[int]] = {}
    for item in evidence:
        facets = item.evidence_facets or ["general_support"]
        for facet in facets:
            grouped.setdefault(facet, []).append(item.citation_id)
    return [
        EvidenceFacetDossier(
            name=name,
            purpose=purpose_by_facet.get(name, ""),
            citation_ids=list(dict.fromkeys(citations)),
        )
        for name, citations in grouped.items()
    ]


def _candidate_for_evidence(
    item: dict[str, Any],
    *,
    allowed: list[str],
) -> str:
    explicit = " ".join(str(item.get("candidate_title") or "").split()).strip()
    if explicit and any(_key(explicit) == _key(title) for title in allowed):
        return next(title for title in allowed if _key(explicit) == _key(title))
    haystack = _key(
        f"{item.get('source_title') or ''} {item.get('claim') or ''}"
    )
    matches = [title for title in allowed if _key(title) and _key(title) in haystack]
    return matches[0] if len(matches) == 1 else ""


def _key(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())


def _clean_list(values: list[str] | None, *, limit: int) -> list[str]:
    return list(
        dict.fromkeys(
            " ".join(str(item or "").split()).strip()[:240]
            for item in (values or [])
            if " ".join(str(item or "").split()).strip()
        )
    )[:limit]


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


__all__ = [
    "CandidateEvidenceDossier",
    "EvidenceFacetDossier",
    "PublicationEvidence",
    "ResearchPublicationPacket",
    "build_research_publication_packet",
]
