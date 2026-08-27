from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from app.services.books.book_identity import normalize_book_work_title


_GENERIC_TOPIC_PHRASES = (
    "不要推荐虚构书目",
    "为关键结论标注来源",
    "关键结论标注来源",
    "markdown表格",
    "适合人群",
    "推荐理由",
    "深度研究",
    "深度搜索",
    "零基础",
    "初学者",
    "适合",
    "读者",
    "推荐",
    "入门",
    "书籍",
    "图书",
    "书目",
    "好书",
    "书名",
    "作者",
    "出版社",
    "内容简介",
    "局限",
    "表格",
    "来源",
    "来源链接",
    "有来源",
    "链接",
    "输出",
    "报告",
    "近期",
    "值得阅读",
    "并给出",
    "给出",
    "至少",
    "几本",
    "方向",
    "主题",
    "请用",
    "请给",
    "请",
    "独立",
    "列为",
    "有什么",
    "有啥",
    "哪些",
    "好的",
    "求推荐",
)

_ENGLISH_TOPIC_STOP_WORDS = {
    "about",
    "author",
    "beginner",
    "book",
    "books",
    "citation",
    "citations",
    "deep",
    "evidence",
    "format",
    "introduction",
    "markdown",
    "recommend",
    "recommendation",
    "research",
    "reader",
    "source",
    "sources",
    "table",
}

_TOPIC_FAMILIES = (
    (
        "人工智能",
        "机器学习",
        "深度学习",
        "神经网络",
        "大模型",
        "生成式智能",
    ),
    ("财商", "理财", "财务", "金钱观", "个人金融"),
    ("沟通", "表达", "对话", "谈判"),
)

_AI_DEEP_LEARNING_CONTEXT = (
    "人工智能",
    "机器学习",
    "神经网络",
    "算法",
    "模型",
    "计算机",
    "python",
    "pytorch",
    "tensorflow",
    "goodfellow",
    "bengio",
    "courville",
    "古德费洛",
    "本吉奥",
    "库维尔",
)

_PERIODICAL_RE = re.compile(
    r"(?:\bissn\b|期刊|杂志|学报|第\s*\d+\s*期|"
    r"\bjournal\b|\bperiodical\b)",
    flags=re.IGNORECASE,
)
_BOOK_ENTITY_RE = re.compile(
    r"(?:\bisbn\b|图书|书籍|著作|单行本|第\s*\d+\s*版|"
    r"\bbook\b|\bhardcover\b|\bpaperback\b)",
    flags=re.IGNORECASE,
)
_EDITORIAL_BOOK_LIST_TITLE_RE = re.compile(
    r"(?:\d+\s*本.{0,20}(?:书|书籍)|"
    r"(?:完整|年度|精选|入门)?书单|"
    r"(?:推荐|盘点).{0,20}(?:书|书籍)|"
    r"(?:最佳|热门|经典|畅销|值得|必读).{0,20}(?:书|书籍)|"
    r"\b(?:top|best|recommended|must[- ]read)\b.{0,50}\bbooks?\b)",
    re.IGNORECASE,
)


def normalize_candidate_title(value: str) -> str:
    """Return an exact work-identity key without semantic prefix matching."""

    normalized = normalize_book_work_title(value)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized.casefold())


def is_probable_book_title(value: object) -> bool:
    """Reject list headlines and retrieval phrases before entity verification.

    A semantic planner is allowed to propose hypotheses, but a phrase such as
    "2026 年最佳理财书籍" is a search intent, not a work identity.  Keeping this
    validation structural avoids hard-coding any recommendation genre.
    """

    title = " ".join(str(value or "").split()).strip(" 《》")
    if not title or len(title) > 140:
        return False
    if _EDITORIAL_BOOK_LIST_TITLE_RE.search(title):
        return False
    return not bool(
        re.fullmatch(
            r"(?:\d{4}\s*年\s*)?(?:关于|适合|面向)?.{0,50}"
            r"(?:书籍推荐|图书推荐|推荐书目|阅读清单|购书清单)",
            title,
            flags=re.IGNORECASE,
        )
    )


def is_book_catalog_url(value: object) -> bool:
    """Accept only canonical book entity pages, never reviews or list pages."""

    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    if parsed.scheme.casefold() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    return bool(
        (
            host == "book.douban.com"
            and re.fullmatch(r"/subject/\d+/?", path)
        )
        or (
            (host == "goodreads.com" or host.endswith(".goodreads.com"))
            and re.fullmatch(r"/book/show/[^/]+/?", path)
        )
        or (host.startswith("books.google.") and path.rstrip("/") == "/books")
    )


def catalog_book_title(value: object, *, source_url: object) -> str:
    """Return a provider-chrome-free title only for a catalog book entity."""

    if not is_book_catalog_url(source_url):
        return ""
    title = str(value or "").strip()
    title = re.sub(
        r"\s*(?:"
        r"[（(]\s*(?:豆瓣|goodreads|google\s+books?)\s*[）)]"
        r"|[-—|]\s*(?:豆瓣读书|goodreads|google\s+books?)"
        r")\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip(" 《》")
    if (
        not title
        or len(title) > 140
        or title.casefold() in {"豆瓣", "豆瓣读书", "goodreads", "google books"}
        or _EDITORIAL_BOOK_LIST_TITLE_RE.search(title)
    ):
        return ""
    return title


def candidate_topic_supported(
    *,
    objective: str,
    themes: Iterable[str] = (),
    candidate_title: str = "",
    evidence_texts: Iterable[str] = (),
) -> bool:
    """Require source-owned topic support for a recommendation candidate.

    Catalog identity proves that an entity exists; it does not prove that the
    entity answers the user's topic.  This gate deliberately ignores provider
    rank and URL host, and fails open only when the request contains no usable
    topic anchor (for example, ``推荐几本好书``).
    """

    evidence = " ".join(
        str(value or "")
        for value in (candidate_title, *tuple(evidence_texts))
        if str(value or "").strip()
    )
    if _looks_like_periodical(evidence):
        return False

    anchors = _topic_anchors(objective=objective, themes=themes)
    if not anchors:
        return True

    haystack = _normalize_aliases(evidence)
    # Chinese "深度学习" is also used by pedagogy books.  A title match alone
    # is therefore insufficient for an AI/ML recommendation; require one
    # additional computing-domain signal to disambiguate the sense.
    if (
        any("深度学习" in anchor for anchor in anchors)
        and "深度学习" in haystack
        and not any(marker in haystack for marker in _AI_DEEP_LEARNING_CONTEXT)
    ):
        return False
    expanded = _expand_topic_families(anchors)
    if any(anchor in haystack for anchor in expanded if len(anchor) >= 2):
        return True

    expected_bigrams = {
        gram
        for anchor in expanded
        for gram in _chinese_bigrams(anchor)
    }
    observed_bigrams = set(_chinese_bigrams(haystack))
    return len(expected_bigrams.intersection(observed_bigrams)) >= 2


def objective_topic_anchors(
    objective: str,
    *,
    themes: Iterable[str] = (),
) -> list[str]:
    """Return bounded, deterministic topic terms for trusted catalog lookup."""

    anchors = _topic_anchors(objective=objective, themes=themes)
    ordered = sorted(
        anchors,
        key=lambda value: (-len(value), value),
    )
    joined = " ".join(anchors)
    # Catalog search is closer to entity lookup than web search. Stable topic
    # facets must lead the query list so verbose model-authored themes cannot
    # crowd out the short terms that catalogs actually match.
    if "深度学习" in _normalize_aliases(f"{objective} {joined}"):
        ordered = list(
            dict.fromkeys(
                [
                    "深度学习",
                    "深度学习入门",
                    "深度学习实战",
                    "Python 深度学习",
                    "神经网络与深度学习",
                    "机器学习实战",
                    *ordered,
                ]
            )
        )
    for family in _TOPIC_FAMILIES:
        if not any(member in joined for member in family):
            continue
        for member in family:
            if member not in ordered:
                ordered.append(member)
    objective_text = _normalize_aliases(objective)
    beginner_focus = bool(
        re.search(
            r"(?:零基础|初学者|新手|入门|beginner|from\s+scratch)",
            objective_text,
            flags=re.IGNORECASE,
        )
    )
    if beginner_focus:
        focused = [
            f"{item}入门"
            for item in ordered
            if re.search(r"[\u3400-\u9fff]", item)
            and not item.endswith("入门")
        ]
        ordered = list(dict.fromkeys([*focused, *ordered]))
    return ordered[:6]


def _topic_anchors(*, objective: str, themes: Iterable[str]) -> set[str]:
    values = [str(value or "") for value in themes if str(value or "").strip()]
    values.append(str(objective or ""))
    normalized = _normalize_aliases(" ".join(values))
    for phrase in sorted(_GENERIC_TOPIC_PHRASES, key=len, reverse=True):
        normalized = normalized.replace(phrase, " ")
    normalized = re.sub(r"\d+\s*本", " ", normalized)
    normalized = re.sub(r"[的地得]", " ", normalized)

    anchors: set[str] = set()
    for token in re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]{2,}", normalized):
        if token in _ENGLISH_TOPIC_STOP_WORDS:
            continue
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            # Conjunctions often join independent user themes without spaces.
            parts = [
                part
                for part in re.split(r"(?:以及|或者|和|与|及|或)", token)
                if len(part) >= 2
            ]
            anchors.update(part[:24] for part in parts)
        else:
            anchors.add(token[:40])
    return {anchor for anchor in anchors if anchor and not _generic_anchor(anchor)}


def _expand_topic_families(anchors: set[str]) -> set[str]:
    expanded = set(anchors)
    joined = " ".join(anchors)
    for family in _TOPIC_FAMILIES:
        if any(member in joined for member in family):
            expanded.update(family)
    return expanded


def _normalize_aliases(value: str) -> str:
    text = str(value or "").casefold()
    replacements = (
        (r"\bartificial\s+intelligence\b", "人工智能"),
        (r"\ba\.?i\.?\b", "人工智能"),
        (r"\bmachine\s+learning\b", "机器学习"),
        (r"\bdeep\s+learning\b", "深度学习"),
        (r"\bneural\s+networks?\b", "神经网络"),
        (r"\bfinancial\s+literacy\b", "财商"),
        (r"\bpersonal\s+finance\b", "理财"),
        (r"\bcommunication\b", "沟通"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return " ".join(text.split())


def _generic_anchor(value: str) -> bool:
    return value in {
        "结果",
        "结论",
        "理由",
        "人群",
        "限制",
        "虚构",
        "可靠",
        "高质量",
    }


def _chinese_bigrams(value: str) -> list[str]:
    grams: list[str] = []
    for segment in re.findall(r"[\u3400-\u9fff]{2,}", str(value or "")):
        grams.extend(segment[index : index + 2] for index in range(len(segment) - 1))
    return grams


def _looks_like_periodical(value: str) -> bool:
    text = str(value or "")
    return bool(_PERIODICAL_RE.search(text)) and not bool(_BOOK_ENTITY_RE.search(text))


__all__ = [
    "catalog_book_title",
    "candidate_topic_supported",
    "is_book_catalog_url",
    "is_probable_book_title",
    "normalize_candidate_title",
    "objective_topic_anchors",
]
