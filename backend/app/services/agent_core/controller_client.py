from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage

from app.infra.llm.factory import get_llm
from app.infra.llm.history import model_history_projector
from app.services.agent_core.capabilities import CapabilityRegistry
from app.services.agent_core.contracts import (
    ControllerOutput,
    ControllerToolCall,
)
from app.services.agent_core.prompt_composer import PromptComposer
from app.services.agent_core.prompt_contracts import ControllerModelRequest
from app.services.agent_core.task_plan_proposal import (
    ControllerTaskPlanProposal,
)
from app.utils.message import convert_message_content_to_string
from app.services.execution_progress import (
    report_completed_step,
    report_model_completion,
)


logger = logging.getLogger(__name__)


REQUEST_CLARIFICATION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "request_clarification",
        "description": (
            "Ask one concise question when required information is missing or "
            "a durable fact is ambiguous, incomplete, conflicting, or sensitive."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                }
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
}

PLAN_TASK_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "plan_task",
        "description": (
            "Propose one durable multi-step plan when the goal spans dependent "
            "steps, multiple rounds, clarification, or crash recovery. The "
            "application decides whether this creates or revises a task."
        ),
        "strict": True,
        "parameters": ControllerTaskPlanProposal.model_json_schema(),
    },
}


class ControllerClientError(ValueError):
    """Raised when a Controller response violates its contract."""


REQUIRED_SEARCH_DECISION_TIMEOUT_SECONDS = 60.0
SYNTHESIS_TIMEOUT_SECONDS = 90.0
SYNTHESIS_MAX_OUTPUT_TOKENS = 2200


class ControllerClient:
    """One model round. It proposes; it never executes."""

    def __init__(
        self,
        *,
        registry: CapabilityRegistry | None = None,
        prompt_composer: PromptComposer | None = None,
        model_factory=None,
    ) -> None:
        self._registry = registry or CapabilityRegistry()
        self._prompt_composer = prompt_composer or PromptComposer(self._registry)
        self._uses_default_model_factory = model_factory is None
        self._model_factory = model_factory or _default_model_factory

    async def decide(self, request: ControllerModelRequest) -> ControllerOutput:
        started = time.perf_counter()
        response: AIMessage | None = None
        required_search_fallback = self._required_search_output(request)
        step_id = f"model:controller:{request.phase}:{id(request)}"
        await report_completed_step(
            kind="model",
            status="waiting",
            title=(
                "正在组织最终回答"
                if request.phase == "synthesis"
                else "正在理解你的问题"
            ),
            detail="正在结合当前对话与可用能力进行处理",
            model_name=request.model_name,
            step_id=step_id,
        )
        try:
            model = (
                self._model_factory(
                    request.model_name,
                    thinking_mode=request.thinking_mode,
                )
                if self._uses_default_model_factory
                else self._model_factory(request.model_name)
            )
            if request.phase == "synthesis":
                # Synthesis is deliberately tool-free: semantic routing has
                # already happened and the signed receipts are the complete
                # evidence boundary for the final answer.
                runnable = (
                    model.bind(max_tokens=SYNTHESIS_MAX_OUTPUT_TOKENS)
                    if hasattr(model, "bind")
                    else model
                )
            else:
                bind_tools = getattr(model, "bind_tools", None)
                if not callable(bind_tools):
                    raise ControllerClientError("model does not expose bind_tools")
                schemas = controller_tool_schemas(self._registry)
                required_tool_name = (
                    required_search_fallback.tool_calls[0].name
                    if required_search_fallback is not None
                    else ""
                )
                bound_schemas = list(schemas)
                if required_tool_name:
                    bound_schemas = [
                        copy.deepcopy(schema)
                        for schema in bound_schemas
                        if str(
                            (schema.get("function") or {}).get("name") or ""
                        )
                        == required_tool_name
                    ]
                    if required_tool_name == "book_search":
                        for schema in bound_schemas:
                            parameters = (
                                schema.get("function", {}).get("parameters", {})
                            )
                            required_fields = list(parameters.get("required") or [])
                            if "evidence_strategy" not in required_fields:
                                required_fields.append("evidence_strategy")
                            parameters["required"] = required_fields
                            definitions = parameters.get("$defs") or {}
                            strategy_schema = definitions.get("EvidenceStrategy")
                            if isinstance(strategy_schema, dict):
                                strategy_schema["required"] = ["facets"]
                            facet_schema = definitions.get("EvidenceFacet")
                            if isinstance(facet_schema, dict):
                                facet_schema["required"] = [
                                    "name",
                                    "purpose",
                                    "query_terms",
                                    "preferred_source_types",
                                    "importance",
                                    "candidate_specific",
                                    "required_for_recommendation",
                                ]
                runnable = bind_tools(
                    bound_schemas,
                    # Several OpenAI-compatible upstreams reject
                    # tool_choice="required" while correctly honoring an explicit
                    # tool-only prompt with tool_choice="auto". The app still
                    # enforces the required capability after parsing, so provider
                    # syntax must not erase the model's semantic plan.
                    tool_choice="auto",
                )
            messages = model_history_projector.project(
                self._prompt_composer.compose(request)
            )
            timeout_seconds = float(request.timeout_seconds)
            if required_search_fallback is not None:
                timeout_seconds = min(
                    timeout_seconds,
                    REQUIRED_SEARCH_DECISION_TIMEOUT_SECONDS,
                )
            elif request.phase == "synthesis":
                timeout_seconds = min(
                    timeout_seconds,
                    SYNTHESIS_TIMEOUT_SECONDS,
                )
            response = (
                await _invoke_synthesis_model(
                    runnable,
                    list(messages),
                    timeout_seconds=timeout_seconds,
                )
                if request.phase == "synthesis"
                else await asyncio.wait_for(
                    runnable.ainvoke(list(messages)),
                    timeout=timeout_seconds,
                )
            )
            if not isinstance(response, AIMessage):
                raise ControllerClientError(
                    f"expected AIMessage, got {type(response).__name__}"
                )
            output = self.parse(response)
            output = self._enforce_required_search(request, output)
            if request.phase == "synthesis" and output.mode != "direct_answer":
                raise ControllerClientError(
                    "synthesis phase must return one direct answer"
                )
        except Exception as exc:
            await report_model_completion(
                response,
                title="\u6a21\u578b\u8c03\u7528\u5b8c\u6210",
                detail="Controller \u672a\u80fd\u5f62\u6210\u6709\u6548\u51b3\u7b56",
                model_name=request.model_name,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="failed",
                error=str(exc) or exc.__class__.__name__,
                step_id=step_id,
            )
            if required_search_fallback is not None:
                return required_search_fallback
            raise
        await report_model_completion(
            response,
            title="\u6a21\u578b\u8c03\u7528\u5b8c\u6210",
            detail=_decision_detail(output),
            model_name=request.model_name,
            duration_ms=int((time.perf_counter() - started) * 1000),
            step_id=step_id,
        )
        return output

    def _enforce_required_search(
        self,
        request: ControllerModelRequest,
        output: ControllerOutput,
    ) -> ControllerOutput:
        """Prevent a weak model from answering an explicit search request from memory."""

        if request.phase != "decision":
            return output
        fallback = self._required_search_output(request)
        if fallback is None:
            return output
        if output.mode == "direct_answer":
            return fallback
        if output.mode != "capability_proposals":
            return output

        required_call = fallback.tool_calls[0]
        calls = list(output.tool_calls)
        for index, call in enumerate(calls):
            if call.name != required_call.name:
                continue
            merged_call = call.model_copy(
                update={
                    "arguments": _merge_required_search_arguments(
                        capability=required_call.name,
                        proposed=call.arguments,
                        required=required_call.arguments,
                    )
                }
            )
            if required_call.name == "book_search":
                # RecommendationService already owns Shelf exclusion and
                # personalization. A parallel bookshelf_read is redundant and,
                # worse, could survive while a malformed model-authored search
                # is silently dropped by validation. Validate the enriched
                # proposal here and fall back to the minimal safe request if
                # the model supplied an invalid nested strategy.
                spec = self._registry.get("book_search")
                try:
                    if spec is None:
                        raise ValueError("book_search unavailable")
                    spec.input_model.model_validate(merged_call.arguments)
                except Exception as strategy_error:
                    # Preserve otherwise valid model-authored candidates and
                    # filters when only the optional nested strategy is
                    # malformed. This keeps the single semantic understanding
                    # useful while letting enrichment fail open to its generic
                    # facet instead of discarding the entire proposal.
                    without_strategy = dict(merged_call.arguments)
                    without_strategy.pop("evidence_strategy", None)
                    logger.warning(
                        "book_search evidence strategy rejected; preserving "
                        "other semantic fields error=%s strategy=%s",
                        str(strategy_error)[:300],
                        json.dumps(
                            merged_call.arguments.get("evidence_strategy"),
                            ensure_ascii=False,
                        )[:1200],
                    )
                    try:
                        if spec is None:
                            raise ValueError("book_search unavailable")
                        spec.input_model.model_validate(without_strategy)
                    except Exception:
                        return fallback
                    merged_call = merged_call.model_copy(
                        update={"arguments": without_strategy}
                    )
                return output.model_copy(update={"tool_calls": [merged_call]})
            calls[index] = merged_call
            return output.model_copy(update={"tool_calls": calls})

        # A capability proposal for the wrong owner is no safer than a direct
        # answer.  Keep the required-search fallback as the stable boundary.
        return fallback

    def _required_search_output(
        self,
        request: ControllerModelRequest,
    ) -> ControllerOutput | None:
        if request.phase != "decision":
            return None
        required = _required_search_capability(request.current_user_message)
        if required is None:
            return None
        capability, arguments = required
        spec = self._registry.get(capability)
        if spec is None or not spec.enabled:
            return None
        return ControllerOutput(
            mode="capability_proposals",
            progress_text="已识别为需要外部检索的请求，正在获取可核验结果。",
            tool_calls=[
                ControllerToolCall(
                    call_id=f"policy-{capability}",
                    name=capability,
                    arguments=arguments,
                )
            ],
        )

    def parse(self, response: AIMessage) -> ControllerOutput:
        content = convert_message_content_to_string(response.content).strip()
        calls = _extract_calls(response)
        if not calls:
            if not content:
                raise ControllerClientError("empty direct Controller response")
            return ControllerOutput(mode="direct_answer", text=content)

        clarification = [
            call for call in calls if call.name == "request_clarification"
        ]
        if clarification:
            if len(calls) != 1:
                raise ControllerClientError(
                    "clarification cannot be mixed with capability proposals"
                )
            arguments = clarification[0].arguments
            question = arguments.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ControllerClientError("clarification question is missing")
            if set(arguments) != {"question"}:
                raise ControllerClientError(
                    "clarification contains unexpected arguments"
                )
            return ControllerOutput(
                mode="request_clarification",
                text=question.strip(),
            )

        task_planning = [
            call for call in calls if call.name == "plan_task"
        ]
        if task_planning:
            if len(calls) != 1:
                raise ControllerClientError(
                    "task planning cannot be mixed with capability proposals"
                )
            try:
                proposal = ControllerTaskPlanProposal.model_validate(
                    task_planning[0].arguments
                )
            except Exception as exc:
                raise ControllerClientError(
                    "task plan violates the Controller proposal contract"
                ) from exc
            return ControllerOutput(
                mode="task_plan_proposal",
                progress_text=content,
                task_plan_proposal=proposal.to_task_plan_draft(),
            )

        return ControllerOutput(
            mode="capability_proposals",
            progress_text=content,
            tool_calls=calls,
        )


async def _invoke_synthesis_model(
    runnable,
    messages,
    *,
    timeout_seconds: float,
) -> AIMessage:
    """Stream one final Search answer and retain received text on timeout."""

    if not hasattr(runnable, "astream"):
        return await asyncio.wait_for(
            runnable.ainvoke(messages),
            timeout=timeout_seconds,
        )

    accumulated = None
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in runnable.astream(messages):
                accumulated = (
                    chunk if accumulated is None else accumulated + chunk
                )
    except TimeoutError:
        if accumulated is None or not str(
            getattr(accumulated, "content", "") or ""
        ).strip():
            raise
    if accumulated is None:
        raise ControllerClientError("synthesis model returned no content")
    if isinstance(accumulated, AIMessage):
        return accumulated
    return AIMessage(
        content=getattr(accumulated, "content", ""),
        response_metadata=dict(
            getattr(accumulated, "response_metadata", {}) or {}
        ),
        usage_metadata=getattr(accumulated, "usage_metadata", None),
    )


def _decision_detail(output: ControllerOutput) -> str:
    if output.mode == "direct_answer":
        return "\u6a21\u578b\u5df2\u751f\u6210\u6700\u7ec8\u56de\u590d"
    if output.mode == "request_clarification":
        return "\u6a21\u578b\u9700\u8981\u7528\u6237\u8865\u5145\u4fe1\u606f"
    if output.mode == "task_plan_proposal":
        return "\u6a21\u578b\u5df2\u751f\u6210\u4efb\u52a1\u8ba1\u5212"
    return f"\u6a21\u578b\u5df2\u89c4\u5212 {len(output.tool_calls)} \u4e2a\u6267\u884c\u52a8\u4f5c"

_EXPLICIT_SEARCH_RE = re.compile(
    r"(?:搜一下|搜索|检索|查一下|查找|联网查|上网查|帮我搜|请搜)"
    r"|\b(?:search|look\s+up|browse)\b",
    re.IGNORECASE,
)
_BOOK_REQUEST_RE = re.compile(
    r"(?:书籍|图书|读物|书单|几本书|一本书|推荐书|入门书|好书|教材|小说|著作"
    r"|(?:[一二三四五六七八九十百两几\d]+本).{0,24}书|《[^》]{1,100}》)"
    r"|\bbooks?\b",
    re.IGNORECASE,
)
_BOOK_RECOMMENDATION_RE = re.compile(
    r"(?:推荐|书单|适合.{0,12}(?:读|看))"
    r"|\brecommend(?:ation|ed)?\b",
    re.IGNORECASE,
)


def _required_search_capability(
    message: str,
) -> tuple[str, dict[str, Any]] | None:
    from app.services.research.search_policy import (
        is_book_recommendation_request,
        is_book_request,
    )

    text = " ".join(str(message or "").split()).strip()
    if not text:
        return None
    is_book = (
        is_book_request(text)
        or _BOOK_REQUEST_RE.search(text) is not None
    )
    is_recommendation = (
        is_book_recommendation_request(text)
        or _BOOK_RECOMMENDATION_RE.search(text) is not None
    )
    if is_book and (is_recommendation or _EXPLICIT_SEARCH_RE.search(text)):
        return (
            "book_search",
            _book_search_fallback_arguments(
                text,
                is_recommendation=is_recommendation,
            ),
        )
    if _EXPLICIT_SEARCH_RE.search(text):
        return "web_search", {"query": text[:300]}
    return None


def _book_search_fallback_arguments(
    text: str,
    *,
    is_recommendation: bool,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "query": text[:300],
        "mode": "recommendation" if is_recommendation else "lookup",
    }
    if not is_recommendation:
        return arguments
    references = list(
        dict.fromkeys(
            title.strip()
            for title in re.findall(r"《([^》]{1,100})》", text)
            if title.strip()
        )
    )[:20]
    if references:
        arguments["reference_titles"] = references
        arguments["excluded_titles"] = list(references)
        arguments["query"] = (
            "类似"
            + "、".join(f"《{title}》" for title in references)
            + "的书 推荐"
        )[:300]
    limit_match = re.search(
        r"(?:推荐|给我|找|列出|选出)?\s*"
        r"([一二三四五六七八九十两\d]{1,3})\s*本",
        text,
    )
    if limit_match:
        limit = _small_chinese_number(limit_match.group(1))
        if limit is not None:
            arguments["limit"] = max(1, min(limit, 10))
    if re.search(r"(?:详细|深入|深度|全面|逐本)", text):
        arguments["response_depth"] = "deep"
    elif re.search(r"(?:简短|精简|只列|只要书名)", text):
        arguments["response_depth"] = "quick"
    years = _explicit_publication_years(text)
    if years:
        arguments["publication_year_from"] = min(years)
        arguments["publication_year_to"] = max(years)
    return arguments


def _merge_required_search_arguments(
    *,
    capability: str,
    proposed: Mapping[str, Any],
    required: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge deterministic safety fields into a model-authored proposal."""

    merged = dict(proposed)
    if capability != "book_search":
        if not str(merged.get("query") or "").strip():
            merged["query"] = required.get("query", "")
        return merged

    if not str(merged.get("query") or "").strip():
        merged["query"] = required.get("query", "")
    merged["mode"] = required.get("mode", merged.get("mode", "recommendation"))

    list_limits = {
        "themes": 6,
        "genres": 10,
        "authors": 10,
        "candidate_titles": 10,
        "reference_titles": 20,
        "excluded_titles": 40,
    }
    for field, limit in list_limits.items():
        proposed_values = merged.get(field)
        required_values = required.get(field)
        proposed_list = (
            proposed_values if isinstance(proposed_values, list) else []
        )
        required_list = (
            required_values if isinstance(required_values, list) else []
        )
        ordered_values = (
            [*required_list, *proposed_list]
            if field in {"themes", "reference_titles", "excluded_titles"}
            else [*proposed_list, *required_list]
        )
        values = [
            " ".join(str(item or "").split()).strip()
            for item in ordered_values
        ]
        values = list(dict.fromkeys(item for item in values if item))[:limit]
        if values or field in merged or field in required:
            merged[field] = values

    # If the user supplied comparison books but no explicit subject theme, the
    # examples themselves are the highest-fidelity retrieval anchors. Prevent
    # a model from replacing them with speculative umbrella categories.
    if (
        required.get("reference_titles")
        and str(required.get("query") or "").strip()
    ):
        merged["themes"] = []
        merged["query"] = required["query"]

    # These fallback fields only exist when the user stated them explicitly,
    # so they take precedence over a model's lossy interpretation.
    for field in (
        "limit",
        "response_depth",
        "publication_year_from",
        "publication_year_to",
    ):
        if field in required:
            merged[field] = required[field]
    return merged


def _small_chinese_number(value: str) -> int | None:
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if text in digits:
        return digits[text]
    if "十" in text:
        left, right = text.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _explicit_publication_years(text: str) -> list[int]:
    matches: list[str] = []
    patterns = (
        r"(?:出版(?:于|年份|时间)?|published\s+(?:in|between)?)"
        r"[^0-9]{0,10}(20\d{2})(?:[^0-9]{1,8}(20\d{2}))?",
        r"(20\d{2})\s*年?.{0,5}(?:至|到|-|—)\s*"
        r"(20\d{2})\s*年?.{0,5}(?:出版|新出版)",
        r"(20\d{2})\s*年.{0,5}(?:出版|新出版)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            matches.extend(group for group in match.groups() if group)
    return sorted({int(value) for value in matches})


def _default_model_factory(
    model_name: str,
    *,
    thinking_mode: bool = False,
):
    return get_llm(model_name, thinking_mode=thinking_mode)


def controller_tool_schemas(
    registry: CapabilityRegistry,
) -> tuple[dict[str, Any], ...]:
    """Project only independently enabled model-facing controls."""

    schemas = list(registry.tool_schemas())
    if registry.task_planning_enabled:
        schemas.append(PLAN_TASK_TOOL)
    schemas.append(REQUEST_CLARIFICATION_TOOL)
    return tuple(schemas)


def _extract_calls(response: AIMessage) -> list[ControllerToolCall]:
    raw_calls = list(response.tool_calls or [])
    if not raw_calls:
        raw_calls = _openai_calls(response.additional_kwargs.get("tool_calls"))
    calls: list[ControllerToolCall] = []
    for raw in raw_calls:
        if not isinstance(raw, Mapping):
            raise ControllerClientError("tool call is not an object")
        call_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not call_id or not name:
            raise ControllerClientError("tool call id and name are required")
        calls.append(
            ControllerToolCall(
                call_id=call_id,
                name=name,
                arguments=_arguments(raw.get("args")),
            )
        )
    return calls


def _openai_calls(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    calls: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        function = raw.get("function")
        if not isinstance(function, Mapping):
            continue
        calls.append(
            {
                "id": raw.get("id"),
                "name": function.get("name"),
                "args": function.get("arguments"),
            }
        )
    return calls


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ControllerClientError("tool arguments are invalid JSON") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ControllerClientError("tool arguments must be an object")


__all__ = [
    "PLAN_TASK_TOOL",
    "ControllerClient",
    "ControllerClientError",
    "REQUEST_CLARIFICATION_TOOL",
    "controller_tool_schemas",
]
