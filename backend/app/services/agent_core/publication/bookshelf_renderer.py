from __future__ import annotations

from typing import Any


_STATUS_LABELS = {
    "want_to_read": "想读",
    "reading": "在读",
    "read": "已读",
    "dropped": "弃读",
}
_EVALUATION_LABELS = {
    "liked": "喜欢",
    "neutral": "一般",
    "disliked": "不喜欢",
    "not_interested": "不感兴趣",
}


def render_bookshelf_read(output: dict[str, Any]) -> str:
    """Render typed Shelf rows without reinterpreting user language."""

    items = [
        item for item in output.get("items", []) if isinstance(item, dict)
    ]
    if not items:
        return "你的书架目前还没有符合条件的书。"

    lines: list[str] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        status = _STATUS_LABELS.get(
            str(item.get("reading_status") or ""),
            "状态未知",
        )
        evaluation_value = item.get("evaluation")
        evaluation = (
            _EVALUATION_LABELS.get(str(evaluation_value), "未评价")
            if evaluation_value is not None
            else "未评价"
        )
        lines.append(f"- 《{title}》：{status}；评价：{evaluation}")

    if not lines:
        return "你的书架目前还没有可展示的书。"
    total = int(output.get("total") or len(lines))
    return f"你的书架目前有 {total} 本书：\n\n" + "\n".join(lines)


__all__ = ["render_bookshelf_read"]
