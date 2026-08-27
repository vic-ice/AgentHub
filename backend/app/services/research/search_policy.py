from __future__ import annotations

import re
from datetime import datetime
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.research.query import normalize_research_query


SearchTimeRange = Literal["day", "week", "month", "year"]

_BOOK_RE = re.compile(
    r"图书(?!馆)|书籍|书单|书目|书名|读物|教材|入门书|新书|旧书|童书|"
    r"这种书|这类书|此类书|风格(?:的)?书|同类型(?:的)?书|"
    r"类似.{0,12}书|像.{0,12}书|"
    r"(?:看|读|推荐|找|选|聊|关于).{0,10}书(?:吗|呢|吧|籍|单|$)|"
    r"书(?:吗|呢|吧|单|籍|推荐|榜单|清单|类型|风格)|"
    r"\bbooks?\b",
    re.IGNORECASE,
)
_BOOK_QUANTITY_RE = re.compile(
    r"(?:至少|不少于|不低于|推荐|选择|列出|给出|找|想读|想看)?"
    r"\s*(?:\d+|[一二两三四五六七八九十几多若干]+)\s*本",
    re.IGNORECASE,
)
_EXPLICIT_BOOK_COUNT_RE = re.compile(
    r"(?:至少|不少于|不低于|推荐|选择|列出|给出|找)?\s*"
    r"(\d{1,2}|[一二两三四五六七八九十])\s*本",
    re.IGNORECASE,
)
_QUOTED_WORK_RE = re.compile(r"《[^》]{1,100}》")
_BOOK_FACT_RE = re.compile(
    r"作者|出版|出版社|版次|译者|书评|评分|阅读|读完|图书|书籍|"
    r"\bauthor\b|\bpublisher\b|\bedition\b|\bbook\b",
    re.IGNORECASE,
)
_BOOK_RECOMMENDATION_RE = re.compile(
    r"推荐|荐书|选书|书单|榜单|清单|值得(?:阅读|看|读)|"
    r"阅读建议|读什么|看什么|"
    r"类似|这种书|这种类型|这类|同类型|风格|有哪些|哪些.{0,8}书|"
    r"\brecommend|\breading\s+list|\bbooks?\s+like",
    re.IGNORECASE,
)
_BOOK_MARKETPLACE_DOMAINS = [
    "taobao.com",
    "tmall.com",
    "jd.com",
    "pinduoduo.com",
    "dangdang.com",
    "amazon.cn",
]


class ResearchSearchRequest(BaseModel):
    """Business-only input for one evidence-oriented web search."""

    objective: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=300)
    max_results: int = Field(default=5, ge=1, le=10)
    detail: Literal["standard", "deep"] = "deep"
    time_range: SearchTimeRange | None = None
    include_domains: list[str] = Field(default_factory=list, max_length=300)
    exclude_domains: list[str] = Field(default_factory=list, max_length=150)
    include_url_prefixes: list[str] = Field(default_factory=list, max_length=20)
    language: str = ""
    category: Literal["general", "news"] = "general"
    requirements: list[str] = Field(default_factory=list)

    def tool_arguments(self) -> dict[str, object]:
        return {
            "query": self.query,
            "max_results": self.max_results,
            "detail": self.detail,
            "time_range": self.time_range,
            "include_domains": list(self.include_domains),
            "exclude_domains": list(self.exclude_domains),
            "include_url_prefixes": list(self.include_url_prefixes),
            "language": self.language,
            "category": self.category,
        }


def build_research_search_request(
    objective: str,
    *,
    constraints: Iterable[Any] = (),
) -> ResearchSearchRequest:
    """Compile canonical routing constraints into search-domain filters."""

    normalized = normalize_research_query(objective)
    query_terms = [normalized]
    requirements: list[str] = []
    include_domains: list[str] = []
    exclude_domains: list[str] = []
    time_range: SearchTimeRange | None = None
    language = ""
    include_url_prefixes: list[str] = []
    max_results = 5

    is_book = _is_book_request_text(normalized)
    is_book_recommendation = bool(
        is_book and _BOOK_RECOMMENDATION_RE.search(normalized)
    )
    if is_book:
        requirements.append("book")
        query_terms.append("图书 出版 作者")
        exclude_domains.extend(_BOOK_MARKETPLACE_DOMAINS)
    if is_book_recommendation:
        requirements.append("book_recommendation")
        query_terms.extend(
            ["推荐书单 经典教材 入门 实战", "对比 适合人群 学习路线"]
        )
    for constraint in constraints:
        field = str(_constraint_value(constraint, "field") or "").strip()
        value = _constraint_value(constraint, "value")
        if field == "published_at":
            time_range = "year"
            requirements.append("recency")
            query_terms.extend([str(datetime.now().year), "出版 发布"])
        elif field == "rating":
            requirements.append("review")
            query_terms.append("书评 评分 推荐 榜单")
        elif field == "source_domain":
            values = value if isinstance(value, list) else [value]
            include_domains.extend(str(item or "") for item in values)
        elif field == "language":
            language = "zh" if "中" in str(value or "") else str(value or "")
        elif field == "result_limit":
            try:
                max_results = max(1, min(int(value), 10))
            except (TypeError, ValueError):
                max_results = 5
            requirements.append("result_limit")
    # Exact bibliographic research benefits from subject-page constraints.
    # Recommendation research first needs candidate discovery across editorial
    # and institutional reading lists; constraining it to one subject host
    # prevents the system from discovering multiple books at all.
    exact_book_lookup = (
        "book" in requirements
        and "book_recommendation" not in requirements
        and bool(_QUOTED_WORK_RE.search(normalized))
    )
    if exact_book_lookup and not include_domains:
        include_domains.extend(["book.douban.com", "douban.com"])
    explicit_douban_scope = any(
        domain.endswith("douban.com") for domain in include_domains
    )
    if (exact_book_lookup or explicit_douban_scope) and explicit_douban_scope:
        query_terms = [
            "site:book.douban.com/subject/",
            normalized,
            "豆瓣 评分 作者 出版社 高分 书评",
        ]
        include_url_prefixes = ["https://book.douban.com/subject/"]

    return ResearchSearchRequest(
        objective=normalized,
        query=_unique_query(" ".join(query_terms)),
        max_results=max_results,
        time_range=time_range,
        include_domains=list(dict.fromkeys(filter(None, include_domains))),
        exclude_domains=list(dict.fromkeys(exclude_domains)),
        include_url_prefixes=include_url_prefixes,
        language=language,
        requirements=list(dict.fromkeys(requirements)),
    )


def is_book_request(objective: str) -> bool:
    """Return the shared deterministic book-domain decision."""

    return _is_book_request_text(normalize_research_query(objective))


def is_book_recommendation_request(objective: str) -> bool:
    """Return the shared recommendation decision used by search and publishing."""

    normalized = normalize_research_query(objective)
    return bool(
        _is_book_request_text(normalized)
        and _BOOK_RECOMMENDATION_RE.search(normalized)
    )


def requested_book_count(objective: str) -> int:
    """Return an explicit requested book count, or zero when unspecified."""

    normalized = normalize_research_query(objective)
    match = _EXPLICIT_BOOK_COUNT_RE.search(normalized)
    if match is None:
        return 0
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    chinese = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    return chinese.get(raw, 0)


def _is_book_request_text(normalized: str) -> bool:
    return (
        bool(_BOOK_RE.search(normalized))
        or bool(_BOOK_QUANTITY_RE.search(normalized))
        or bool(
            _QUOTED_WORK_RE.search(normalized)
            and _BOOK_FACT_RE.search(normalized)
        )
    )


def _unique_query(query: str) -> str:
    terms = query.split()
    return " ".join(dict.fromkeys(terms))[:300].strip()


def _constraint_value(constraint: Any, field: str) -> Any:
    if isinstance(constraint, dict):
        return constraint.get(field)
    return getattr(constraint, field, None)
