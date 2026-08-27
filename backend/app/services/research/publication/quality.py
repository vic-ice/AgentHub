"""Independent quality axes for a research answer delivery."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PublicationQualityAxes(BaseModel):
    """Keep research, safety, usefulness and delivery as separate decisions."""

    model_config = ConfigDict(extra="forbid")

    research_sufficient: bool = False
    publication_safe: bool = False
    answer_quality_pass: bool = False
    delivery_succeeded: bool = False
    quality_reasons: list[str] = Field(default_factory=list)


def evaluate_publication_axes(
    *,
    research_sufficient: bool,
    render_ok: bool,
    entity_safe: bool,
    format_ok: bool,
    candidate_quality_ok: bool,
    answer_quality_ok: bool,
    delivered: bool,
    quality_reasons: list[str] | None = None,
) -> PublicationQualityAxes:
    return PublicationQualityAxes(
        research_sufficient=bool(research_sufficient),
        # Publication safety answers only "may this model answer be shown?".
        # Requested formatting, portfolio breadth and semantic richness are
        # quality dimensions: they can trigger repair and warnings, but must
        # not erase a safe answer and replace it with a weaker fallback.
        publication_safe=bool(render_ok and entity_safe),
        answer_quality_pass=bool(
            format_ok and candidate_quality_ok and answer_quality_ok
        ),
        delivery_succeeded=bool(delivered),
        quality_reasons=list(quality_reasons or [])[:12],
    )


__all__ = ["PublicationQualityAxes", "evaluate_publication_axes"]
