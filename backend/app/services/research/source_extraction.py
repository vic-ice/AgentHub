from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.services.research.contracts import (
    EVIDENCE_QUALITIES,
    EVIDENCE_SOURCE_TYPES,
    normalize_research_token,
    normalize_text,
)
from app.services.research.evidence_quality import (
    EvidenceQualityAssessment,
    assess_evidence_candidate,
    classify_source,
    source_quality,
)
from app.services.research.source_acquisition import (
    ResearchObservationBatch,
    ResearchSourceRecord,
    build_research_observation_batch,
)


RESEARCH_SOURCE_EXTRACTION_CONTRACT_VERSION = "research-source-extraction-v1"


class RejectedResearchSourceDocument(BaseModel):
    source_type: str = "web"
    source_title: str = ""
    source_url: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchSourceExtractionResult(BaseModel):
    result_mode: str = "research_source_extraction"
    contract_version: str = RESEARCH_SOURCE_EXTRACTION_CONTRACT_VERSION
    status: str = "ok"
    query: str = ""
    subquestion: str = ""
    source_records: list[ResearchSourceRecord] = Field(default_factory=list)
    observation_batch: ResearchObservationBatch
    rejected_documents: list[RejectedResearchSourceDocument] = Field(default_factory=list)
    document_count: int = 0
    extracted_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


def extract_research_source_records(
    *,
    query: str = "",
    subquestion: str = "",
    documents: list[dict[str, Any]] | None = None,
    provider_source: str = "source_extraction",
    max_records_per_document: int = 1,
    metadata: dict[str, Any] | None = None,
) -> ResearchSourceExtractionResult:
    """Extract explicit source-sentence claims from approved source documents.

    This adapter does not search, fetch URLs, call providers, or write state. It
    only maps already-returned source/search text into app-owned source records
    when a claim can be copied from the source text.
    """

    normalized_query = normalize_text(query)
    normalized_subquestion = normalize_text(subquestion)
    normalized_provider = normalize_text(provider_source) or "source_extraction"
    source_documents = documents or []
    max_records = max(1, min(int(max_records_per_document or 1), 5))
    source_records: list[ResearchSourceRecord] = []
    rejected_documents: list[RejectedResearchSourceDocument] = []

    for index, item in enumerate(source_documents):
        document = _source_document(item)
        rejection_reasons = _document_rejection_reasons(document)
        if rejection_reasons:
            rejected_documents.append(
                _rejected_document(
                    document,
                    reason_codes=rejection_reasons,
                    provider_source=normalized_provider,
                )
            )
            continue

        booklist_records = (
            []
            if re.search(r"/subject/\d+/?", document["source_url"])
            else _booklist_records(
                document,
                query=normalized_query,
                subquestion=normalized_subquestion,
                provider_source=normalized_provider,
                index=index,
            )
        )
        if booklist_records:
            source_records.extend(booklist_records)
            continue

        explicit_assessment = (
            assess_evidence_candidate(
                claim=document["claim"],
                query=f"{normalized_query} {normalized_subquestion}".strip(),
                source_title=document["source_title"],
                source_url=document["source_url"],
                published_date=document["published_date"],
            )
            if document["claim"]
            else None
        )
        explicit_record = _explicit_record(
            document,
            query=normalized_query,
            subquestion=normalized_subquestion,
            provider_source=normalized_provider,
            index=index,
            assessment=explicit_assessment,
        )
        if explicit_record is not None:
            source_records.append(explicit_record)
            continue

        structured_record = _structured_book_record(
            document,
            query=normalized_query,
            subquestion=normalized_subquestion,
            provider_source=normalized_provider,
            index=index,
        )
        if structured_record is not None:
            source_records.append(structured_record)

        candidate_content = (
            _book_content_regions(document["content"])
            if structured_record is not None
            else document["content"]
        )
        candidates = _select_claim_sentences(
            content=candidate_content,
            query=normalized_query,
            subquestion=normalized_subquestion,
            source_title=document["source_title"],
            source_url=document["source_url"],
            published_date=document["published_date"],
            limit=max_records,
        )
        if not candidates:
            if structured_record is not None:
                continue
            fallback_assessment = explicit_assessment or assess_evidence_candidate(
                claim=document["content"],
                query=f"{normalized_query} {normalized_subquestion}".strip(),
                source_title=document["source_title"],
                source_url=document["source_url"],
                published_date=document["published_date"],
            )
            reason_codes = (
                fallback_assessment.reason_codes
                or ["no_publishable_claim_sentence"]
            )
            rejected_documents.append(
                _rejected_document(
                    document,
                    reason_codes=reason_codes,
                    provider_source=normalized_provider,
                )
            )
            continue

        for sentence_index, sentence, assessment in candidates:
            source_records.append(
                ResearchSourceRecord(
                    source_type=document["source_type"],
                    source_title=document["source_title"],
                    source_url=document["source_url"],
                    claim=sentence,
                    excerpt=sentence,
                    quality=document["quality"],
                    relevance=document["relevance"],
                    metadata={
                        **document["metadata"],
                        "provider_source": normalized_provider,
                        "provider_raw": document["raw_payload"],
                        "source_extraction": {
                            "contract_version": (
                                RESEARCH_SOURCE_EXTRACTION_CONTRACT_VERSION
                            ),
                            "method": "source_sentence_overlap",
                            "query": normalized_query,
                            "subquestion": normalized_subquestion,
                            "document_index": index,
                            "sentence_index": sentence_index,
                            "claim_is_source_sentence": True,
                        },
                        "evidence_quality": assessment.model_dump(mode="json"),
                    },
                )
            )

    batch = build_research_observation_batch(
        query=normalized_query,
        subquestion=normalized_subquestion,
        sources=[record.model_dump(mode="json") for record in source_records],
        provider_source=normalized_provider,
        metadata={
            **(metadata or {}),
            "source_extraction": {
                "contract_version": RESEARCH_SOURCE_EXTRACTION_CONTRACT_VERSION,
            },
        },
    )
    return ResearchSourceExtractionResult(
        status=_status(source_records, rejected_documents),
        query=normalized_query,
        subquestion=normalized_subquestion,
        source_records=source_records,
        observation_batch=batch,
        rejected_documents=rejected_documents,
        document_count=len(source_documents),
        extracted_count=len(source_records),
        metadata={
            **(metadata or {}),
            "writes_research_state": False,
            "writes_evidence": False,
            "writes_long_term_memory": False,
            "writes_recommendation_events": False,
            "external_call": False,
            "provider_source": normalized_provider,
            "rejected_document_count": len(rejected_documents),
        },
    )


def _source_document(item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item or {})
    metadata = dict(payload.get("metadata") or {})
    source_type = _source_type(payload.get("source_type") or metadata.get("source_type"))
    from app.services.research.text_cleaner import clean_source_title

    source_title = clean_source_title(
        payload.get("source_title")
        or payload.get("title")
        or payload.get("name")
        or metadata.get("source_title")
    )
    source_url = normalize_text(
        payload.get("source_url")
        or payload.get("url")
        or payload.get("href")
        or metadata.get("source_url")
    )
    from app.services.research.text_cleaner import clean_text_for_context

    raw_content = (
        payload.get("content")
        or payload.get("raw_content")
        or payload.get("text")
        or payload.get("body")
        or payload.get("snippet")
        or payload.get("summary")
        or payload.get("excerpt")
    )
    # SourceVisit already converted the fetched page to plain text. Running
    # that single long line through the generic line-boilerplate cleaner can
    # drop the entire document merely because it begins with "登录". Preserve
    # the visited body and let the extraction/quality gates select facts.
    if metadata.get("source_visit"):
        content = " ".join(normalize_text(raw_content).split())[:24_000]
    else:
        content = clean_text_for_context(raw_content)
    published_date = normalize_text(
        payload.get("published_date")
        or payload.get("published_at")
        or payload.get("date")
        or metadata.get("published_date")
    )
    if published_date:
        metadata["published_date"] = published_date
    return {
        "source_type": source_type,
        "source_title": source_title,
        "source_url": source_url,
        "content": content,
        "claim": normalize_text(payload.get("claim") or metadata.get("claim")),
        "excerpt": normalize_text(payload.get("excerpt") or metadata.get("excerpt")),
        "published_date": published_date,
        "quality": _resolved_quality(
            payload.get("quality") or metadata.get("quality"),
            source_url,
        ),
        "relevance": _relevance(payload.get("relevance") or payload.get("score")),
        "metadata": metadata,
        "raw_payload": payload,
    }


def _document_rejection_reasons(document: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not document["source_title"] and not document["source_url"]:
        reasons.append("missing_source_metadata")
    if not document["content"] and not document["claim"]:
        reasons.append("missing_source_content")
    return reasons


def _explicit_record(
    document: dict[str, Any],
    *,
    query: str,
    subquestion: str,
    provider_source: str,
    index: int,
    assessment: EvidenceQualityAssessment | None,
) -> ResearchSourceRecord | None:
    if not document["claim"] or assessment is None or not assessment.publishable:
        return None
    excerpt = document["excerpt"] or document["claim"]
    return ResearchSourceRecord(
        source_type=document["source_type"],
        source_title=document["source_title"],
        source_url=document["source_url"],
        claim=document["claim"],
        excerpt=excerpt,
        quality=document["quality"],
        relevance=document["relevance"],
        metadata={
            **document["metadata"],
            "provider_source": provider_source,
            "provider_raw": document["raw_payload"],
            "source_extraction": {
                "contract_version": RESEARCH_SOURCE_EXTRACTION_CONTRACT_VERSION,
                "method": "provided_claim",
                "query": query,
                "subquestion": subquestion,
                "document_index": index,
                "claim_is_source_sentence": bool(document["content"])
                and document["claim"] in document["content"],
            },
            "evidence_quality": assessment.model_dump(mode="json"),
        },
    )


def _select_claim_sentences(
    *,
    content: str,
    query: str,
    subquestion: str,
    source_title: str,
    source_url: str,
    published_date: str,
    limit: int,
) -> list[tuple[int, str, EvidenceQualityAssessment]]:
    sentences = _split_sentences(content)
    terms = _query_terms(f"{query} {subquestion}")
    scored: list[tuple[int, int, str, EvidenceQualityAssessment]] = []
    for index, sentence in enumerate(sentences):
        candidate = _clean_claim_text(sentence)
        if (
            not candidate
            or _is_source_boilerplate(candidate)
            or _has_unresolved_anaphora(candidate)
            or not _claim_looks_complete(candidate)
        ):
            continue
        assessment = assess_evidence_candidate(
            claim=candidate,
            query=f"{query} {subquestion}".strip(),
            source_title=source_title,
            source_url=source_url,
            published_date=published_date,
        )
        if not assessment.publishable:
            continue
        score = _sentence_score(candidate, terms)
        if terms and score <= 0:
            continue
        scored.append((score, -index, candidate, assessment))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected: list[tuple[int, str, EvidenceQualityAssessment]] = []
    for _score, negative_index, sentence, assessment in scored[:limit]:
        selected.append((-negative_index, sentence, assessment))
    selected.sort(key=lambda item: item[0])
    return selected


def _booklist_records(
    document: dict[str, Any],
    *,
    query: str,
    subquestion: str,
    provider_source: str,
    index: int,
    limit: int = 12,
) -> list[ResearchSourceRecord]:
    """Split a booklist/editorial page into one record per book.

    Pages like "10???????????" contain multiple \u300a\u4e66\u540d\u300b
    entries with author and description; each book becomes its own claim so the
    final writer can present tiers instead of a single sentence fragment.
    """

    content = str(document.get("content") or "")
    titles = re.findall(r"\u300a([^\u300b]{2,80})\u300b", content)
    if len(titles) < 3:
        return []
    records: list[ResearchSourceRecord] = []
    seen_claims: set[str] = set()
    for title in titles:
        title_clean = normalize_text(title)
        if not title_clean:
            continue
        if any(record.claim.startswith("\u300a" + title_clean + "\u300b") for record in records):
            continue
        claim = _clean_claim_text(_book_context(content, title_clean))
        if not claim:
            # List membership is itself a source-backed recommendation fact.
            # This neutral projection does not invent an author, topic or
            # rating when the page exposes only a title.
            claim = f"\u300a{title_clean}\u300b\u88ab\u8be5\u6765\u6e90\u5217\u5165\u63a8\u8350\u4e66\u5355\u3002"
        if len(claim) < 12 or claim in seen_claims:
            continue
        if not claim.startswith("\u300a"):
            continue
        seen_claims.add(claim)
        assessment = assess_evidence_candidate(
            claim=claim,
            query=f"{query} {subquestion}".strip(),
            source_title=document["source_title"],
            source_url=document["source_url"],
            published_date=document["published_date"],
        )
        if not assessment.publishable:
            continue
        records.append(
            ResearchSourceRecord(
                source_type=document["source_type"],
                source_title=document["source_title"],
                source_url=document["source_url"],
                claim=claim,
                excerpt=claim,
                quality=document["quality"],
                relevance=document["relevance"],
                metadata={
                    **document["metadata"],
                    "provider_source": provider_source,
                    "provider_raw": document["raw_payload"],
                    "source_extraction": {
                        "contract_version": RESEARCH_SOURCE_EXTRACTION_CONTRACT_VERSION,
                        "method": "booklist_entries",
                        "query": query,
                        "subquestion": subquestion,
                        "document_index": index,
                        "claim_is_source_sentence": False,
                    },
                    "evidence_quality": assessment.model_dump(mode="json"),
                },
            )
        )
        if len(records) >= limit:
            break
    return records


def _book_context(content: str, title: str) -> str:
    """Compose one atomic book entry: title + first description sentence + author."""

    marker = "\u300a" + title + "\u300b"
    start = content.find(marker)
    if start < 0:
        return marker
    tail = content[start + len(marker):]
    next_marker = tail.find("\u300a")
    segment = tail if next_marker < 0 else tail[:next_marker]
    segment = _clean_claim_text(segment)
    segment = re.sub(
        r"^(?:\u8fd4\u56de\u6982\u89c8\s*)?\d+\s*/\s*\d+\s*",
        "",
        segment,
    )
    sentences = _split_sentences(segment)
    parts = []
    for sentence in sentences:
        candidate = normalize_text(sentence).strip(" \uff1a:")
        if len(candidate) < 12:
            continue
        if re.fullmatch(
            r"(?:\u4f5c\u8005|\u8bd1\u8005|\u51fa\u7248\u793e)[:\uff1a].+",
            candidate,
        ):
            continue
        parts.append(candidate[:220])
        break
    author = re.search(
        r"\u4f5c\u8005[:\uff1a]\s*([^\u3002\uff01\uff1f!?\uff1b;]{2,120})",
        segment,
    )
    if author:
        parts.append("\u4f5c\u8005\uff1a" + normalize_text(author.group(1)).strip())
    body = "\u3002".join(part for part in parts if part)[:280]
    return marker + "\uff1a" + body if body else ""


def _structured_book_record(
    document: dict[str, Any],
    *,
    query: str,
    subquestion: str,
    provider_source: str,
    index: int,
) -> ResearchSourceRecord | None:
    if classify_source(document["source_url"]) != "review":
        return None
    content = document["content"]
    title = re.sub(
        r"\s*[\(（]豆瓣[\)）]\s*$",
        "",
        document["source_title"],
    ).strip()
    rating = _first_group(r"豆瓣评分\s*([0-9]+(?:\.[0-9]+)?)", content)
    author = _first_group(
        r"作者\s*[:：]\s*(.{1,100}?)(?=译者\s*[:：]|出版社\s*[:：]|出品方\s*[:：]|出版年\s*[:：]|ISBN\s*[:：]|$)",
        content,
    )
    publisher = _first_group(
        r"出版社\s*[:：]\s*(.{1,80}?)(?=出品方\s*[:：]|出版年\s*[:：]|ISBN\s*[:：]|页数\s*[:：]|$)",
        content,
    )
    published = _first_group(
        r"出版年\s*[:：]\s*([0-9]{4}(?:[-./][0-9]{1,2}){0,2})",
        content,
    )
    if not title or not any((author, publisher, published, rating)):
        return None

    parts = [f"《{_bounded_field(title, 80)}》"]
    if author:
        parts.append(f"作者为{_bounded_field(author, 80)}")
    if publisher:
        parts.append(f"由{_bounded_field(publisher, 60)}出版")
    if published:
        parts.append(f"出版时间为{published}")
    if rating:
        parts.append(f"豆瓣页面显示评分为{rating}")
    claim = "，".join(parts) + "。"
    assessment = assess_evidence_candidate(
        claim=claim,
        query=f"{query} {subquestion}".strip(),
        source_title=document["source_title"],
        source_url=document["source_url"],
        published_date=document["published_date"],
    )
    if not assessment.publishable:
        return None
    return ResearchSourceRecord(
        source_type=document["source_type"],
        source_title=document["source_title"],
        source_url=document["source_url"],
        claim=claim,
        excerpt=claim,
        quality=document["quality"],
        relevance=document["relevance"],
        metadata={
            **document["metadata"],
            "provider_source": provider_source,
            "provider_raw": document["raw_payload"],
            "published_date": document["published_date"] or published,
            "source_extraction": {
                "contract_version": RESEARCH_SOURCE_EXTRACTION_CONTRACT_VERSION,
                "method": "structured_book_page",
                "query": query,
                "subquestion": subquestion,
                "document_index": index,
                "claim_is_source_sentence": False,
            },
            "evidence_quality": assessment.model_dump(mode="json"),
        },
    )


def _book_content_regions(content: str) -> str:
    """Keep book-owned synopsis/excerpt regions, excluding reviews and chrome."""

    regions: list[str] = []
    for start_marker, end_markers in (
        ("内容简介", ("作者简介", "目录", "原文摘录", "喜欢读")),
        ("原文摘录", ("喜欢读", "短评", "书评", "论坛")),
    ):
        start = content.find(start_marker)
        if start < 0:
            continue
        start += len(start_marker)
        tail = content[start:]
        ends = [tail.find(marker) for marker in end_markers]
        ends = [index for index in ends if index >= 0]
        region = tail[: min(ends)] if ends else tail
        region = normalize_text(region).strip(" ·")
        if region:
            regions.append(region[:8_000])
    return "\n".join(regions)


def _first_group(pattern: str, content: str) -> str:
    match = re.search(pattern, content, flags=re.IGNORECASE)
    return normalize_text(match.group(1)) if match is not None else ""


def _bounded_field(value: str, limit: int) -> str:
    normalized = normalize_text(value)
    return normalized if len(normalized) <= limit else normalized[:limit].rstrip()


_NESTED_IMAGE_LINK_RE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_MARKERS_RE = re.compile(r"\*\*|__|`|\*")
_LIST_LEADER_RE = re.compile(r"^\s*(?:[-*\u2022\u00b7]|\d+[.\u3001)\uff09])\s*")
_ORPHAN_CLOSER_RE = re.compile(r"[\]\)\}]")
_IMAGE_TOKEN_RE = re.compile(r"\b(?:png|jpg|jpeg|webp|gif|img)\b", re.IGNORECASE)
_PAGE_META_RE = re.compile(r"\d{1,2}\s+\d{2}:\d{2}|\u6d4f\u89c8\u91cf[:\uff1a]\d+")
_HEADING_MARKER_RE = re.compile(r"#{1,6}\s*")
_PIPE_OR_HTML_TOKEN_RE = re.compile(r"\||\b(?:htm|html)\b", re.IGNORECASE)
_PUNCT_RUN_RE = re.compile(r"[\u3002\uff01\uff1f!?\uff1b;]{2,}")


def _clean_claim_text(text: str) -> str:
    """Strip markdown/source artifacts from one candidate claim."""

    cleaned = _NESTED_IMAGE_LINK_RE.sub(" ", text)
    for _round in range(2):
        cleaned = _MARKDOWN_IMAGE_RE.sub(" ", cleaned)
        cleaned = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"\]\([^)]*\)", " ", cleaned)
    cleaned = _MARKDOWN_MARKERS_RE.sub("", cleaned)
    cleaned = _ORPHAN_CLOSER_RE.sub(" ", cleaned)
    cleaned = _IMAGE_TOKEN_RE.sub(" ", cleaned)
    cleaned = _PAGE_META_RE.sub(" ", cleaned)
    cleaned = _HEADING_MARKER_RE.sub(" ", cleaned)
    cleaned = _PIPE_OR_HTML_TOKEN_RE.sub(" ", cleaned)
    cleaned = _LIST_LEADER_RE.sub("", cleaned)
    cleaned = re.sub(r"^\s*\u7b80\u4ecb[:\uff1a]\s*", "", cleaned)
    cleaned = normalize_text(cleaned)
    return _PUNCT_RUN_RE.sub(lambda match: match.group(0)[0], cleaned)


def _claim_looks_complete(text: str) -> bool:
    """Skip fragments that are neither complete sentences nor book titles."""

    if not text:
        return False
    if text[-1] in "\u3002\uff01\uff1f!?.":
        return True
    return "\u300a" in text and "\u300b" in text and len(text) >= 15


def _is_source_boilerplate(text: str) -> bool:
    normalized = normalize_text(text).casefold()
    return bool(
        re.search(r"(?:©|copyright|all rights reserved|版权所有)", normalized)
        or re.search(r"\bdouban\.com\b", normalized)
        or re.search(r"(?:京icp|公网安备|网站备案)", normalized)
        or re.search(r"(?:feed\s*:\s*rss|订阅关于.{0,80}评论)", normalized)
        or re.search(r"(?:扫码直接下载|全新发布|登录\s*/\s*注册|豆瓣读书\s+搜索)", normalized)
    )


def _has_unresolved_anaphora(text: str) -> bool:
    """Reject person-pronoun fragments whose antecedent is outside the claim."""

    normalized = normalize_text(text).lstrip(" >-—–")
    return bool(re.match(r"^(?:他|她|他们|她们|其)(?:与|和|曾|在|是|将|把|为|以|的)", normalized))


def _split_sentences(content: str) -> list[str]:
    chunks = re.findall(r"[^.!?。！？；;\n]+[.!?。！？；;]?", content)
    return [normalize_text(chunk) for chunk in chunks if normalize_text(chunk)]


def _query_terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = {term for term in re.findall(r"[a-z0-9]{3,}", lowered)}
    for segment in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(segment) <= 4:
            if len(segment) >= 2:
                terms.add(segment)
            continue
        terms.update(
            segment[index : index + 2]
            for index in range(len(segment) - 1)
        )
    return terms


def _sentence_score(sentence: str, terms: set[str]) -> int:
    lowered = sentence.lower()
    return sum(1 for term in terms if term in lowered)


def _rejected_document(
    document: dict[str, Any],
    *,
    reason_codes: list[str],
    provider_source: str,
) -> RejectedResearchSourceDocument:
    return RejectedResearchSourceDocument(
        source_type=document["source_type"],
        source_title=document["source_title"],
        source_url=document["source_url"],
        reason_codes=reason_codes,
        metadata={
            "provider_source": provider_source,
            "provider_raw": document["raw_payload"],
        },
    )


def _source_type(value: Any) -> str:
    token = normalize_research_token(value or "web")
    return token if token in EVIDENCE_SOURCE_TYPES else "other"


def _quality(value: Any) -> str:
    token = normalize_research_token(value or "unknown")
    return token if token in EVIDENCE_QUALITIES else "unknown"


def _resolved_quality(value: Any, source_url: str) -> str:
    source_class = classify_source(source_url)
    derived = source_quality(source_class)
    if source_class in {"marketplace", "social"}:
        return derived
    supplied = _quality(value)
    return supplied if supplied != "unknown" else derived


def _relevance(value: Any) -> int:
    try:
        numeric = float(value if value is not None else 3)
    except (TypeError, ValueError):
        numeric = 3
    if numeric <= 1:
        numeric *= 5
    return max(1, min(5, round(numeric)))


def _status(
    source_records: list[ResearchSourceRecord],
    rejected_documents: list[RejectedResearchSourceDocument],
) -> str:
    if source_records and rejected_documents:
        return "partial"
    if source_records:
        return "ok"
    return "empty_result"
