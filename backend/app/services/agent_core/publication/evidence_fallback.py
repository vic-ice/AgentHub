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
from app.services.books.book_identity import (
    canonical_book_source_url,
    normalize_book_work_title,
)
from app.services.publication_safety import (
    explicit_table_requested,
    requested_table_columns,
    table_contract_satisfied,
)
from app.services.external_capabilities.contracts import BookEvidence


_AUTHOR_SECTION_RE = re.compile(
    r"(?:#{1,3}\s*)?(?:作者简介|作者介绍|创作者简介|译者简介)\s*"
    r".*?(?=(?:#{1,3}\s*)?(?:内容简介|作品简介|图书简介|本书简介)\s*|"
    r"\[\s*\.{3}\s*\]|$)",
    re.IGNORECASE | re.DOTALL,
)
_AUTHOR_OR_BIBLIOGRAPHIC_RE = re.compile(
    r"(?:作者|著者|译者|出版社|出版年|出版时间|ISBN|页数|定价)\s*[:：]"
    r"|(?:出生|生于|毕业于|任职于|教授|投资家|企业家|演说家|"
    r"畅销书作家|创立|代表作|荣获|获得.{0,12}奖)",
    re.IGNORECASE,
)
_CONTENT_REASON_RE = re.compile(
    r"本书|书中|作品|讲述|介绍|讲解|涵盖|聚焦|围绕|讨论|"
    r"通过.{0,12}(?:故事|案例|练习|方法)|帮助.{0,20}(?:理解|学习|掌握|建立|培养)|"
    r"教会|方法|原理|案例|实践|知识|技能|主题|内容|"
    r"(?:book|guide)\s+(?:covers|explains|introduces)|"
    r"(?:covers|explains|introduces|focuses on)",
    re.IGNORECASE,
)
_AUDIENCE_SIGNAL_RE = re.compile(
    r"零基础|零起点|无需基础|初学者|初学|新手|入门|小白|"
    r"幼儿|学龄前|儿童|孩子|少儿|小学生|青少年|中学生|"
    r"大学生|职场|家长|父母|亲子|研究生|专业读者|有基础|进阶|"
    r"beginner|introductory|children|kids|teen|student|professional|advanced",
    re.IGNORECASE,
)


def render_external_evidence_fallback(
    evidence: Sequence[ReceiptEvidenceBundle],
    *,
    user_request: str = "",
) -> PublishedAnswer | None:
    """Publish admitted external evidence when model synthesis is unavailable.

    This renderer never invents recommendations or reinterprets the request. It
    only exposes deduplicated titles, snippets and URLs already admitted by the
    typed capability receipts.
    """

    view = project_external_answer_view(
        evidence,
        user_request=user_request,
    )
    if view is None:
        return None
    content = (
        "以下对比仅使用本轮已核验来源；来源没有明确说明的字段已如实标注。"
        if explicit_table_requested(user_request)
        else _render_external_answer_view(view)
    )
    content = ensure_explicit_evidence_format(
        content,
        evidence,
        user_request=user_request,
    )
    return PublishedAnswer(
        status="completed",
        content=content,
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


def ensure_explicit_evidence_format(
    content: str,
    evidence: Sequence[ReceiptEvidenceBundle],
    *,
    user_request: str,
) -> str:
    """Deterministically satisfy an explicit table request from admitted data."""

    if not explicit_table_requested(user_request):
        return content
    view = project_external_answer_view(
        evidence,
        user_request=user_request,
    )
    if view is None:
        return content
    if view.books and table_contract_satisfied(content, request=user_request):
        linked, complete = _bind_verified_book_table_links(content, view.books)
        if complete:
            return linked
    rows: list[str] = []
    if view.books:
        columns = requested_table_columns(user_request) or [
            "书名",
            "作者",
            "方向",
            "可核验依据",
        ]
        header = (
            "| " + " | ".join(_table_cell(item) for item in columns) + " |\n"
            + "| " + " | ".join("---" for _ in columns) + " |"
        )
        for item in view.books:
            cells = [
                _book_table_value(column, item)
                for column in columns
            ]
            rows.append("| " + " | ".join(cells) + " |")
    elif view.web:
        columns = requested_table_columns(user_request) or ["来源", "摘要"]
        header = (
            "| " + " | ".join(_table_cell(item) for item in columns) + " |\n"
            + "| " + " | ".join("---" for _ in columns) + " |"
        )
        for item in view.web:
            cells = [
                _web_table_value(column, item)
                for column in columns
            ]
            rows.append("| " + " | ".join(cells) + " |")
    else:
        return content
    table = "### 对比表\n\n" + header + "\n" + "\n".join(rows)
    if view.books:
        # Unknown or missing entities cannot be repaired safely. Only this
        # exceptional path replaces the table with admitted receipt fields.
        return (
            "以下对比仅使用本轮已核验书目与公开来源；"
            "证据未覆盖的字段已如实标注。\n\n"
            + table
        )
    if table_contract_satisfied(content, request=user_request):
        return content
    return content.rstrip() + "\n\n" + table


def _bind_verified_book_table_links(
    content: str,
    books: Sequence[AnswerSourceView],
) -> tuple[str, bool]:
    """Bind admitted URLs into a valid model-written table without rewriting prose."""

    lines = str(content or "").splitlines()
    admitted = {
        normalize_book_work_title(item.title): item
        for item in books
        if normalize_book_work_title(item.title) and str(item.url or "").strip()
    }
    if not admitted:
        return content, False
    for index in range(len(lines) - 1):
        header_line = lines[index].strip()
        separator_line = lines[index + 1].strip()
        if not header_line.startswith("|") or not separator_line.startswith("|"):
            continue
        headers = [cell.strip() for cell in header_line.strip("|").split("|")]
        if not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separator_line.strip("|").split("|")):
            continue
        title_index = next(
            (
                position
                for position, header in enumerate(headers)
                if any(marker in re.sub(r"\s+", "", header).casefold() for marker in ("书名", "title", "图书"))
            ),
            None,
        )
        if title_index is None:
            continue
        seen: set[str] = set()
        cursor = index + 2
        while cursor < len(lines) and lines[cursor].lstrip().startswith("|"):
            cells = [cell.strip() for cell in lines[cursor].strip().strip("|").split("|")]
            if len(cells) != len(headers):
                return content, False
            raw_title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cells[title_index])
            normalized = normalize_book_work_title(raw_title.strip("《》 "))
            item = admitted.get(normalized)
            if item is None or normalized in seen:
                return content, False
            cells[title_index] = (
                f"[{_table_cell(item.title)}]"
                f"({canonical_book_source_url(item.url)})"
            )
            lines[cursor] = "| " + " | ".join(cells) + " |"
            seen.add(normalized)
            cursor += 1
        return "\n".join(lines), bool(seen)
    return content, False


def _table_cell(value: str) -> str:
    return " ".join(str(value or "").split()).replace("|", "｜")


def _book_table_value(column: str, item: AnswerSourceView) -> str:
    key = re.sub(r"\s+", "", str(column or "")).casefold()
    if any(marker in key for marker in ("书名", "title", "图书")):
        return (
            f"[{_table_cell(item.title)}]"
            f"({canonical_book_source_url(item.url)})"
        )
    if any(marker in key for marker in ("作者", "author")):
        return _table_cell("、".join(item.authors) or "现有来源未注明")
    if any(marker in key for marker in ("主题", "方向", "类型", "theme", "category")):
        return _table_cell(item.theme or "推荐候选")
    if any(marker in key for marker in ("出版", "年份", "日期", "date", "year")):
        return _table_cell(item.published_date or "现有来源未注明")
    if any(marker in key for marker in ("来源", "链接", "source", "link")):
        return f"[查看来源]({item.url})"
    if any(marker in key for marker in ("理由", "依据", "简介", "内容", "summary", "reason")):
        return _book_recommendation_reason(item)[0]
    if any(marker in key for marker in ("适合", "人群", "audience")):
        return _book_audience(item)[0]
    if any(marker in key for marker in ("局限", "取舍", "注意", "limit", "trade")):
        return _book_evidence_limitation(item)
    return "现有来源未提供该字段"


def _book_recommendation_reason(item: AnswerSourceView) -> tuple[str, bool]:
    """Return one bounded content reason, never an author biography."""

    fragments = _book_content_fragments(item.summary)
    candidates: list[tuple[int, int, int, str]] = []
    for index, fragment in enumerate(fragments):
        semantic = fragment.replace(item.title, " ").strip()
        signals = _CONTENT_REASON_RE.findall(semantic)
        if not signals:
            continue
        candidates.append(
            (
                len(signals),
                1 if _AUDIENCE_SIGNAL_RE.search(semantic) else 0,
                -index,
                fragment,
            )
        )
    if candidates:
        selected = max(candidates)[3]
        return _bounded_book_field(selected, limit=180), True

    summary = " ".join(str(item.summary or "").split()).strip()
    if _AUTHOR_OR_BIBLIOGRAPHIC_RE.search(summary):
        return (
            "现有来源仅提供作者或书目信息，缺少可核验的内容型推荐依据",
            False,
        )
    if summary:
        return (
            "现有来源摘要未明确说明本书内容，暂无法给出具体推荐理由",
            False,
        )
    return (
        "现有来源未提供可核验的内容简介，暂无法给出具体推荐理由",
        False,
    )


def _book_audience(item: AnswerSourceView) -> tuple[str, bool]:
    """Infer only coarse audiences explicitly signalled by title/content."""

    content = " ".join(
        [item.title, *_book_content_fragments(item.summary)]
    ).casefold()
    audience_rules = (
        (r"幼儿|学龄前", "幼儿或亲子共读人群"),
        (r"小学生|儿童|孩子|少儿|children|kids", "儿童或亲子共读人群"),
        (r"青少年|中学生|teen", "青少年读者"),
        (r"大学生|college student|university student", "大学生读者"),
        (r"职场|professional", "职场读者"),
        (r"家长|父母|亲子", "家长或亲子共读人群"),
        (
            r"零基础|零起点|无需基础|初学者|初学|新手|入门|小白|"
            r"beginner|introductory",
            "零基础或初学者",
        ),
        (r"研究生|专业读者|有基础|进阶|advanced", "已有相关基础、希望进阶的读者"),
    )
    for pattern, label in audience_rules:
        if re.search(pattern, content, flags=re.IGNORECASE):
            return label, True
    return "标题和内容摘要未明确标注适合人群", False


def _book_evidence_limitation(item: AnswerSourceView) -> str:
    _, reason_supported = _book_recommendation_reason(item)
    _, audience_supported = _book_audience(item)
    if not reason_supported and not audience_supported:
        return "缺少内容型来源与明确适读标记，无法核验具体价值和阅读门槛"
    if not reason_supported:
        return "缺少内容型来源，无法核验具体推荐价值"
    if not audience_supported:
        return "内容摘要未标注适读人群，阅读门槛和适配性仍需自行确认"
    source_urls = {
        value
        for value in [
            item.url,
            *(source.url for source in item.supporting_sources),
        ]
        if str(value or "").strip()
    }
    if len(source_urls) <= 1:
        return "内容定位与适读判断仅由单一公开来源支持，未做多源交叉核验"
    return "现有来源主要覆盖内容定位和适读线索，未充分说明版本差异或前置知识"


def _book_content_fragments(value: str) -> list[str]:
    """Extract content-bearing fragments while excluding author metadata."""

    text = str(value or "").strip()
    if not text:
        return []
    text = _AUTHOR_SECTION_RE.sub(" ", text)
    text = re.sub(
        r"(?:#{1,3}\s*)?(?:内容简介|作品简介|图书简介|本书简介)\s*[:：]?",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\[\s*\.{3}\s*\]", "。", text)
    compact = " ".join(text.split()).strip()
    fragments: list[str] = []
    for raw in re.split(r"[。！？!?；;]+", compact):
        fragment = raw.strip(" ·#，,：:")
        fragment = re.sub(
            r"^(?:(?:购买)?(?:纸质版|电子版|精装版)?\s*"
            r"\d+(?:\.\d+)?\s*元\s*)+",
            "",
            fragment,
        ).strip(" ·#，,：:")
        if len(fragment) < 6:
            continue
        if _AUTHOR_OR_BIBLIOGRAPHIC_RE.search(fragment):
            continue
        if not (
            _CONTENT_REASON_RE.search(fragment)
            or _AUDIENCE_SIGNAL_RE.search(fragment)
        ):
            continue
        fragments.append(fragment)
    return fragments


def _bounded_book_field(value: str, *, limit: int) -> str:
    cleaned = _table_cell(value).strip(" ，,；;。")
    if len(cleaned) <= limit:
        return cleaned
    shortened = cleaned[: limit - 1].rstrip(" ，,；;。")
    return shortened + "…"


def _web_table_value(column: str, item: AnswerSourceView) -> str:
    key = re.sub(r"\s+", "", str(column or "")).casefold()
    if any(marker in key for marker in ("来源", "标题", "链接", "source", "title", "link")):
        return f"[{_table_cell(item.title)}]({item.url})"
    if any(marker in key for marker in ("摘要", "结论", "内容", "依据", "summary", "finding")):
        return _table_cell(item.summary or "现有来源未提供摘要")[:360]
    if any(marker in key for marker in ("日期", "时间", "date", "time")):
        return _table_cell(item.published_date or "现有来源未注明")
    return "现有来源未提供该字段"


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
    "ensure_explicit_evidence_format",
    "render_book_evidence",
    "render_external_evidence_fallback",
]
