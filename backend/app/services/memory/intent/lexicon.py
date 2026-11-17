from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


MemoryIntentKind = str


@dataclass(frozen=True)
class MemoryIntent:
    """One deterministic memory-domain intent with extracted slots."""

    kind: MemoryIntentKind
    predicate: str = ""
    value: dict[str, Any] | None = None
    qualifiers: dict[str, Any] = field(default_factory=dict)
    scope: str | None = None
    identity: dict[str, Any] | None = None
    query: str = ""
    assertions: list[dict[str, Any]] = field(default_factory=list)
    evidence_quote: str = ""
    requires: list[str] = field(default_factory=list)
    confidence: float = 0.9


Builder = Callable[[re.Match, str], MemoryIntent | None]


@dataclass(frozen=True)
class IntentRule:
    kind: MemoryIntentKind
    pattern: str
    build: Builder
    confidence: float = 0.95


def _text(match: re.Match, group: str) -> str:
    return " ".join(str(match.group(group) or "").split()).strip()


def _name_intent(
    kind: str,
    name: str,
    text: str,
    *,
    requires: list[str] | None = None,
) -> MemoryIntent:
    missing = [item for item in (requires or []) if not name]
    return MemoryIntent(
        kind=kind,
        predicate="name",
        value={"name": name} if name else None,
        evidence_quote=text,
        requires=missing,
    )


def _entity_intent(
    kind: str,
    predicate: str,
    entity: str,
    text: str,
    *,
    polarity: str = "",
) -> MemoryIntent:
    value: dict[str, Any] = {"entity": entity} if entity else {}
    if polarity:
        value["polarity"] = polarity
    return MemoryIntent(
        kind=kind,
        predicate=predicate,
        value=value if entity else None,
        evidence_quote=text,
        requires=[] if entity else ["entity"],
    )


def _read_intent(
    kind: str,
    predicate: str,
    scope: str,
    text: str,
    *,
    confidence: float = 0.95,
    query: str = "",
) -> MemoryIntent:
    return MemoryIntent(
        kind=kind,
        predicate=predicate,
        scope=scope,
        query=query,
        evidence_quote=text,
        confidence=confidence,
    )


def _build_name_current(match: re.Match, text: str) -> MemoryIntent:
    return _read_intent("current", "name", "current", text)


def _build_possess_current(match: re.Match, text: str) -> MemoryIntent:
    return _read_intent("current", "has", "current", text)


def _clean_entity_query(value: str) -> str:
    entity = " ".join(str(value or "").split()).strip()
    return entity.strip(" 吗么呢嘛？?。.!！")


def _build_possess_entity_current(match: re.Match, text: str) -> MemoryIntent:
    entity = _clean_entity_query(_text(match, "entity"))
    return _read_intent("current", "has", "current", text, query=entity)


def _build_prefer_entity_current(match: re.Match, text: str) -> MemoryIntent:
    entity = _clean_entity_query(_text(match, "entity"))
    return _read_intent("current", "likes", "current", text, query=entity)


def _build_possess_named_entity(match: re.Match, text: str) -> MemoryIntent:
    entity = _text(match, "entity")
    name = _text(match, "name")
    assertions = []
    if entity:
        assertions.append(
            {
                "subject": "self",
                "predicate": "has",
                "value": {"entity": entity},
                "qualifiers": {},
                "evidence_quote": text,
            }
        )
    return MemoryIntent(
        kind="write",
        predicate="has",
        value={"entity": entity} if entity else None,
        assertions=assertions,
        evidence_quote=text,
        requires=[] if entity and name else [slot for slot, value in (("entity", entity), ("name", name)) if not value],
    )


def _build_previous_name(match: re.Match, text: str) -> MemoryIntent:
    return _read_intent("previous", "name", "previous", text)


def _build_previous_prefer(match: re.Match, text: str) -> MemoryIntent:
    return _read_intent("previous", "prefers", "previous", text)


def _build_previous_possess(match: re.Match, text: str) -> MemoryIntent:
    return _read_intent("previous", "has", "previous", text)


def _build_earliest_name(match: re.Match, text: str) -> MemoryIntent:
    return _read_intent("earliest", "name", "earliest", text)


def _build_timeline_name(match: re.Match, text: str) -> MemoryIntent:
    return _read_intent("timeline", "name", "timeline", text)


def _build_when_name(match: re.Match, text: str) -> MemoryIntent:
    name = _text(match, "name")
    if not name:
        return _read_intent("when", "name", "timeline", text, confidence=0.8)
    return MemoryIntent(
        kind="when",
        predicate="name",
        scope="timeline",
        identity={"name": name},
        evidence_quote=text,
        confidence=0.9,
    )


def _build_write_name(match: re.Match, text: str) -> MemoryIntent:
    return _name_intent("write", _text(match, "name"), text)


def _build_correct_with_old(match: re.Match, text: str) -> MemoryIntent:
    new = _text(match, "new")
    return _name_intent("correct", new, text, requires=["new"])


def _build_correct_no_new(match: re.Match, text: str) -> MemoryIntent:
    return _name_intent("correct", "", text, requires=["new"])


def _build_prefer(match: re.Match, text: str, polarity: str) -> MemoryIntent:
    entity = _text(match, "entity")
    predicate = {
        "like": "likes",
        "dislike": "dislikes",
        "avoid": "dislikes",
        "want": "wants",
    }[polarity]
    return _entity_intent("write", predicate, entity, text, polarity=polarity)


def _build_like(match: re.Match, text: str) -> MemoryIntent:
    return _build_prefer(match, text, "like")


def _build_dislike(match: re.Match, text: str) -> MemoryIntent:
    return _build_prefer(match, text, "dislike")


def _build_avoid(match: re.Match, text: str) -> MemoryIntent:
    return _build_prefer(match, text, "avoid")


def _build_want(match: re.Match, text: str) -> MemoryIntent:
    return _build_prefer(match, text, "want")


def _build_possess(match: re.Match, text: str) -> MemoryIntent:
    return _entity_intent("write", "has", _text(match, "entity"), text)


def _build_entity_like(match: re.Match, text: str, polarity: str) -> MemoryIntent:
    subject = _text(match, "subject")
    entity = _text(match, "entity")
    assertions = []
    if entity:
        assertions.append(
            {
                "subject": subject,
                "predicate": "likes",
                "value": {"entity": entity, "polarity": polarity},
                "qualifiers": {},
                "evidence_quote": text,
            }
        )
    return MemoryIntent(
        kind="write",
        predicate="likes",
        value={"entity": entity, "polarity": polarity} if entity else None,
        assertions=assertions,
        evidence_quote=text,
        requires=["entity"] if not entity else [],
    )


def _build_named_pet(match: re.Match, text: str) -> MemoryIntent:
    entity = _text(match, "species")
    name = _text(match, "name")
    assertions = []
    if entity:
        assertions.append(
            {
                "subject": "self",
                "predicate": "has",
                "value": {"entity": entity},
                "qualifiers": {},
                "evidence_quote": text,
            }
        )
    return MemoryIntent(
        kind="write",
        predicate="has",
        value={"entity": entity} if entity else None,
        assertions=assertions,
        evidence_quote=text,
        requires=(
            []
            if entity and name
            else [
                slot
                for slot, value in (("entity", entity), ("name", name))
                if not value
            ]
        ),
    )


def _build_forget_name(match: re.Match, text: str) -> MemoryIntent:
    return MemoryIntent(
        kind="forget",
        predicate="name",
        identity={},
        evidence_quote=text,
    )


def _build_decline(match: re.Match, text: str) -> MemoryIntent:
    return MemoryIntent(
        kind="decline",
        evidence_quote=text,
        confidence=0.9,
    )


def _build_remember_name(match: re.Match, text: str) -> MemoryIntent:
    return _name_intent("write", _text(match, "name"), text)


RULES: tuple[IntentRule, ...] = (
    # ── current ─────────────────────────────────────────────
    IntentRule("current", r"^(?:请问)?我(?:的)?名字(?:是|叫)(?:什么|啥)[?？]?$", _build_name_current),
    IntentRule("current", r"^我是谁[?？]?$", _build_name_current),
    IntentRule("current", r"^我(?:现在)?(?:叫|是)(?:什么|啥)(?:名字)?[?？]?$", _build_name_current),
    IntentRule("current", r"^我(?:现在)?有(?:什么|啥)(?:宠物|物品|东西)[?？]?$", _build_possess_current),
    IntentRule("current", r"^我(?:有没有|是否有)(?:一只|一个|一条|只|个)?(?P<entity>[^，。？?！!\s]+?)[?？]?$", _build_possess_entity_current),
    IntentRule("current", r"^我(?:现在)?(?:有|拥有|养了|养着|养)(?:一只|一个|一条|只|个)?(?P<entity>[^，。？?！!\s]+?)(?:没有|吗|么|没)[?？]?$", _build_possess_entity_current),
    IntentRule("current", r"^我(?:现在)?(?:喜欢|偏爱|想要|不喜欢|讨厌)(?P<entity>[^，。？?！!\s]+?)(?:吗|么)[?？]?$", _build_prefer_entity_current),
    # ── previous ────────────────────────────────────────────
    IntentRule("previous", r"^我(?:之前|以前|原来|先前)(?:叫|是)(?:什么|啥)(?:名字)?[?？]?$", _build_previous_name),
    IntentRule("previous", r"^(?:之前|以前)我(?:叫|是)(?:什么|啥)(?:名字)?(?:来着)?[?？]?$", _build_previous_name),
    IntentRule("previous", r"^我(?:之前|以前)的(?:名字|称呼)(?:呢)?[?？]?$", _build_previous_name),
    IntentRule("previous", r"^我(?:之前|以前)(?:喜欢|偏爱)(?:什么|啥)[?？]?$", _build_previous_prefer),
    IntentRule("previous", r"^我(?:之前|以前)(?:养|有)(?:过)?(?:什么|啥)[?？]?$", _build_previous_possess),
    # ── earliest ────────────────────────────────────────────
    IntentRule("earliest", r"^我(?:最早|一开始|最开始)(?:叫|是)(?:什么|啥)(?:名字)?[?？]?$", _build_earliest_name),
    IntentRule("earliest", r"^我(?:最早|一开始|最开始)的(?:名字|称呼)(?:呢)?[?？]?$", _build_earliest_name),
    # ── timeline ────────────────────────────────────────────
    IntentRule("timeline", r"^我(?:的)?名字(?:改过|变过|变更过)(?:几次|多少(?:次|回))[?？]?$", _build_timeline_name),
    IntentRule("timeline", r"^(?:我的)?名字(?:历史|变更|时间线)[?？]?$", _build_timeline_name),
    IntentRule("timeline", r"^我(?:改过|换过)几次名字[?？]?$", _build_timeline_name),
    IntentRule("timeline", r"^我(?:是)?(?:什么时候|何时)改(?:的)?名字[?？]?$", _build_timeline_name),
    # ── when ────────────────────────────────────────────────
    IntentRule("when", r"^(?:我叫|我是)(?P<name>[^，。？?！!\s]+?)是(?:什么时候|何时)(?:说|记|告诉)(?:的)?[?？]?$", _build_when_name),
    IntentRule("when", r"^(?:什么时候|何时)(?:说|记|告诉)(?:我)?(?:叫|是|的名字是)?(?P<name>[^，。？?！!\s]*)[?？]?$", _build_when_name),
    IntentRule("when", r"^我(?:是)?(?:什么时候|何时)(?:说|记|告诉)(?:我叫|我的名字|名字)?(?P<name>[^，。？?！!\s]*)[?？]?$", _build_when_name),
    # ── write / correct ─────────────────────────────────────
    IntentRule("write", r"^我(?:现在)?叫(?P<name>[^，。？?！!\s]+?)(?:了)?$", _build_write_name),
    IntentRule("write", r"^我(?:现在)?是(?P<name>[^，。？?！!\s]+?)(?:了)?$", _build_write_name),
    IntentRule("write", r"^我(?:的)?名字(?:是|叫)(?P<name>[^，。？?！!\s]+?)(?:了)?$", _build_write_name),
    IntentRule("correct", r"^我(?:现在)?不叫(?P<old>[^，。？?！!\s]+?)(?:了)?，?(?:我)?(?:现在)?(?:改)?叫(?P<new>[^，。？?！!\s]+?)$", _build_correct_with_old),
    IntentRule("correct", r"^我(?:现在)?不叫(?P<old>[^，。？?！!\s]+?)(?:了)?$", _build_correct_no_new),
    IntentRule("correct", r"^(?:把|将)?(?:我)?(?:的)?名字(?:改成|改为|改叫)(?P<new>[^，。？?！!\s]+?)$", _build_correct_with_old),
    IntentRule("correct", r"^(?:我)?(?:现在)?(?:改名|改叫|改称)(?:叫|为|成)?(?P<new>[^，。？?！!\s]+?)$", _build_correct_with_old),
    IntentRule("correct", r"^(?:我)?(?:以后|从今以后)就?叫(?P<new>[^，。？?！!\s]+?)(?:了)?$", _build_correct_with_old),
    IntentRule("write", r"^(?:请)?(?:帮我)?记住(?:我叫|我是|我的名字叫)?(?P<name>[^，。？?！!\s]+?)$", _build_remember_name),
    IntentRule("write", r"^(?:请)?(?:帮我)?记(?:一下|下来|下)(?:我叫|我是|我的名字叫)?(?P<name>[^，。？?！!\s]+?)$", _build_remember_name),
    IntentRule("write", r"^(?:请)?(?:帮我)?别忘了(?:我叫|我是|我的名字叫)?(?P<name>[^，。？?！!\s]+?)$", _build_remember_name),
    # ── preference ──────────────────────────────────────────
    IntentRule("write", r"^我(?:更)?(?:喜欢|偏爱)(?P<entity>[^，。？?！!\s]+?)$", _build_like),
    IntentRule("write", r"^我(?:现在|以后|今后)?(?:更)?不喜欢(?P<entity>[^，。？?！!\s]+?)$", _build_dislike),
    IntentRule("write", r"^我讨厌(?P<entity>[^，。？?！!\s]+?)$", _build_dislike),
    IntentRule("write", r"^我(?:想)?(?:避免|避开|不要|不想要)(?P<entity>[^，。？?！!\s]+?)$", _build_avoid),
    IntentRule("write", r"^我(?:现在)?想要(?P<entity>[^，。？?！!\s]+?)$", _build_want),
    # ── possession ──────────────────────────────────────────
    IntentRule("write", r"^我(?:还)?(?:有|养了|养着|养)(?:一只|一个|一条|只|个)?(?P<entity>[^，。？?！!\s]+?)[，,]?(?:它的|这个|那个)?(?:名字|名称|昵称|英文名)?(?:是|叫|为)(?P<name>[^，。？?！!\s]+?)$", _build_possess_named_entity),
    IntentRule("write", r"^我(?:还)?(?:有|养了|养着)(?:一只)?(?P<species>猫咪|猫|狗狗|狗)(?:叫|名字叫)(?P<name>[^，。？?！!\s]+?)$", _build_named_pet),
    IntentRule("write", r"^我(?:还)?(?:有|养了|养着)(?:一只)?(?:叫|名字叫)(?P<name>[^，。？?！!\s]+?)(?:的)?(?P<species>猫咪|猫|狗狗|狗)$", _build_named_pet),
    IntentRule("write", r"^我(?:还)?(?:有|养了|养)(?:一只|一个|一条|只|个)?(?P<entity>[^，。？?！!\s]+?)$", _build_possess),
    IntentRule("write", r"^(?P<subject>[^\u6211\uff0c\u3002\uff1f?\uff01!\s]+?)(?:\u559c\u6b22|\u504f\u7231)(?:\u5403|\u559d)?(?P<entity>[^\uff0c\u3002\uff1f?\uff01!\s]+?)$", lambda m, t: _build_entity_like(m, t, "like")),
    IntentRule("write", r"^(?P<subject>[^\u6211\uff0c\u3002\uff1f?\uff01!\s]+?)\u4e0d\u559c\u6b22(?P<entity>[^\uff0c\u3002\uff1f?\uff01!\s]+?)$", lambda m, t: _build_entity_like(m, t, "dislike")),
    # ── forget ──────────────────────────────────────────────
    IntentRule("forget", r"^(?:请)?(?:帮我)?忘了?我叫(?P<name>[^，。？?！!\s]+?)$", _build_forget_name),
    IntentRule("forget", r"^(?:请)?(?:帮我)?(?:忘掉|忘记|删掉|去掉|不要再记)我(?:的)?名字$", _build_forget_name),
    # ── decline (user declines to complete a clarification) ─
    IntentRule("decline", r"^(?:那)?(?:算了|先记着|先这样|就这样|不用了|不想说|不说了|随便吧)(?:吧|了)?$", _build_decline),
)


def compile_rules() -> tuple[tuple[str, re.Pattern, Builder, float], ...]:
    return tuple(
        (rule.kind, re.compile(rule.pattern), rule.build, rule.confidence)
        for rule in RULES
    )



