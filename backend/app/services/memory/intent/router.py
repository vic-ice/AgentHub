from __future__ import annotations

from app.services.agent_core.contracts import (
    ControllerOutput,
    ControllerToolCall,
)
from app.services.memory.guardrails import (
    guard_memory_forget_source,
    guard_memory_read_query,
    guard_memory_write_source,
    is_affirmative_forget_request,
)
from app.services.memory.intent.classifier import MemoryIntentClassifier
from app.services.memory.intent.lexicon import MemoryIntent


_READ_SCOPES = {"current", "previous", "earliest", "timeline"}
_SLOT_LABELS = {
    "new": "新的名字/称呼",
    "entity": "具体对象",
}
_GENERIC_ENTITIES = {"宠物", "动物", "东西", "物品"}
_FILLERS = {"嗯", "好", "行", "好的", "行吧", "知道了", "哦", "嗯嗯", "好的吧"}


class MemoryIntentRouter:
    """Rule-driven memory flow; returns None to let the Controller decide."""

    def __init__(
        self,
        classifier: MemoryIntentClassifier | None = None,
    ) -> None:
        self._classifier = classifier or MemoryIntentClassifier()

    def decide(self, request) -> ControllerOutput | None:
        text = " ".join(
            str(getattr(request, "current_user_message", "") or "").split()
        ).strip()
        if not text:
            return None
        if self._blocked_before_classification(text):
            return None
        topic = self._previous_topic(request)
        intent = self._classifier.classify(text, previous_topic=topic)
        if intent is None:
            return self._complete_pending(request, text, topic)
        if intent.kind == "forget" and guard_memory_forget_source(text).blocked:
            return None
        if intent.kind in {"write", "correct"} and guard_memory_write_source(text).blocked:
            return None
        if intent.kind == "decline":
            return self._decline_output(topic)
        return self._to_output(intent)

    def matches(self, text: str) -> bool:
        """Conservative early gate: true only for non-anaphora memory intents.

        Anaphora ("那之前呢") needs the previous user turn, so it is excluded
        from the early path and resolved with full conversation context.
        """

        cleaned = " ".join(str(text or "").split()).strip()
        if self._blocked_before_classification(cleaned):
            return False
        intent = self._classifier.classify(cleaned)
        if intent is None:
            return False
        if intent.kind == "forget" and guard_memory_forget_source(cleaned).blocked:
            return False
        if intent.kind in {"write", "correct"} and guard_memory_write_source(cleaned).blocked:
            return False
        return True

    def _to_output(self, intent: MemoryIntent) -> ControllerOutput | None:
        if intent.kind in {"write", "correct"}:
            if intent.requires or self._needs_entity_clarification(intent):
                return self._clarify(intent)
            return self._write_output(intent)
        if intent.kind == "forget":
            return ControllerOutput(
                mode="capability_proposals",
                tool_calls=[
                    ControllerToolCall(
                        call_id="rule-memory-forget",
                        name="forget_memory",
                        arguments={
                            "targets": [
                                {
                                    "subject": "self",
                                    "predicate": intent.predicate,
                                    "identity": intent.identity or {},
                                    "qualifiers": intent.qualifiers,
                                    "evidence_quote": intent.evidence_quote,
                                }
                            ]
                        },
                    )
                ],
            )
        if intent.kind in _READ_SCOPES or intent.kind in {"when", "read"}:
            scope = intent.scope or "current"
            return ControllerOutput(
                mode="capability_proposals",
                tool_calls=[
                    ControllerToolCall(
                        call_id="rule-memory-search",
                        name="search_memory",
                        arguments={
                            "query": intent.query,
                            "predicate": intent.predicate,
                            "scope": scope,
                        },
                    )
                ],
            )
        return None

    def _write_output(self, intent: MemoryIntent) -> ControllerOutput:
        assertions = intent.assertions or [
            {
                "subject": "self",
                "predicate": intent.predicate,
                "value": intent.value or {},
                "qualifiers": intent.qualifiers,
                "evidence_quote": intent.evidence_quote,
            }
        ]
        return ControllerOutput(
            mode="capability_proposals",
            tool_calls=[
                ControllerToolCall(
                    call_id="rule-memory-write",
                    name="remember_memory",
                    arguments={"assertions": assertions},
                )
            ],
        )

    def _needs_entity_clarification(self, intent: MemoryIntent) -> bool:
        if intent.predicate != "has":
            return False
        entity = str((intent.value or {}).get("entity") or "").strip()
        return entity in _GENERIC_ENTITIES

    def _blocked_before_classification(self, text: str) -> bool:
        if guard_memory_read_query(text).blocked:
            return True
        forget_guard = guard_memory_forget_source(text)
        if forget_guard.reason_code == "memory_forget_forbidden_by_user":
            return True
        if is_affirmative_forget_request(text):
            return False
        write_guard = guard_memory_write_source(text)
        return write_guard.reason_code in {
            "memory_write_forbidden_by_user",
            "memory_write_metalinguistic",
        }

    def _decline_output(
        self,
        prior: MemoryIntent | None,
    ) -> ControllerOutput | None:
        """User declined to complete a clarification: save the partial fact."""

        if prior is None or prior.kind != "write":
            return None
        return self._write_output(prior)

    def _complete_pending(
        self,
        request,
        text: str,
        prior: MemoryIntent | None,
    ) -> ControllerOutput | None:
        """Bare value answer to a pending clarification completes the fact."""

        if prior is None or prior.kind not in {"write", "correct"}:
            return None
        context = getattr(request, "context", None)
        working = getattr(context, "working_state", None)
        pending = (
            str(getattr(working, "pending_question", "") or "").strip()
            if working is not None
            else ""
        )
        if not pending:
            return None
        value = " ".join(text.split()).strip()
        if (
            not value
            or len(value) < 2
            or len(value) > 30
            or value in _FILLERS
            or any(ch in value for ch in "，。？?！!、")
        ):
            return None
        if prior.predicate == "has":
            completed = MemoryIntent(
                kind="write",
                predicate="has",
                value={"entity": value},
                evidence_quote=text,
            )
        elif prior.predicate == "name":
            completed = MemoryIntent(
                kind="write",
                predicate="name",
                value={"name": value},
                evidence_quote=text,
            )
        else:
            return None
        return self._write_output(completed)

    def _clarify(self, intent: MemoryIntent) -> ControllerOutput:
        if intent.predicate == "has":
            return ControllerOutput(
                mode="request_clarification",
                text=(
                    f"你提到{intent.evidence_quote}，能告诉我具体是什么宠物吗？"
                    "不方便细说的话，回复'先记着'我会先保存。"
                ),
            )
        missing = "、".join(
            _SLOT_LABELS.get(slot, slot) for slot in intent.requires
        )
        return ControllerOutput(
            mode="request_clarification",
            text=f"还缺少必要信息：{missing}，请补充完整后我再保存。",
        )

    def _previous_topic(self, request) -> MemoryIntent | None:
        """Resolve anaphora from the last user turn in the conversation."""

        context = getattr(request, "context", None)
        conversation = getattr(context, "conversation", None) or []
        for turn in reversed(conversation):
            if getattr(turn, "role", "") != "user":
                continue
            return self._classifier.classify(
                str(getattr(turn, "content", "") or "").strip()
            )
        return None

