"""Small, user-facing contracts shared by Search and Deep Research.

The research and retrieval runtimes own discovery, verification and stopping.
This module owns only the description of the answer the user should receive.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


AnswerDepth = Literal["quick", "balanced", "deep"]
AnswerStyle = Literal["conversational", "formal_report"]
TemporalIntent = Literal[
    "unspecified",
    "decision_horizon",
    "publication_recency",
]


class UserAnswerBrief(BaseModel):
    """Stable writing contract whose vocabulary is about the user, not the run."""

    model_config = ConfigDict(extra="forbid")

    original_request: str
    task_type: str = "general_research"
    primary_goal: str
    decision_dimensions: list[str] = Field(default_factory=list)
    user_constraints: list[str] = Field(default_factory=list)
    critical_questions: list[str] = Field(default_factory=list)
    temporal_intent: TemporalIntent = "unspecified"
    answer_depth: AnswerDepth = "balanced"
    response_style: AnswerStyle = "conversational"
    preferred_structure: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    language: str = "zh-CN"

    @field_validator(
        "decision_dimensions",
        "user_constraints",
        "critical_questions",
        "preferred_structure",
        "success_criteria",
        mode="before",
    )
    @classmethod
    def clean_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = " ".join(str(item or "").split()).strip()
            if text and text not in cleaned:
                cleaned.append(text[:200])
        return cleaned[:10]


def build_user_answer_brief(
    request: str,
    *,
    task_type: str = "general_research",
    decision_dimensions: list[str] | None = None,
    critical_questions: list[str] | None = None,
    answer_depth: str = "balanced",
    response_style: str = "conversational",
    user_constraints: list[str] | None = None,
) -> UserAnswerBrief:
    """Compile a conservative answer brief without inventing user preferences."""

    original = " ".join(str(request or "").split()).strip()
    language = "zh-CN" if re.search(r"[\u4e00-\u9fff]", original) else "en"
    depth: AnswerDepth = (
        answer_depth
        if answer_depth in {"quick", "balanced", "deep"}
        else "balanced"
    )
    style: AnswerStyle = (
        response_style
        if response_style in {"conversational", "formal_report"}
        else "conversational"
    )
    recommendation = "recommendation" in task_type
    fact_check = "fact_check" in task_type or "lookup" in task_type
    route_requested = bool(
        re.search(
            r"(?:顺序|路线|路径|怎么读|如何读|行动计划|下一步|"
            r"reading\s+order|roadmap|action\s+plan|next\s+steps?)",
            original,
            flags=re.IGNORECASE,
        )
    )
    table_requested = bool(
        re.search(r"(?:表格|列表|对比表|table|comparison\s+table)", original, re.I)
    )
    temporal_intent = _temporal_intent(
        original,
        recommendation=recommendation,
    )
    if language == "zh-CN":
        preferred = ["先给直接结论"]
        if recommendation:
            preferred.append("围绕选择条件比较候选及取舍")
        elif fact_check:
            preferred.append("区分已确认事实与仍不确定之处")
        else:
            preferred.append("按重要性综合关键发现及其影响")
        if table_requested:
            preferred.append("在表格确实更清楚时使用一张主表")
        if route_requested:
            preferred.append("给出可执行的顺序或下一步")
        success = [
            "开头直接回应用户的核心问题",
            "不是逐条复述材料，而是形成有证据支撑的综合判断",
            "重要事实可追溯，编辑判断与来源事实清楚区分",
            "只说明会改变结论或行动的不确定性",
            "研究证据面不能被改写成用户没有提出的筛选条件",
        ]
    else:
        preferred = ["lead with the direct answer"]
        if recommendation:
            preferred.append("compare choices around decision conditions and trade-offs")
        elif fact_check:
            preferred.append("separate confirmed facts from material uncertainty")
        else:
            preferred.append("synthesize findings and implications by importance")
        if table_requested:
            preferred.append("use one main table only when it improves comparison")
        if route_requested:
            preferred.append("provide an actionable sequence or next step")
        success = [
            "answer the core question at the start",
            "synthesize evidence instead of listing source summaries",
            "separate editorial judgment from sourced fact",
            "mention only uncertainty that changes the conclusion or action",
            "never turn a research facet into a constraint the user did not state",
        ]
    return UserAnswerBrief(
        original_request=original,
        task_type=task_type or "general_research",
        primary_goal=original,
        decision_dimensions=list(decision_dimensions or []),
        user_constraints=list(user_constraints or []),
        critical_questions=list(critical_questions or []),
        temporal_intent=temporal_intent,
        answer_depth=depth,
        response_style=style,
        preferred_structure=preferred,
        success_criteria=success,
        language=language,
    )


def _temporal_intent(
    request: str,
    *,
    recommendation: bool,
) -> TemporalIntent:
    value = str(request or "")
    if re.search(
        r"(?:最新出版|新出版|刚出版|当年出版|近\s*[一二两三四五六七八九十\d]+\s*年出版|"
        r"\d{4}\s*年(?:出版|发布|上市)|出版(?:于|在)?\s*\d{4}\s*年|"
        r"newly\s+published|published\s+in\s+\d{4}|latest\s+releases?)",
        value,
        flags=re.IGNORECASE,
    ):
        return "publication_recency"
    if recommendation and re.search(r"(?:19|20)\d{2}", value):
        return "decision_horizon"
    return "unspecified"


__all__ = ["UserAnswerBrief", "build_user_answer_brief"]
