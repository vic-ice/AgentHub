from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExternalCapabilityAvailability:
    """Fine-grained switches shared by model projection and runtime admission."""

    weather_get: bool = False
    web_search: bool = False
    book_search: bool = False
    research_start: bool = False

    @classmethod
    def from_settings(
        cls,
        settings: Any | None = None,
    ) -> "ExternalCapabilityAvailability":
        # Deep Research is triggered by the user's explicit per-turn toggle
        # (research_mode=deep_research), not by a model-invocable capability.
        # research_start therefore stays out of the Chat tool set.
        return cls(
            weather_get=True,
            web_search=True,
            book_search=True,
            research_start=False,
        )

    def enabled(self, capability: str) -> bool:
        field_name = str(capability or "").strip()
        if field_name not in {
            "weather_get",
            "web_search",
            "book_search",
            "research_start",
        }:
            return False
        return bool(getattr(self, field_name))


__all__ = ["ExternalCapabilityAvailability"]
