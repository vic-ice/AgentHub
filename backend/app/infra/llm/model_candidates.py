from __future__ import annotations

import threading
import time
from typing import Any


_FAILURE_COOLDOWN_SECONDS = {
    "rate_limited": 60,
    "quota": 60,
    "timeout": 30,
    "model_unavailable": 120,
}

_cooldowns: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def record_model_failure(model_id: str, cause: str) -> None:
    """Mark a model unavailable for a cooldown window after a health failure."""

    if not model_id:
        return
    seconds = _FAILURE_COOLDOWN_SECONDS.get(str(cause or ""), 0)
    if seconds <= 0:
        return
    with _lock:
        _cooldowns[model_id] = {
            "until": time.time() + seconds,
            "cause": str(cause),
            "ts": time.time(),
        }


def record_model_success(model_id: str) -> None:
    """Clear the cooldown after a successful call."""

    if not model_id:
        return
    with _lock:
        _cooldowns.pop(model_id, None)


def _in_cooldown(model_id: str) -> str | None:
    with _lock:
        entry = _cooldowns.get(model_id)
        if entry is None:
            return None
        if entry["until"] <= time.time():
            _cooldowns.pop(model_id, None)
            return None
        return str(entry.get("cause") or "")


def candidate_model_ids(
    requested: str | None = None,
    *,
    limit: int = 3,
) -> list[str]:
    """Ordered fallback candidates: requested first, then healthy active LLMs.

    Models in cooldown (rate/quota/timeout/unavailable) are deprioritized so
    the fallback tries the next *usable* model instead of burning attempts on
    known-broken ones.
    """

    from app.infra.llm.manager import get_model_manager

    manager = get_model_manager()
    candidates: list[str] = []

    def _add(model_id: str) -> None:
        if model_id and model_id not in candidates:
            candidates.append(model_id)

    requested_resolved = _resolve_requested(requested)
    if requested_resolved:
        _add(requested_resolved)
    healthy: list[str] = []
    cooling: list[str] = []
    try:
        infos = manager.get_model_info_list(active_only=True)
    except Exception:
        infos = []
    for info in infos:
        model_id = str(
            getattr(info, "model_uuid", "")
            or getattr(info, "id", "")
            or ""
        )
        if not model_id:
            continue
        if _in_cooldown(model_id):
            cooling.append(model_id)
        else:
            healthy.append(model_id)
    for model_id in [*healthy, *cooling]:
        _add(model_id)
        if len(candidates) >= max(1, limit):
            break
    return candidates


def _resolve_requested(requested: str | None) -> str:
    from app.infra.llm.manager import get_model_manager

    if requested:
        return str(requested)
    manager = get_model_manager()
    return (
        manager.default_llm_id
        or manager.get_first_active_llm_id()
        or ""
    )


__all__ = [
    "candidate_model_ids",
    "record_model_failure",
    "record_model_success",
]
