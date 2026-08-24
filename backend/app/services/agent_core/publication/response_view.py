"""User-facing projection for externally sourced answers.

Evidence receipts decide what may be said.  This module projects those trusted
facts into a presentation-only view model; it never performs search, semantic
interpretation, ranking, or a business write.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.agent_core.publication.contracts import ReceiptEvidenceBundle
from app.services.external_capabilities.contracts import BookEvidence, WebEvidence


class AnswerSourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    summary: str = ""
    published_date: str = ""
    authors: list[str] = Field(default_factory=list)
    theme: str = ""
    cover_url: str = ""
    supporting_sources: list["AnswerCitationView"] = Field(default_factory=list)


class AnswerCitationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    summary: str = ""


class AnswerCoverageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: str
    candidate_count: int = 0
    status: Literal["complete", "partial", "missing"] = "missing"


class ExternalAnswerView(BaseModel):
    """Facts approved for presentation, with no internal receipt prose."""

    model_config = ConfigDict(extra="forbid")

    answer_type: Literal["book_recommendation", "book_lookup", "web_lookup", "mixed"]
    status: Literal["ready", "empty", "unavailable"]
    books: list[AnswerSourceView] = Field(default_factory=list)
    web: list[AnswerSourceView] = Field(default_factory=list)
    response_depth: Literal["quick", "balanced", "deep"] = "balanced"
    coverage: list[AnswerCoverageView] = Field(default_factory=list)
    receipt_refs: list[str] = Field(default_factory=list)


def project_external_answer_view(
    evidence: Sequence[ReceiptEvidenceBundle],
) -> ExternalAnswerView | None:
    books: list[AnswerSourceView] = []
    web: list[AnswerSourceView] = []
    refs: list[str] = []
    seen: set[tuple[str, str]] = set()
    book_modes: list[str] = []
    statuses: list[str] = []
    recognized = False
    recognized_books = False
    response_depth: Literal["quick", "balanced", "deep"] = "balanced"
    coverage: list[AnswerCoverageView] = []

    for bundle in evidence:
        for action in bundle.receipt.actions:
            if action.status != "completed" or not isinstance(action.output, dict):
                continue
            model = {
                "book_search_v1": BookEvidence,
                "web_search_v2": WebEvidence,
            }.get(action.operation)
            if model is None:
                continue
            try:
                payload = model.model_validate(
                    {
                        key: value
                        for key, value in action.output.items()
                        if key in model.model_fields
                    }
                )
            except Exception:
                continue
            recognized = True
            refs.append(action.action_id)
            statuses.append(payload.status)
            if isinstance(payload, BookEvidence):
                recognized_books = True
                book_modes.append(str(payload.metadata.get("mode") or "recommendation"))
                response_depth = payload.response_depth
                coverage = [
                    AnswerCoverageView.model_validate(item.model_dump(mode="json"))
                    for item in payload.coverage
                ]
                target = books
                if payload.items:
                    for item in payload.items:
                        url = item.catalog_url.strip()
                        if not url and item.evidence_sources:
                            url = item.evidence_sources[0].url.strip()
                        title = item.title.strip()
                        key = (url.casefold(), title.casefold())
                        if not url or key in seen:
                            continue
                        seen.add(key)
                        target.append(
                            AnswerSourceView(
                                title=title,
                                url=url,
                                summary=" ".join(item.summary.split()).strip(),
                                published_date=item.published_date.strip(),
                                authors=list(item.authors),
                                theme=item.theme.strip(),
                                cover_url=item.cover_url.strip(),
                                supporting_sources=[
                                    AnswerCitationView(
                                        title=source.title.strip() or source.url,
                                        url=source.url.strip(),
                                        summary=" ".join(source.snippet.split()).strip(),
                                    )
                                    for source in item.evidence_sources
                                    if source.url.strip()
                                ],
                            )
                        )
                    continue
            else:
                target = web
            for source in payload.sources:
                url = source.url.strip()
                title = source.title.strip() or url
                key = (url.casefold(), title.casefold())
                if not url or key in seen:
                    continue
                seen.add(key)
                target.append(
                    AnswerSourceView(
                        title=title,
                        url=url,
                        summary=" ".join(source.snippet.split()).strip(),
                        published_date=source.published_date.strip(),
                    )
                )

    if not recognized or not refs:
        return None
    if books and web:
        answer_type = "mixed"
    elif books or recognized_books:
        answer_type = (
            "book_lookup" if book_modes and all(mode == "lookup" for mode in book_modes)
            else "book_recommendation"
        )
    else:
        answer_type = "web_lookup"
    if books or web:
        status = "ready"
    elif statuses and all(item == "empty_result" for item in statuses):
        status = "empty"
    else:
        status = "unavailable"
    return ExternalAnswerView(
        answer_type=answer_type,
        status=status,
        books=books[:10],
        web=web[:10],
        response_depth=response_depth,
        coverage=coverage,
        receipt_refs=list(dict.fromkeys(refs)),
    )


__all__ = [
    "AnswerSourceView",
    "AnswerCitationView",
    "AnswerCoverageView",
    "ExternalAnswerView",
    "project_external_answer_view",
]
