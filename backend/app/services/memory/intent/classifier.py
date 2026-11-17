from __future__ import annotations

import re

from app.services.memory.intent.lexicon import (
    MemoryIntent,
    compile_rules,
)


_ANAPHORA_RE = re.compile(
    r"^(?:那|那之前|那最早|那现在|那后来|之前呢|最早呢|那之前呢|那最早呢"
    r"|这(?:是)?(?:什么时候|啥时候)(?:说|记|改)(?:的)?(?:呢)?)[?？]?$"
)


class MemoryIntentClassifier:
    """Deterministic memory-domain intent classification with a lexicon."""

    def __init__(self) -> None:
        self._rules = compile_rules()

    def classify(
        self,
        text: str,
        *,
        previous_topic: MemoryIntent | None = None,
    ) -> MemoryIntent | None:
        cleaned = " ".join(str(text or "").split()).strip()
        if not cleaned:
            return None

        if _ANAPHORA_RE.match(cleaned) and previous_topic is not None:
            return self._anaphora(cleaned, previous_topic)

        for kind, pattern, build, confidence in self._rules:
            match = pattern.match(cleaned)
            if match is None:
                continue
            intent = build(match, cleaned)
            if intent is None:
                continue
            return MemoryIntent(
                **{
                    **intent.__dict__,
                    "confidence": min(intent.confidence, confidence),
                }
            )
        return None

    def _anaphora(
        self,
        text: str,
        topic: MemoryIntent,
    ) -> MemoryIntent | None:
        if "什么时候" in text or "啥时候" in text:
            return MemoryIntent(
                kind="when",
                predicate=topic.predicate or "name",
                scope="timeline",
                evidence_quote=text,
                confidence=0.8,
            )
        if "最早" in text:
            scope = "earliest"
        elif "之前" in text:
            scope = "previous"
        elif "后来" in text:
            scope = "timeline"
        else:
            scope = "current"
        return MemoryIntent(
            kind="read",
            predicate=topic.predicate or "name",
            scope=scope,
            evidence_quote=text,
            confidence=0.8,
        )
