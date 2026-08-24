from __future__ import annotations

import re
from collections.abc import Sequence

from app.services.agent_core.contracts import PublishedAnswer
from app.services.agent_core.publication.contracts import ReceiptEvidenceBundle
from app.services.agent_core.publication.response_view import (
    AnswerSourceView,
    ExternalAnswerView,
    project_external_answer_view,
)
from app.services.books.book_identity import normalize_book_work_title
from app.services.external_capabilities.contracts import BookEvidence


def render_external_evidence_fallback(
    evidence: Sequence[ReceiptEvidenceBundle],
) -> PublishedAnswer | None:
    """Publish admitted external evidence when model synthesis is unavailable.

    This renderer never invents recommendations or reinterprets the request. It
    only exposes deduplicated titles, snippets and URLs already admitted by the
    typed capability receipts.
    """

    view = project_external_answer_view(evidence)
    if view is None:
        return None
    return PublishedAnswer(
        status="completed",
        content=_render_external_answer_view(view),
        receipt_backed=True,
        receipt_refs=view.receipt_refs,
        publication_mode="deterministic_receipt",
    )


def complete_book_candidate_coverage(
    content: str,
    evidence: Sequence[ReceiptEvidenceBundle],
) -> str:
    """Append only omitted admitted books without another semantic pass.

    The model remains responsible for prose. This deterministic final guard
    prevents balanced/deep answers from silently shrinking the Owner's
    admitted candidate set while using only the already trusted Response View.
    """

    view = project_external_answer_view(evidence)
    if view is None or view.response_depth == "quick" or not view.books:
        return content
    normalized_content = _normalized_book_text(content)
    missing = [
        item
        for item in view.books
        if not any(
            alias in normalized_content
            for alias in _book_title_aliases(item.title)
        )
    ]
    if not missing:
        return content
    addition = "\n\n### 📚 其他已核验候选\n\n" + "\n\n".join(
        _source_card(item, icon="") for item in missing
    )
    return content.rstrip() + addition


def render_book_evidence(output: dict) -> str:
    """Render one typed lookup receipt without another semantic model pass."""

    try:
        payload = BookEvidence.model_validate(output)
    except Exception:
        return ""
    query = str(payload.query or "").strip()
    if not payload.sources:
        return (
            f"没有找到与“{query}”匹配的可核验图书条目。"
            if query
            else "没有找到可核验的图书条目。"
        )
    heading = (
        f"找到与“{query}”匹配的图书条目："
        if query
        else "找到以下图书条目："
    )
    lines = [
        _source_card(
            AnswerSourceView(
                title=source.title.strip() or source.url.strip(),
                url=source.url.strip(),
                summary=source.snippet.strip(),
                published_date=source.published_date.strip(),
            ),
            icon="📚",
        )
        for source in payload.sources
        if source.url.strip()
    ]
    return f"{heading}\n\n" + "\n\n".join(lines) if lines else ""


def _render_external_answer_view(view: ExternalAnswerView) -> str:
    if view.status == "empty":
        return (
            "这次没有找到真正匹配的结果，我先不拿不相关的内容凑数。\n\n"
            "🔎 你可以把范围稍微放宽一点，或者告诉我更看重主题、难度还是阅读体验，我再帮你找。"
        )
    if view.status == "unavailable":
        return (
            "这次检索没有拿到足够可靠的内容，我先不贸然给结论。\n\n"
            "稍后重试即可；如果你愿意，也可以补充一个偏好的主题或阅读难度。"
        )

    sections: list[str] = []
    if view.books:
        opening = (
            "找到了，先把最相关的书目信息给你："
            if view.answer_type == "book_lookup"
            else "可以，先从这些候选里挑会比较合适："
        )
        grouped: dict[str, list[AnswerSourceView]] = {}
        for item in view.books:
            grouped.setdefault(item.theme or "推荐候选", []).append(item)
        book_sections = [opening]
        for theme, items in grouped.items():
            heading = (
                f"### 📚 {theme}"
                if len(grouped) > 1 or theme != "推荐候选"
                else ""
            )
            cards = "\n\n".join(_source_card(item, icon="") for item in items)
            book_sections.append(f"{heading}\n\n{cards}".strip())
        missing = [item.theme for item in view.coverage if item.status == "missing"]
        if missing:
            book_sections.append(
                "⚠️ 这次还没有找到足够可靠的"
                + "、".join(missing)
                + "候选，因此没有用不相关书目补位。"
            )
        sections.append("\n\n".join(book_sections))
    if view.web:
        sections.append(
            "### 🔎 相关资料\n\n"
            + "\n\n".join(_source_card(item, icon="") for item in view.web)
        )
    return "\n\n".join(sections).strip()


def _source_card(item: AnswerSourceView, *, icon: str) -> str:
    snippet = _clean_book_snippet(item.summary)
    suffix_parts = []
    if item.authors:
        suffix_parts.append(f"作者：{'、'.join(item.authors)}")
    if item.published_date:
        suffix_parts.append(f"日期：{item.published_date}")
    if snippet:
        suffix_parts.append(snippet[:220])
    heading = f"### {icon} [{item.title}]({item.url})".replace("###  ", "### ")
    return heading + ("\n" + "；".join(suffix_parts) if suffix_parts else "")


def _clean_book_snippet(value: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if re.search(
        r"我要写书评|写书评|#{2,}\s*\d+\s*有用|"
        r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}",
        text,
    ):
        return ""
    if len(re.findall(r"第[一二三四五六七八九十百\d]+章", text)) >= 2:
        return ""
    for marker in (
        "## 谁读这本书",
        "## 二手市场",
        "豆友",
        "人在读",
        "人读过",
        "人想读",
        "© 2005",
        "all rights reserved",
    ):
        if marker in text:
            text = text.split(marker, 1)[0].strip(" ·#;；")
    return text


def _normalized_book_text(value: str) -> str:
    return re.sub(
        r"[^\w\u4e00-\u9fff]+",
        "",
        str(value or "").casefold(),
    )


def _book_title_aliases(value: str) -> list[str]:
    """Return explicit display-title variants for coverage comparison only."""

    raw = normalize_book_work_title(value)
    values = [raw]
    # Catalog titles often append a marketing strapline in parentheses while
    # natural prose uses the bibliographic title. Removing only that explicit
    # suffix avoids duplicate completion cards without guessing synonyms.
    base = re.split(r"[（(【\[]", raw, maxsplit=1)[0].strip()
    if len(_normalized_book_text(base)) >= 4:
        values.append(base)
    return list(
        dict.fromkeys(
            item
            for candidate in values
            if (item := _normalized_book_text(candidate))
        )
    )


__all__ = [
    "complete_book_candidate_coverage",
    "render_book_evidence",
    "render_external_evidence_fallback",
]
