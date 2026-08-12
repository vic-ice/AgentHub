from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.services.research.publication.publisher import publish_research_answer
from app.services.research.publication.synthesis import (
    synthesize_research_report,
    synthesize_research_report_deterministic,
)
from app.infra.llm.model_candidates import (
    record_model_failure,
    record_model_success,
)
from app.services.research.report import ResearchReport


logger = logging.getLogger(__name__)

RESEARCH_REPORT_WRITE_CONTRACT_VERSION = "research-report-write-v1"
# P95 of observed report-model calls ~17.9s (n=4, 11.6-18.0s); 45s keeps
# 2.5x headroom while bounding worst-case waits before deterministic fallback.
REPORT_TIMEOUT_SECONDS = 45
MAX_EVIDENCE = 12

_URL_RE = re.compile(r"https?://[^\s)\]>]+\S*", re.IGNORECASE)
_CITATION_RE = re.compile(r"\[(\d+)\]")
_FENCE_RE = re.compile(r"^```(?:markdown)?\s*|\s*```$", re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class ResearchReportWriteResult(BaseModel):
    """Goal-centered final report written from admitted evidence."""

    result_mode: str = "research_report_write"
    contract_version: str = RESEARCH_REPORT_WRITE_CONTRACT_VERSION
    run_id: UUID
    objective: str
    status: str = "fallback"
    provider: str = "deterministic"
    model_id: str = ""
    report_markdown: str = ""
    cited_source_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


async def write_research_report(
    report: ResearchReport | dict[str, Any],
    *,
    model_id: str = "",
    review: dict[str, Any] | None = None,
) -> ResearchReportWriteResult:
    """Write one report that answers the user's objective from evidence.

    One model call: the final answer is taken directly from the model's
    text output (extraction only remains as a parsing fallback for
    thinking-only providers). On model failure or empty output the
    deterministic source-backed renderer keeps the turn stable.
    """

    validated = ResearchReport.model_validate(report)
    language = "zh-CN" if re.search(r"[\u4e00-\u9fff]", validated.objective) else "en"
    evidence = _evidence_list(validated, limit=MAX_EVIDENCE)
    all_sources = _all_sources(validated)
    limitations = _limitations(validated)

    selected_model_id = ""
    attempts: list[dict[str, Any]] = []
    error = ""
    if evidence:
        selected_model_id = model_id or _resolve_model_id("")
        if selected_model_id:
            prompt = _prompt_freeform(
                objective=validated.objective,
                language=language,
                evidence=evidence,
                limitations=limitations,
                review=review,
            )
            try:
                rendered, cited_ids, response, body, attempt = (
                    await _generate_report_once(
                        model_id=selected_model_id,
                        prompt=prompt,
                        objective=validated.objective,
                        evidence=evidence,
                        sources=all_sources,
                        language=language,
                    )
                )
                attempts.append(attempt)
                if not attempt.get("ok"):
                    cause = _attempt_cause(str(attempt.get("error") or ""))
                    if cause:
                        record_model_failure(selected_model_id, cause)
                if rendered.strip():
                    record_model_success(selected_model_id)
                    return ResearchReportWriteResult(
                        run_id=validated.run_id,
                        objective=validated.objective,
                        status="synthesized",
                        provider="runtime_llm",
                        model_id=selected_model_id,
                        report_markdown=rendered,
                        cited_source_ids=cited_ids,
                        limitations=limitations,
                        metadata={
                            "external_call": True,
                            "sections": _sections(rendered),
                            "attempts": attempts,
                            "failure_cause": "success",
                        },
                    )
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                record_model_failure(selected_model_id, _attempt_cause(error))
                attempts.append(
                    {
                        "model_id": selected_model_id,
                        "ok": False,
                        "error": error[:200],
                        "duration_ms": 0,
                        "body_chars": 0,
                        "report_chars": 0,
                    }
                )
                logger.warning(
                    "report_writer failed model=%s error=%s",
                    selected_model_id,
                    error[:500],
                )
        if not attempts:
            error = "no model available for report writing"
        elif not error:
            error = "report writer returned no usable markdown"
    else:
        error = "no admitted evidence to synthesize"

    cause = classify_report_failure(attempts, error=error)
    fallback = _fallback_markdown(validated, language=language)
    return ResearchReportWriteResult(
        run_id=validated.run_id,
        objective=validated.objective,
        status="fallback",
        provider="deterministic",
        model_id=selected_model_id,
        report_markdown=fallback,
        cited_source_ids=[
            str(item["source_id"]) for item in evidence
        ],
        limitations=limitations,
        error=error[:500],
        metadata={
            "external_call": False,
            "attempts": attempts,
            "failure_cause": cause,
        },
    )


async def _generate_report_once(
    *,
    model_id: str,
    prompt: str,
    objective: str,
    evidence: list[dict[str, Any]],
    language: str,
    sources: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], Any, str, dict[str, Any]]:
    """One model call; take the final answer directly, keep an attempt log.

    The renderer only guards against empty/unusable extractions; content
    quality is the model's own job. Deterministic fallback happens outside
    this function, only when the model produced nothing usable.
    """

    from app.infra.llm import get_llm

    started = time.perf_counter()
    try:
        model = get_llm(model_id, thinking_mode=None)
        async with asyncio.timeout(REPORT_TIMEOUT_SECONDS):
            response = await model.ainvoke(prompt)
        final_text, thinking_text = _message_text_and_thinking(response)
        body = final_text or thinking_text
        if final_text.strip():
            rendered, cited_ids, ok = _render_final_deliverable(
                final_text,
                evidence=evidence,
                sources=sources,
                language=language,
            )
        else:
            rendered, cited_ids, ok = _render_freeform(
                thinking_text,
                objective=objective,
                evidence=evidence,
                sources=sources,
                language=language,
            )
        attempt = {
            "model_id": model_id,
            "thinking": None,
            "ok": ok,
            "error": "",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "body_chars": len(body or ""),
            "report_chars": len(rendered or ""),
        }
        return rendered, cited_ids, response, body, attempt
    except Exception as exc:
        attempt = {
            "model_id": model_id,
            "thinking": None,
            "ok": False,
            "error": (str(exc) or exc.__class__.__name__)[:200],
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "body_chars": 0,
            "report_chars": 0,
        }
        return "", [], None, "", attempt


def _render_freeform(
    body: str,
    *,
    objective: str,
    evidence: list[dict[str, Any]],
    language: str,
    sources: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], bool]:
    """Freeform narrative path: extract the report with a minimal usability gate.

    Content quality belongs to the LLM; this only guards against empty or
    placeholder-only extractions so a good narrative is never discarded.
    """

    extracted = _extract_report_body(body)
    if not _looks_like_report(extracted):
        return "", [], False
    rendered, cited_ids = _sanitize_body(
        extracted,
        evidence=evidence,
        sources=sources,
        language=language,
    )
    return rendered, cited_ids, bool(rendered.strip())


def _render_final_deliverable(
    body: str,
    *,
    evidence: list[dict[str, Any]],
    language: str,
    sources: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], bool]:
    """Clean the model's final answer directly (no thinking extraction)."""

    if not _looks_like_report(body):
        return "", [], False
    rendered, cited_ids = _sanitize_body(
        body,
        evidence=evidence,
        sources=sources,
        language=language,
    )
    return rendered, cited_ids, bool(rendered.strip())


MIN_USABLE_BODY_CHARS = 80


_PLACEHOLDER_BODIES = {
    "...",
    "...",
    "\u5f85\u8865\u5145",
    "todo",
    "tbd",
}


_META_TAIL_MARKERS = (
    "\u68c0\u67e5\u7ea6\u675f",
    "\u68c0\u67e5\u7ea6\u675f\u6761\u4ef6",
    "\u4fee\u6539\u540e\u7684\u6587\u672c",
    "\u7a0d\u4f5c\u4f18\u5316",
    "\u5fae\u8c03\u6587\u6848",
    "\u5fae\u8c03\u8bed\u8a00",
    "\u601d\u8003\u7ed3\u6784",
    "\u6700\u7ec8\u6587\u672c\u7ed3\u6784",
    "\u4ed4\u7ec6\u68c0\u67e5\u6bb5\u843d",
    "\u786e\u8ba4\u5f15\u7528\u6807\u8bb0",
    "\u6700\u540e\u68c0\u67e5",
    "\u6536\u5c3e\u68c0\u67e5",
    "\u68c0\u67e5\u5b8c\u6bd5",
    "\u76f4\u63a5\u8f93\u51fa",
    "\u5b8c\u6210\u3002",
    "Constraint check",
    "Revised",
    "Final answer",
)


_REPORT_ANCHORS = (
    "\u8d77\u8349\u62a5\u544a",
    "\u8d77\u8349\u5185\u5bb9",
    "\u8349\u7a3f",
    "\u62a5\u544a\u5982\u4e0b",
    "\u6700\u7ec8\u62a5\u544a",
    "\u4ee5\u4e0b\u662f\u62a5\u544a",
    "\u8f93\u51fa\u62a5\u544a",
    "\u6b63\u6587\u5982\u4e0b",
    "\u6b63\u6587\uff1a",
    "Final report",
    "Here is the report",
    "Report:",
)


def classify_report_failure(
    attempts: list[dict[str, Any]],
    *,
    error: str = "",
) -> str:
    """Classify the dominant reason the LLM path fell back."""

    if any(bool(item.get("ok")) for item in attempts):
        return "success"
    if not attempts:
        return "no_model"
    errors = " ".join(
        str(item.get("error") or "") for item in attempts
    ).lower()
    if any(
        token in errors
        for token in ("rate", "429", "quota", "\u989d\u5ea6", "limit exceeded")
    ):
        return "rate_limited"
    if any(token in errors for token in ("timeout", "timed out")):
        return "timeout"
    if any(token in errors for token in ("thinking", "enable_thinking")):
        return "model_config"
    if any(
        token in errors
        for token in ("not found", "auth", "401", "403", "api key")
    ):
        return "model_unavailable"
    if not any(int(item.get("body_chars") or 0) > 0 for item in attempts):
        return "empty_output"
    return "gate_rejected"


def _extract_report_body(text: str) -> str:
    """Slice the actual report out of thinking/planning drafts.

    Thinking-only models put the finished report inside their reasoning
    channel after a drafting marker. Prefer the anchored segment, then the
    last heading-led block (markdown or Chinese-numbered), and never drop
    the whole output when no heading structure exists.
    """

    source = str(text or "")
    if not source.strip():
        return ""
    for anchor in _REPORT_ANCHORS:
        index = source.find(anchor)
        if index < 0:
            continue
        tail_lines = source[index:].splitlines()
        for offset, line in enumerate(tail_lines[1:], start=1):
            if line.strip():
                return _cut_meta_tail(
                    "\n".join(tail_lines[offset:]).strip()
                )
    lines = source.splitlines()
    strong_start = None
    weak_start = None
    for index, line in enumerate(lines):
        if _strong_heading(line):
            strong_start = index
        elif _weak_heading(line):
            weak_start = index
    start = strong_start if strong_start is not None else weak_start
    if start is not None:
        rest = lines[start:]
        if sum(1 for line in rest if line.strip()) >= 3:
            return _cut_meta_tail("\n".join(rest).strip())
    return _cut_meta_tail(source.strip())


def _heading_line(line: str) -> bool:
    """Markdown or Chinese-numbered section heading line."""

    return _strong_heading(line) or _weak_heading(line)


def _strong_heading(line: str) -> bool:
    """Markdown or Chinese-numbered headings ? reliable section markers."""

    stripped = str(line or "").strip()
    if stripped.startswith(("# ", "## ", "### ")):
        return True
    for prefix in (
        "\u4e00\u3001",
        "\u4e8c\u3001",
        "\u4e09\u3001",
        "\u56db\u3001",
        "\u4e94\u3001",
        "\u516d\u3001",
        "\u4e03\u3001",
        "\u516b\u3001",
        "\u4e5d\u3001",
        "\u5341\u3001",
        "\uff08\u4e00\uff09",
        "\uff08\u4e8c\uff09",
        "\uff08\u4e09\uff09",
        "\uff08\u56db\uff09",
        "\uff08\u4e94\uff09",
    ):
        if stripped.startswith(prefix):
            return True
    return False


def _weak_heading(line: str) -> bool:
    """Plain numeric list-style heading ("1. xxx") ? fallback only."""

    stripped = str(line or "").strip()
    return (
        len(stripped) >= 3
        and stripped[0].isdigit()
        and stripped[1] in ".\u3001"
    )


def _cut_meta_tail(text: str) -> str:
    """Drop planning/self-check/closing notes trailing the report body."""

    lines = str(text or "").splitlines()
    cut = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if any(marker in stripped for marker in _META_TAIL_MARKERS):
            cut = index
            break
    return "\n".join(lines[:cut]).strip()


def _looks_like_report(text: str) -> bool:
    """Minimal usability gate: real content, not placeholder-only.

    Deliberately lenient: quality is judged by the LLM; this only blocks
    empty stubs and planning fragments from being published.
    """

    compact = " ".join(str(text or "").split())
    if len(compact) < MIN_USABLE_BODY_CHARS:
        return False
    return compact.casefold() not in _PLACEHOLDER_BODIES


def _extract_json_objects(text: str) -> list[str]:
    """Split balanced top-level JSON objects (string-aware)."""

    cleaned = str(text or "")
    objects: list[str] = []
    index = 0
    size = len(cleaned)
    while index < size:
        if cleaned[index] != "{":
            index += 1
            continue
        depth = 0
        in_string = False
        escaped = False
        cursor = index
        while cursor < size:
            char = cleaned[cursor]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            else:
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        objects.append(cleaned[index : cursor + 1])
                        break
            cursor += 1
        index = cursor + 1
    return objects


def _bounded(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text if len(text) <= limit else text[:limit].rstrip()


_FINAL_REPORT_INSTRUCTIONS_ZH = '研究已经完成。下面是已筛选、核验并整理好的研究成果。\n\n现在你是一位资深研究编辑，亲手完成这份最终交付：把它写成一篇直接面向用户的最终报告，让用户读完就知道该怎么选、怎么用。\n\n编辑原则：\n1. 有主见：直接给出你的判断和推荐立场，敢于说“最值得”“不建议”；观点放前面，事实作支撑，不要中立地罗列信息。\n2. 信息不要挤：一个要点一段，重要内容充分展开，次要内容一句带过；宁可少而精，敢于砍掉对用户帮助不大的内容。\n3. 结构不要齐：详略跟着内容重要性走，不要机械对齐；先讲什么、怎么组织，由内容和用户问题决定，不套固定模板。\n4. 重点敢取舍：突出真正重要的，敢排雷、敢舍弃；不为了“全面”把无关紧要的东西都搬上来。\n5. 保持专业度：不编造、不夸大证据能支持的结论；有冲突或不确定性时如实呈现；正文不写 URL；引用沿用输入中的 [n] 编号；保留名称、作者、数据等有价值的具体信息。\n6. 直接写最终正文，不输出分析过程、规划、检查过程、修改说明或任务复述。\n7. 善用 Markdown 排版元素（加粗、列表、表格、引用块）增强可读性，让用户扫一眼就能抓住重点；排版为可读性服务，不为了用而用。\n\n输出应像资深研究者完成研究后交付给用户的成品，而不是研究笔记、资料摘要或搜索结果目录。'


_FINAL_REPORT_INSTRUCTIONS_EN = 'The research is complete. Below is the screened, verified, and organized research material.\n\nNow you are a senior research editor delivering this final piece yourself: write it into a final report that directly serves the user, so that after reading it they know how to choose and how to use it.\n\nEditorial principles:\n1. Be opinionated: state your judgment and recommendation stance directly — dare to say "the most worthwhile" or "not recommended"; put the claim first, facts as support, do not neutrally list information.\n2. Do not cram information: one point per paragraph; expand important content fully and pass over minor content in one sentence; prefer fewer, better items and dare to cut what does not help the user.\n3. Do not force even structure: let depth follow importance, no mechanical alignment; what to cover first and how to organize is decided by the content and the user\'s question, not by a fixed template.\n4. Dare to make trade-offs: highlight what truly matters, flag what to avoid, and leave out what is not needed; do not include everything just for completeness.\n5. Stay professional: do not fabricate or overstate what the evidence supports; present conflicts and uncertainty honestly; no raw URLs in the body; keep [n] citations from the input; keep valuable specifics such as names, authors, and data.\n6. Write the final body directly; do not output analysis process, planning, checking, revision notes, or restate the task.\n7. Use Markdown formatting well (bold, lists, tables, blockquotes) to make the output scannable; formatting serves readability, never use it just for its own sake.\n\nOutput like an expert delivering finished work, not research notes, digests, or a search result listing.'


def _prompt_freeform(
    *,
    objective: str,
    language: str,
    evidence: list[dict[str, Any]],
    limitations: list[str],
    review: dict[str, Any] | None = None,
) -> str:
    """Final deliverable prompt: organize verified research into a report."""

    zh = language == "zh-CN"
    material_lines = [
        f"- [{index}] {_bounded(item.get('claim'), 220)}"
        f"（来源：{_bounded(item.get('source_title'), 80)}）"
        for index, item in enumerate(evidence, start=1)
    ]
    if limitations:
        material_lines.extend(["", "限制：" if zh else "Limitations:"])
        material_lines.extend(
            f"- {str(limit)[:200]}" for limit in limitations[:5]
        )
    source_lines = [
        f"{index}. {_bounded(item.get('source_title'), 80)} — {item.get('source_url')}"
        for index, item in enumerate(evidence, start=1)
    ]
    if zh:
        return (
            _FINAL_REPORT_INSTRUCTIONS_ZH
            + "\n\n用户原始问题：\n"
            + objective
            + "\n\n已整理研究成果：\n"
            + "\n".join(material_lines)
            + "\n\n来源：\n"
            + "\n".join(source_lines)
        )
    return (
        _FINAL_REPORT_INSTRUCTIONS_EN
        + "\n\nUser's original question:\n"
        + objective
        + "\n\nOrganized research material:\n"
        + "\n".join(material_lines)
        + "\n\nSources:\n"
        + "\n".join(source_lines)
    )


def _all_sources(report: ResearchReport) -> list[dict[str, Any]]:
    """Full deduplicated source list for the report's Sources section."""

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in report.sources:
        url = str(source.source_url or "").strip()
        if not url or url.lower() in seen:
            continue
        seen.add(url.lower())
        sources.append(
            {
                "source_id": str(source.evidence_id),
                "source_title": source.source_title,
                "source_url": url,
                "claim": source.claim,
            }
        )
        if len(sources) >= 20:
            break
    return sources


def _evidence_list(
    report: ResearchReport,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    sources_by_id = {
        str(source.evidence_id): source for source in report.sources
    }
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for decision in report.verified_claims:
        if not decision.publishable:
            continue
        for evidence_id in decision.evidence_ids:
            source = sources_by_id.get(str(evidence_id))
            if source is None or str(evidence_id) in seen:
                continue
            seen.add(str(evidence_id))
            evidence.append(
                {
                    "source_id": str(evidence_id),
                    "source_title": source.source_title,
                    "source_url": source.source_url,
                    "claim": decision.claim,
                    "quality": decision.quality,
                    "corroborated": decision.corroborated,
                    "published_date": source.published_date,
                }
            )
            if len(evidence) >= limit:
                return evidence
    return evidence


def _limitations(report: ResearchReport) -> list[str]:
    values: list[str] = []
    if report.uncertain_claims:
        values.append(
            f"{len(report.uncertain_claims)} 条\u8bc1\u636e\u56e0\u5f3a\u5ea6\u4e0d\u8db3\u672a\u4f5c\u4e3a\u7ed3\u8bba\u3002"
        )
    if report.rejected_claims:
        values.append(
            f"{len(report.rejected_claims)} 条\u5019\u9009\u8bc1\u636e\u672a\u901a\u8fc7\u53d1\u5e03\u8d28\u91cf\u68c0\u67e5\u3002"
        )
    values.extend(f"\u5f85\u8865\u8bc1\u636e\uff1a{gap}" for gap in report.gaps[:3])
    if not values:
        values.append(
            "\u73b0\u6709\u6765\u6e90\u672a\u901a\u8fc7\u76f8\u5173\u6027\u548c\u53ef\u53d1\u5e03\u6027\u68c0\u67e5\u3002"
        )
    return values[:5]


def _sanitize_body(
    body: str,
    *,
    evidence: list[dict[str, Any]],
    language: str,
    sources: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str]]:
    text = _FENCE_RE.sub("", body).strip()
    allowed = {
        item["source_id"]: index + 1
        for index, item in enumerate(evidence)
    }
    allowed_by_number = {index: source_id for source_id, index in allowed.items()}
    text = _URL_RE.sub("", text)
    cited_ids: list[str] = []
    for match in _CITATION_RE.finditer(text):
        source_id = allowed_by_number.get(int(match.group(1)))
        if source_id is not None and source_id not in cited_ids:
            cited_ids.append(source_id)
    lines = text.splitlines()
    cleaned_lines: list[str] = []
    for line in lines:
        if line.strip().startswith(("[1]", "[2]", "[3]", "[4]", "[5]", "[6]", "[7]", "[8]", "[9]")):
            continue
        cleaned_lines.append(line)
    body_text = "\n".join(cleaned_lines).strip()
    body_text = re.sub(r"\n{3,}", "\n\n", body_text)
    source_items = sources if sources is not None else evidence
    if body_text and source_items:
        zh = language == "zh-CN"
        source_lines: list[str] = []
        for index, item in enumerate(source_items, start=1):
            title = str(item.get("source_title") or item.get("source_url") or f"Source {index}")
            url = str(item.get("source_url") or "")
            line = f"{index}. [{title}]({url})" if url else f"{index}. {title}"
            source_lines.append(line)
        sources_label = "\u6765\u6e90" if zh else "Sources"
        body_text += f"\n\n## {sources_label}\n" + "\n".join(source_lines)
    return body_text, cited_ids


def _fallback_markdown(
    report: ResearchReport,
    *,
    language: str,
) -> str:
    synthesis = synthesize_research_report_deterministic(report)
    published = publish_research_answer(report, synthesis)
    return str(published.answer or "").strip()


def _sections(markdown: str) -> list[str]:
    return [
        line.strip().lstrip("#").strip()
        for line in markdown.splitlines()
        if line.strip().startswith("## ")
    ]


def _message_text_and_thinking(response: Any) -> tuple[str, str]:
    """Split the response into the final answer and the thinking content.

    DashScope thinking models put the final deliverable as a bare string at
    the end of ``content``, with reasoning in ``type="thinking"`` blocks and
    an empty ``type="text"`` block.
    """

    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip(), ""
    if isinstance(content, list):
        parts: list[str] = []
        thinking: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "text":
                text = str(item.get("text") or "")
                if text.strip():
                    parts.append(text)
            elif item_type == "thinking":
                thinking.append(str(item.get("thinking") or ""))
        return "".join(parts).strip(), "".join(thinking).strip()
    return str(content or "").strip(), ""


def _message_text(response: Any) -> str:
    """Extract the assistant's final answer; fall back to thinking."""

    final, thinking = _message_text_and_thinking(response)
    return final or thinking


def _attempt_cause(error: str) -> str:
    """Map an attempt error to a health cooldown cause."""

    lowered = str(error or "").lower()
    if any(token in lowered for token in ("rate", "429", "quota", "\u989d\u5ea6", "limit exceeded")):
        return "rate_limited"
    if any(token in lowered for token in ("timeout", "timed out")):
        return "timeout"
    if any(
        token in lowered
        for token in ("not found", "auth", "401", "403", "api key")
    ):
        return "model_unavailable"
    return ""


def _resolve_model_id(requested: str) -> str:
    if requested:
        return requested
    from app.infra.llm.manager import get_model_manager

    manager = get_model_manager()
    return manager.default_llm_id or manager.get_first_active_llm_id() or ""


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _workspace_from_review(review: dict[str, Any] | None) -> dict[str, Any]:
    """Project the final evolving workspace into the report prompt."""

    if not isinstance(review, dict):
        return {}
    metadata = review.get("metadata")
    workspace = metadata.get("evolving_report") if isinstance(metadata, dict) else None
    payload = workspace if isinstance(workspace, dict) else {}
    facts = payload.get("confirmed_facts") if isinstance(payload, dict) else None
    if not isinstance(facts, list):
        facts = []
    return {
        "objective": str(payload.get("objective") or "")[:300],
        "confirmed_facts": [
            {
                "content": str(item.get("content") or "")[:320],
                "confidence": str(item.get("confidence") or "medium"),
                "source": str(item.get("source") or "")[:300],
            }
            for item in facts[:5]
            if isinstance(item, dict)
        ],
        "information_gaps": [
            str(item)[:300]
            for item in (payload.get("information_gaps") or [])[:5]
        ],
        "conflicts": [
            str(item)[:300]
            for item in (payload.get("conflicts") or [])[:5]
        ],
        "missing_questions": [
            str(item)[:300]
            for item in (review.get("missing_questions") or [])[:5]
        ],
    }


__all__ = ["ResearchReportWriteResult", "write_research_report"]
