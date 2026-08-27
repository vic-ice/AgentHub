"""User-facing projection for the memory administration panel.

The version store keeps protocol-level schema keys and entity identifiers.
This module translates both legacy and current records into a small, stable UI
vocabulary without changing the authoritative stored facts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.services.memory.version_contracts import MemoryVersionRecord
from app.services.memory.current_projection import (
    collapse_semantic_current,
    semantic_history_for_key,
    semantic_name_slot,
)


_CATEGORY_LABELS = {
    "identity": "身份与称呼",
    "preference": "偏好",
    "possession": "物品与宠物",
    "relationship": "关系与名称",
    "agreement": "约定",
    "feedback": "反馈",
    "background": "能力与背景",
    "state": "状态",
    "fact": "其他事实",
    "reading": "阅读记忆",
}

_EDITABLE_PRESENTATION_KEYS = {
    "identity.self_reported_name",
    "identity.preferred_address",
    "identity.alias",
    "preference.entity",
    "possession.entity",
    "relationship.entity",
    "entity.name",
    "instruction.behavior",
    "feedback.outcome",
    "temporary.state",
}


@dataclass(frozen=True)
class MemoryAdminPresentation:
    presentation_key: str
    category_key: str
    category_label: str
    display_value: str
    can_edit: bool
    can_forget: bool
    show_evidence: bool


def collapse_current_versions(
    records: Iterable[MemoryVersionRecord],
) -> list[MemoryVersionRecord]:
    """Collapse compatibility rows that represent one current semantic slot.

    Older profile writes used personal.fact:name while corrections used
    personal.correction:name. They are distinct storage chains but one
    user-facing current name. Keep only the newest head in the administration
    view; history remains untouched in the version store.
    """

    return collapse_semantic_current(records)


def history_versions_for(
    records: Iterable[MemoryVersionRecord],
    *,
    memory_key: str,
) -> list[MemoryVersionRecord]:
    """Return the user-facing history for one semantic memory slot.

    Name corrections written by older releases used separate storage keys.
    The current panel collapses them into one fact, so its timeline must also
    include both compatibility chains.
    """

    values = list(records)
    selected = semantic_history_for_key(values, memory_key=memory_key)
    ordered = sorted(selected, key=lambda item: item.valid_from)
    if len({record.memory_key for record in ordered}) > 1:
        ordered = [
            (
                record.model_copy(update={"valid_to": ordered[index + 1].valid_from})
                if index < len(ordered) - 1 and record.valid_to is None
                else record
            )
            for index, record in enumerate(ordered)
        ]
    return ordered


def project_memory_record(
    record: MemoryVersionRecord,
    *,
    domain: str,
    kind: str,
) -> MemoryAdminPresentation:
    presentation_key = _presentation_key(record, domain=domain, kind=kind)
    category_key = _category_key(
        record,
        presentation_key=presentation_key,
        domain=domain,
        kind=kind,
    )
    editable = (
        not record.is_tombstone
        and
        presentation_key in _EDITABLE_PRESENTATION_KEYS
        and category_key != "reading"
    )
    forgettable = not record.is_tombstone and category_key != "reading"
    generated_admin_evidence = _text(record.evidence_quote).startswith(
        "用户在记忆中心"
    )
    return MemoryAdminPresentation(
        presentation_key=presentation_key,
        category_key=category_key,
        category_label=_CATEGORY_LABELS[category_key],
        display_value=_display_value(
            record,
            presentation_key=presentation_key,
            category_key=category_key,
        ),
        can_edit=editable,
        can_forget=forgettable,
        show_evidence=(
            category_key != "reading" and not generated_admin_evidence
        ),
    )


def _presentation_key(
    record: MemoryVersionRecord,
    *,
    domain: str,
    kind: str,
) -> str:
    schema_key = str(record.schema_key or "").strip()
    value = record.value
    if schema_key in _EDITABLE_PRESENTATION_KEYS:
        return schema_key
    name_slot = semantic_name_slot(record)
    if name_slot is not None and name_slot[0] == "entity_name":
        return "entity.name"
    if schema_key == "general.preference" or kind == "preference":
        return "preference.entity"
    if _is_self_name(record):
        return "identity.self_reported_name"
    if schema_key == "general.fact" and value.get("state"):
        return "temporary.state"
    if schema_key.startswith("reading.") or domain == "reading":
        return schema_key or f"reading.{kind}"
    return schema_key or f"{domain}.{kind}"


def _category_key(
    record: MemoryVersionRecord,
    *,
    presentation_key: str,
    domain: str,
    kind: str,
) -> str:
    if domain == "reading" or presentation_key.startswith("reading."):
        return "reading"
    if presentation_key.startswith("identity."):
        return "identity"
    if presentation_key.startswith("preference.") or kind == "preference":
        return "preference"
    if presentation_key.startswith("possession.") or domain == "possession":
        return "possession"
    if (
        presentation_key.startswith("relationship.")
        or presentation_key == "entity.name"
        or domain == "relationship"
    ):
        return "relationship"
    if presentation_key.startswith("instruction.") or kind == "agreement":
        return "agreement"
    if presentation_key.startswith("feedback.") or kind == "feedback":
        return "feedback"
    if (
        presentation_key == "temporary.state"
        and _text(record.value.get("state")) == "skill"
    ):
        return "background"
    if presentation_key.startswith("temporary.") or kind == "state":
        return "state"
    return "fact"


def _display_value(
    record: MemoryVersionRecord,
    *,
    presentation_key: str,
    category_key: str,
) -> str:
    if record.is_tombstone:
        return "遗忘此条记忆"
    value = record.value
    summary = _text(value.get("summary"))
    if category_key == "reading":
        title = _text(value.get("book_title") or value.get("entity"))
        if record.schema_key == "reading.state":
            status = {
                "want_to_read": "想读",
                "reading": "正在读",
                "read": "已读",
                "dropped": "已放弃",
            }.get(_text(value.get("reading_status")), "阅读状态")
            return _book_value(title, status)
        if record.schema_key == "reading.feedback":
            evaluation = {
                "liked": "喜欢",
                "neutral": "一般",
                "disliked": "不喜欢",
                "not_interested": "不感兴趣",
            }.get(_text(value.get("evaluation")), "阅读评价")
            return _book_value(title, evaluation)
    if presentation_key == "identity.self_reported_name":
        name = _text(value.get("name"))
        return f"当前名字：{name}" if name else summary
    if presentation_key == "identity.preferred_address":
        address = _text(value.get("address"))
        return f"希望被称为：{address}" if address else summary
    if presentation_key == "identity.alias":
        alias = _text(value.get("alias"))
        return f"别名：{alias}" if alias else summary
    if presentation_key == "entity.name":
        entity = _text(value.get("entity"))
        name = _text(value.get("name"))
        return f"{entity}叫{name}" if entity and name else summary
    if presentation_key == "preference.entity":
        if summary:
            return summary
        entity = _text(value.get("entity"))
        verb = {
            "like": "喜欢",
            "liked": "喜欢",
            "dislike": "不喜欢",
            "disliked": "不喜欢",
            "want": "想要",
            "avoid": "避免",
            "neutral": "关注",
        }.get(_text(value.get("polarity")), "关注")
        return f"{verb}{entity}" if entity else ""
    if presentation_key == "possession.entity":
        entity = _text(value.get("entity"))
        return f"拥有：{entity}" if entity else summary
    if presentation_key == "relationship.entity":
        entity = _text(value.get("entity"))
        relation = _text(value.get("relation"))
        return f"{entity}（{relation}）" if entity and relation else entity or summary
    if presentation_key == "instruction.behavior":
        return _text(value.get("instruction")) or summary
    if presentation_key == "feedback.outcome":
        target = _text(value.get("target"))
        outcome = _text(value.get("outcome"))
        return f"{target}：{outcome}" if target and outcome else target or outcome or summary
    if presentation_key == "temporary.state":
        state = _text(value.get("state"))
        state_label = {"skill": "技能"}.get(state, state)
        state_value = _text(value.get("value"))
        return (
            f"{state_label}：{state_value}"
            if state_label and state_value
            else summary or state_value or state_label
        )
    return summary or _text(record.evidence_quote) or _text(record.memory_key)


def _book_value(title: str, state: str) -> str:
    return f"《{title}》 · {state}" if title else state


def _is_self_name(record: MemoryVersionRecord) -> bool:
    if str(record.schema_key or "") == "entity.name":
        return False
    value = record.value
    predicate = _text(value.get("predicate") or record.predicate)
    subject = _text(record.subject).casefold()
    return (
        (
            str(record.schema_key or "") == "identity.self_reported_name"
            or predicate
            in {"name", "self_reported_name", "identity.self_reported_name"}
        )
        and bool(_text(value.get("name")))
        and subject in {"self", "user"}
    )


def _text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


__all__ = [
    "MemoryAdminPresentation",
    "collapse_current_versions",
    "history_versions_for",
    "project_memory_record",
]
