from __future__ import annotations

from typing import Any


LOGIN_WALL_SIGNALS = (
    "请登录",
    "登录后查看",
    "登录以继续",
    "订阅后查看",
    "订阅以继续",
    "会员专享",
    "付费内容",
    "购买后查看",
    "开通会员",
    "请订阅",
    "log in",
    "sign in to continue",
    "please login",
    "please log in",
    "subscribe to continue",
    "members only",
    "premium content",
)

NOT_FOUND_SIGNALS = (
    "页面不存在",
    "您访问的页面不存在",
    "页面已删除",
    "404 not found",
    "page not found",
    "not found - ",
)


def check_content_garbage(
    text: Any,
    *,
    min_chars: int | None = 100,
    url_ratio: float = 0.15,
) -> tuple[bool, str]:
    """Detect silently-bad content before it enters the evidence chain.

    Returns (is_garbage, reason) where reason is one of
    login_wall / too_short / ad_page / not_found. Pure and cheap; the gate
    is deliberately conservative and only rejects clearly unusable pages.
    """

    normalized = " ".join(str(text or "").split())
    if not normalized:
        return True, "too_short"
    lowered = normalized.lower()

    if min_chars is not None and len(normalized) < max(1, int(min_chars)):
        return True, "too_short"

    for signal in NOT_FOUND_SIGNALS:
        if signal in lowered:
            return True, "not_found"

    for signal in LOGIN_WALL_SIGNALS:
        if signal in lowered:
            return True, "login_wall"

    word_count = len(normalized.split())
    url_count = lowered.count("http")
    if word_count > 0 and url_count / word_count > max(0.0, float(url_ratio)):
        return True, "ad_page"

    return False, ""


def garbage_status(reason: str) -> str:
    """Normalize one garbage reason into a stable status token."""

    token = " ".join(str(reason or "").strip().lower().split())
    if token in {"login_wall", "too_short", "ad_page", "not_found"}:
        return f"garbage_{token}"
    return "garbage_content"


__all__ = ["check_content_garbage", "garbage_status"]
