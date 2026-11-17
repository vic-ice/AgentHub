from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryGuardDecision:
    blocked: bool
    reason_code: str = ""


_READ_NEGATION_RE = re.compile(
    r"(?:不要|别|不准|禁止|无需|不用|不要真的).{0,12}"
    r"(?:读|读取|查|查询|看|访问|检索|使用).{0,12}"
    r"(?:记忆|长期记忆|名字|姓名|称呼|偏好|个人信息|memory)",
    re.IGNORECASE,
)
_METALINGUISTIC_RE = re.compile(
    r"(?:字符串|代码示例|示例|分类标签|不要真的查询|不要真的读取|literal|string)",
    re.IGNORECASE,
)
_WRITE_NEGATION_RE = re.compile(
    r"(?:不要再|别再|不要|别|不准|禁止|无需|不用).{0,8}"
    r"(?:(?<!忘)记(?:住|下|下来)?|保存|记录|写入|持久化|加入(?:长期)?记忆|"
    r"存储|存入|存下来|remember|save|store)",
    re.IGNORECASE,
)
_STOP_REMEMBER_RE = re.compile(
    r"(?:不要再|别再|不用再|无需再).{0,4}"
    r"(?:记(?:住|下|下来)?|保存|记录|保留).{0,16}"
    r"(?:记忆|长期记忆|名字|姓名|称呼|偏好|个人信息|memory)",
    re.IGNORECASE,
)
_NEGATED_FORGET_RE = re.compile(
    r"(?:不要|别|不准|禁止|无需|不用).{0,8}"
    r"(?:忘记|忘掉|忘了|删掉|删除|去掉|清除|forget|delete|remove).{0,16}"
    r"(?:记忆|长期记忆|名字|姓名|称呼|偏好|个人信息|这条|它|内容|资料|memory)",
    re.IGNORECASE,
)
_QUESTION_WRITE_SOURCE_RE = re.compile(
    r"(?:[?？]\s*$|(?:吗|么|是不是|是否|有没有|会不会|能不能)\s*[?？]?$)",
    re.IGNORECASE,
)
_UNCERTAIN_WRITE_SOURCE_RE = re.compile(
    r"(?:我觉得|感觉|好像|似乎|可能|也许|大概|应该|算是|不确定|不太确定|貌似)"
    r".{0,16}(?:我)?(?:有|养|叫|是|喜欢|不喜欢|想要|住在|从事)",
    re.IGNORECASE,
)
_REMEMBER_CUE_WHEN_FORGET_RE = re.compile(
    r"(?:别忘了|不要忘了|不要忘记).{0,10}"
    r"(?:我叫|我是|我的名字(?:叫|是))",
    re.IGNORECASE,
)


def guard_memory_read_query(text: str) -> MemoryGuardDecision:
    normalized = _normalize_text(text)
    if not normalized:
        return MemoryGuardDecision(False)
    if is_memory_metalinguistic(normalized):
        return MemoryGuardDecision(True, "memory_query_metalinguistic")
    if _READ_NEGATION_RE.search(normalized):
        return MemoryGuardDecision(True, "memory_read_forbidden_by_user")
    return MemoryGuardDecision(False)


def guard_memory_write_source(text: str) -> MemoryGuardDecision:
    normalized = _normalize_text(text)
    if not normalized:
        return MemoryGuardDecision(False)
    if is_memory_metalinguistic(normalized):
        return MemoryGuardDecision(True, "memory_write_metalinguistic")
    if _QUESTION_WRITE_SOURCE_RE.search(normalized):
        return MemoryGuardDecision(True, "memory_write_question_source")
    if _UNCERTAIN_WRITE_SOURCE_RE.search(normalized):
        return MemoryGuardDecision(True, "memory_write_uncertain_source")
    if _WRITE_NEGATION_RE.search(normalized):
        return MemoryGuardDecision(True, "memory_write_forbidden_by_user")
    return MemoryGuardDecision(False)


def guard_memory_forget_source(text: str) -> MemoryGuardDecision:
    normalized = _normalize_text(text)
    if not normalized:
        return MemoryGuardDecision(False)
    if is_memory_metalinguistic(normalized):
        return MemoryGuardDecision(True, "memory_forget_metalinguistic")
    if _REMEMBER_CUE_WHEN_FORGET_RE.search(normalized):
        return MemoryGuardDecision(True, "memory_forget_contradicted_by_remember_cue")
    if _NEGATED_FORGET_RE.search(normalized):
        return MemoryGuardDecision(True, "memory_forget_forbidden_by_user")
    return MemoryGuardDecision(False)


def is_memory_metalinguistic(text: str) -> bool:
    return bool(_METALINGUISTIC_RE.search(_normalize_text(text)))


def is_affirmative_forget_request(text: str) -> bool:
    return bool(_STOP_REMEMBER_RE.search(_normalize_text(text)))


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


__all__ = [
    "MemoryGuardDecision",
    "guard_memory_forget_source",
    "guard_memory_read_query",
    "guard_memory_write_source",
    "is_affirmative_forget_request",
    "is_memory_metalinguistic",
]

