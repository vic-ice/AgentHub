from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.services.agent_core.prompt_contracts import (
    ControllerContextSnapshot,
    TrustedTurnIntentContext,
)
from app.services.routing.speech_act import analyze_speech_act


TurnIntentLabel = Literal[
    "normal_answer",
    "memory_query",
    "memory_write",
    "memory_update",
    "memory_forget",
    "uncertain_statement",
    "research",
    "conversation_recall",
    "clarification_answer",
    "other_capability",
]

_MEMORY_ACTIONS = {"search_memory", "remember_memory", "forget_memory"}
_QUESTION_RE = re.compile(
    r"(?:[?？]\s*$|(?:吗|么|是不是|是否|有没有|会不会|能不能)\s*[?？]?$)",
    re.I,
)
_UNCERTAIN_RE = re.compile(
    r"(?:我觉得|感觉|好像|似乎|可能|也许|大概|应该|算是|不确定|不太确定|貌似)",
    re.I,
)
_RESEARCH_RE = re.compile(r"深度(?:搜索|研究)|研究报告|deep research", re.I)
_CONVERSATION_RECALL_RE = re.compile(
    r"(?:刚才|上面|上一轮|上一句).*(?:说|问|回答|回复)|"
    r"(?:我刚才|你刚才|我们刚才).*(?:什么)|"
    r"what did (?:i|you) (?:just )?say",
    re.I,
)
_FORGET_RE = re.compile(
    r"^(?:请)?(?:帮我)?(?:忘记|忘掉|忘了|删掉|删除|去掉|清除|不要再记)",
    re.I,
)
_FORGET_NEGATION_RE = re.compile(r"^(?:不要|别|不准|禁止).{0,8}(?:忘记|忘掉|忘了|删除|删掉)", re.I)
_WRITE_PROHIBITION_RE = re.compile(
    r"^(?:不要|别|不准|禁止|不用|无需).{0,12}(?:记住|记录|保存|存|写入)",
    re.I,
)
_EXPLICIT_WRITE_RE = re.compile(
    r"^(?:请)?(?:帮我)?(?:记住|记一下|记录|保存|以后要记得|别忘了|不要忘记)",
    re.I,
)
_MEMORY_UPDATE_RE = re.compile(
    r"(?:不是.+(?:是|叫)|改成|改为|更正|纠正|现在叫|以后叫|不叫.+叫)",
    re.I,
)
_USER_MEMORY_QUERY_RE = re.compile(
    r"^(?:我|我的|关于我|你(?:还)?记得我|你知道我).*(?:什么|哪些|多少|几|吗|么|是不是|是否|有没有|是谁|叫什么)|"
    r"^(?:我是谁|我叫什么|我的名字|我的宠物|我的偏好)",
    re.I,
)
_PERSONAL_DURABLE_RE = re.compile(
    r"^我(?:现在|以后|平时|通常|还)?(?:有|拥有|养|叫|是|喜欢|偏爱|不喜欢|讨厌|想要|住在|从事)"
    r"(?!谁|什么|啥|哪|多少)[^?？]*$",
    re.I,
)
_ENTITY_NAME_WRITE_RE = re.compile(
    r"^[^，。？?！!\s]+?的(?:名字|名称|昵称|英文名)(?:是|叫|为)[^，。？?！!\s]+$",
    re.I,
)
_ASSISTANT_TARGET_RE = re.compile(
    r"^(?:你|你们|这个助手|这个应用|系统).*(?:谁|什么|身份|名字|能做什么)",
    re.I,
)
_EXTERNAL_RE = re.compile(
    r"搜索|联网|查一下|查询|今天|现在|最新|天气|价格|新闻|search|browse|latest|weather",
    re.I,
)


@dataclass(frozen=True)
class TurnIntentDecision:
    intent: TurnIntentLabel
    source_role: Literal["user"] = "user"
    is_question: bool = False
    is_uncertain: bool = False
    needs_clarification: bool = False
    required_slots: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ()
    blocked_actions: tuple[str, ...] = ()
    confidence: float = 0.8
    rationale: tuple[str, ...] = field(default_factory=tuple)

    def to_context(self) -> TrustedTurnIntentContext:
        return TrustedTurnIntentContext(
            intent=self.intent,
            source_role=self.source_role,
            is_question=self.is_question,
            is_uncertain=self.is_uncertain,
            needs_clarification=self.needs_clarification,
            required_slots=list(self.required_slots),
            allowed_actions=list(self.allowed_actions),
            blocked_actions=list(self.blocked_actions),
            confidence=self.confidence,
            rationale=list(self.rationale),
        )


class TurnIntentGate:
    """Cheap, conservative turn-level action gate.

    It decides the kind of operation, not the full semantic facts. LLM/schema
    layers still extract and validate entities before any durable write.
    """

    def classify(
        self,
        text: str,
        *,
        context: ControllerContextSnapshot | None = None,
    ) -> TurnIntentDecision:
        normalized = " ".join(str(text or "").split()).strip()
        speech = analyze_speech_act(normalized)
        surface = speech.outer_text
        is_question = bool(_QUESTION_RE.search(surface))
        is_uncertain = bool(_UNCERTAIN_RE.search(surface))

        if context is not None and context.working_state is not None:
            pending = str(context.working_state.pending_question or "").strip()
            if pending and not _looks_like_new_request(surface):
                return TurnIntentDecision(
                    intent="clarification_answer",
                    is_question=is_question,
                    is_uncertain=is_uncertain,
                    allowed_actions=("remember_memory", "forget_memory", "search_memory"),
                    confidence=0.92,
                    rationale=("pending_clarification_answer",),
                )

        if speech.metalinguistic or speech.act in {
            "analyze_utterance",
            "translate",
            "classify_utterance",
            "generate_example",
            "compare",
            "summarize",
            "explain",
        }:
            return _decision("normal_answer", is_question, is_uncertain, "metalinguistic_or_explanatory")
        if _ASSISTANT_TARGET_RE.search(surface):
            return _decision("normal_answer", is_question, is_uncertain, "assistant_or_system_target")
        if _RESEARCH_RE.search(surface):
            return TurnIntentDecision(
                intent="research",
                is_question=is_question,
                is_uncertain=is_uncertain,
                allowed_actions=("research_start",),
                confidence=0.98,
                rationale=("explicit_research",),
            )
        if _CONVERSATION_RECALL_RE.search(surface):
            return TurnIntentDecision(
                intent="conversation_recall",
                is_question=is_question,
                is_uncertain=is_uncertain,
                allowed_actions=("conversation_read",),
                blocked_actions=tuple(sorted(_MEMORY_ACTIONS)),
                confidence=0.97,
                rationale=("current_thread_recall",),
            )
        if _FORGET_RE.search(surface) and not _FORGET_NEGATION_RE.search(surface):
            return TurnIntentDecision(
                intent="memory_forget",
                is_question=is_question,
                is_uncertain=is_uncertain,
                allowed_actions=("forget_memory",),
                blocked_actions=("remember_memory",),
                confidence=0.96,
                rationale=("explicit_forget",),
            )
        if _WRITE_PROHIBITION_RE.search(surface) or _FORGET_NEGATION_RE.search(surface):
            return TurnIntentDecision(
                intent="normal_answer",
                is_question=is_question,
                is_uncertain=is_uncertain,
                blocked_actions=tuple(sorted(_MEMORY_ACTIONS)),
                confidence=0.96,
                rationale=("explicit_memory_action_prohibition",),
            )
        if is_uncertain and (_PERSONAL_DURABLE_RE.search(surface) or _ENTITY_NAME_WRITE_RE.search(surface)):
            return TurnIntentDecision(
                intent="uncertain_statement",
                is_question=is_question,
                is_uncertain=True,
                needs_clarification=True,
                required_slots=("confirmation",),
                blocked_actions=("remember_memory",),
                confidence=0.95,
                rationale=("uncertain_durable_statement",),
            )
        if is_question and _USER_MEMORY_QUERY_RE.search(surface):
            return TurnIntentDecision(
                intent="memory_query",
                is_question=True,
                is_uncertain=is_uncertain,
                allowed_actions=("search_memory",),
                blocked_actions=("remember_memory",),
                confidence=0.95,
                rationale=("personal_memory_question",),
            )
        if _MEMORY_UPDATE_RE.search(surface):
            return TurnIntentDecision(
                intent="memory_update",
                is_question=is_question,
                is_uncertain=is_uncertain,
                allowed_actions=("remember_memory",),
                confidence=0.9,
                rationale=("correction_or_update",),
            )
        if _EXPLICIT_WRITE_RE.search(surface) or _PERSONAL_DURABLE_RE.search(surface) or _ENTITY_NAME_WRITE_RE.search(surface):
            return TurnIntentDecision(
                intent="memory_write",
                is_question=is_question,
                is_uncertain=is_uncertain,
                allowed_actions=("remember_memory",),
                blocked_actions=("search_memory",) if not is_question else ("remember_memory",),
                confidence=0.9,
                rationale=("durable_user_statement",),
            )
        if _EXTERNAL_RE.search(surface):
            return TurnIntentDecision(
                intent="other_capability",
                is_question=is_question,
                is_uncertain=is_uncertain,
                confidence=0.82,
                rationale=("non_memory_capability_signal",),
            )
        return _decision("normal_answer", is_question, is_uncertain, "default")


def classify_turn_intent(
    text: str,
    *,
    context: ControllerContextSnapshot | None = None,
) -> TurnIntentDecision:
    return TurnIntentGate().classify(text, context=context)


def _decision(
    intent: TurnIntentLabel,
    is_question: bool,
    is_uncertain: bool,
    reason: str,
) -> TurnIntentDecision:
    return TurnIntentDecision(
        intent=intent,
        is_question=is_question,
        is_uncertain=is_uncertain,
        confidence=0.85,
        rationale=(reason,),
    )


def _looks_like_new_request(text: str) -> bool:
    return bool(
        _RESEARCH_RE.search(text)
        or _CONVERSATION_RECALL_RE.search(text)
        or _FORGET_RE.search(text)
        or _EXPLICIT_WRITE_RE.search(text)
        or _EXTERNAL_RE.search(text)
    )


__all__ = [
    "TurnIntentDecision",
    "TurnIntentGate",
    "TurnIntentLabel",
    "classify_turn_intent",
]
