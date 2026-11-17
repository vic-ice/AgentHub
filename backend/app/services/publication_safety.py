from __future__ import annotations

import re


_STRONG_INTERNAL_REASONING_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"(?:analysis|reasoning|thought\s+process|internal\s+(?:analysis|reasoning))\s*[:：]"
    r"|(?:以下是)?我的(?:思考过程|推理过程|内部分析|分析过程)"
    r"|我的思路是\s*[:：]"
    r")",
    re.IGNORECASE,
)

_SOFT_INTERNAL_REASONING_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"(?:我需要(?:先|首先)?|我得先|首先我需要)(?:理解|分析|梳理|检查|规划|考虑|推理|起草)"
    r"|(?:现在)?让我(?:先|来)?(?:分析|思考|检查|规划|推理|起草|梳理)"
    r"|接下来(?:我将|我要|需要)(?:分析|检查|规划|推理|起草|梳理)"
    r"|(?:分析|理解)(?:一下)?(?:用户需求|用户请求|这个请求|这个任务)"
    r"|检查(?:约束|格式|引用|指令)"
    r"|好的[，,]?\s*我们先(?:分析|思考|规划|检查)"
    r"|I\s+need\s+to\s+(?:understand|analy[sz]e|reason|plan|draft|check)"
    r"|We\s+need\s+to\s+(?:understand|analy[sz]e|reason|plan|draft|check|answer)"
    r"|Let(?:'s|\s+me)\s+(?:analy[sz]e|think|reason|plan|draft|check)"
    r"|Now\s+I(?:'ll|\s+will)\s+(?:analy[sz]e|reason|plan|draft|check)"
    r"|Checking\s+(?:the\s+)?(?:constraints|format|citations)"
    r"|Analysis\s+of\s+(?:the\s+)?(?:request|task|user(?:'s)?\s+request)"
    r")",
    re.IGNORECASE,
)


def contains_internal_reasoning(text: str) -> bool:
    """Return whether text contains a clear internal-monologue marker.

    The detector intentionally targets line-leading planning/checking phrases,
    not ordinary words such as "analysis" inside a finished answer. It is a
    publication guard, not a classifier for arbitrary prose.
    """

    value = str(text or "")
    if not value.strip():
        return False
    if re.search(r"</?\s*(?:think|analysis)\s*>", value, re.IGNORECASE):
        return True
    if _STRONG_INTERNAL_REASONING_RE.search(value):
        return True
    # One phrase such as “分析用户需求的方法” can be legitimate answer prose.
    # Requiring two planning/checking markers keeps the plain-text fallback
    # guard useful without turning a broad language regex into a topic ban.
    return len(_SOFT_INTERNAL_REASONING_RE.findall(value)) >= 2


def explicit_table_requested(request: str) -> bool:
    value = str(request or "").casefold()
    return (
        "表格" in value
        or "对比表" in value
        or re.search(r"\btable\b", value) is not None
    )


def markdown_table_present(text: str) -> bool:
    return bool(markdown_table_columns(text))


def markdown_table_columns(text: str) -> list[str]:
    lines = [line.strip() for line in str(text or "").splitlines()]
    for index in range(len(lines) - 1):
        header = lines[index]
        separator = lines[index + 1]
        if not header.startswith("|") or header.count("|") < 3:
            continue
        if not re.fullmatch(
            r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?",
            separator,
        ):
            continue
        return [
            cell.strip()
            for cell in header.strip("|").split("|")
            if cell.strip()
        ]
    return []


def requested_table_columns(request: str) -> list[str]:
    value = " ".join(str(request or "").split())
    patterns = (
        r"(?:列(?:为|包括|包含)|表头(?:为|是)|字段(?:为|包括|包含))\s*[:：]?\s*([^。；;\n]{2,180})",
        r"(?:请)?按\s*([^。；;\n]{2,180}?)\s*(?:的\s*)?"
        r"(?:[一二两三四五六七八九十\d]+\s*列\s*)?"
        r"(?:markdown\s*)?(?:表格|对比表)",
        r"(?:columns?|table\s+columns?)\s*(?:are|include|including|:|：)\s*([^.;\n]{2,180})",
    )
    segment = ""
    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            segment = match.group(1)
            break
    if not segment:
        return []
    segment = re.split(
        r"[，,]\s*(?:并|且|同时|以及|and\b|with\b)",
        segment,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    columns = [
        item.strip(" ：:列字段")
        for item in re.split(r"[、,，|/]", segment)
        if item.strip(" ：:列字段")
    ]
    return list(dict.fromkeys(columns))[:12]


def table_contract_satisfied(text: str, *, request: str) -> bool:
    if not explicit_table_requested(request):
        return True
    actual = markdown_table_columns(text)
    if not actual:
        return False
    required = requested_table_columns(request)
    if not required:
        return True
    normalized_actual = {
        re.sub(r"[\s_*\x60]+", "", item).casefold()
        for item in actual
    }
    return all(
        re.sub(r"[\s_*\x60]+", "", item).casefold() in normalized_actual
        for item in required
    )


__all__ = [
    "contains_internal_reasoning",
    "explicit_table_requested",
    "markdown_table_columns",
    "markdown_table_present",
    "requested_table_columns",
    "table_contract_satisfied",
]
