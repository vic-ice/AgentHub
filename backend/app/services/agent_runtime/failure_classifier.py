from __future__ import annotations

from typing import Literal


FailureDisposition = Literal["ok", "retry", "degrade", "skip", "abandon"]

_RETRY_STATUSES = frozenset({"timeout"})
_RETRY_ERROR_TYPES = frozenset(
    {"timeout", "network", "connection", "rate_limited", "temporary"}
)
_DEGRADE_STATUSES = frozenset({"unavailable"})
_SKIP_STATUSES = frozenset(
    {
        "empty",
        "empty_result",
        "invalid_url",
        "blocked_url",
        "no_extractable_claims",
        "no_publishable_evidence",
        "not_requested",
        "garbage_content",
        "garbage_login_wall",
        "garbage_too_short",
        "garbage_ad_page",
        "garbage_not_found",
    }
)
_ABANDON_STATUSES = frozenset({"hard_error", "blocked", "failed", "error"})
_ABANDON_ERROR_TYPES = frozenset({"hard_error", "blocked", "invalid_url"})
_OK_STATUSES = frozenset({"ok", "completed", "success", "accepted"})


def classify_capability_failure(
    *,
    status: str = "",
    error_type: str = "",
) -> FailureDisposition:
    """Classify one tool result into a stable failure disposition.

    retry   -> transient (timeout/network): retry with backoff or later.
    degrade -> service unavailable: fall back to another provider/channel.
    skip    -> useless but expected (empty/garbage): change strategy, do not
               retry the same call.
    abandon -> hard or blocked: stop and disclose; do not auto-retry.
    """

    normalized_status = str(status or "").strip().lower()
    normalized_error = str(error_type or "").strip().lower()
    if normalized_status in _OK_STATUSES and not normalized_error:
        return "ok"
    if normalized_status in _RETRY_STATUSES or any(
        token in normalized_error for token in _RETRY_ERROR_TYPES
    ):
        return "retry"
    if normalized_status in _DEGRADE_STATUSES:
        return "degrade"
    if normalized_status.startswith("garbage_") or normalized_status in _SKIP_STATUSES:
        return "skip"
    if normalized_status in _ABANDON_STATUSES or any(
        token in normalized_error for token in _ABANDON_ERROR_TYPES
    ):
        return "abandon"
    if normalized_status:
        return "abandon"
    return "ok"


__all__ = ["FailureDisposition", "classify_capability_failure"]
