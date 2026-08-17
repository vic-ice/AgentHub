from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from app.infra.llm import get_llm
from app.services.research.publication.contracts import ResearchBrief
from app.services.research.publication.provider import ResearchSynthesisRequest
from app.services.execution_progress import report_model_completion


SYNTHESIS_TIMEOUT_SECONDS = 45


class LLMResearchSynthesisProvider:
    """Schema-validated prose synthesis over admitted evidence only."""

    name = "runtime_llm"

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    async def synthesize(
        self,
        request: ResearchSynthesisRequest,
    ) -> ResearchBrief:
        model = get_llm(self.model_id)
        prompt = _prompt(request)
        started = time.perf_counter()
        response = None
        try:
            async with asyncio.timeout(SYNTHESIS_TIMEOUT_SECONDS):
                response = await model.ainvoke(prompt)
        except Exception as exc:
            await report_model_completion(
                response,
                title="\u7814\u7a76\u7efc\u5408\u6a21\u578b\u8c03\u7528\u5b8c\u6210",
                detail="\u7814\u7a76\u7efc\u5408\u5931\u8d25",
                model_name=self.model_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="failed",
                error=str(exc) or exc.__class__.__name__,
            )
            raise
        await report_model_completion(
            response,
            title="\u7814\u7a76\u7efc\u5408\u6a21\u578b\u8c03\u7528\u5b8c\u6210",
            detail="\u5df2\u6839\u636e\u5df2\u63a5\u7eb3\u8bc1\u636e\u751f\u6210\u7efc\u5408\u7ed3\u679c",
            model_name=self.model_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return ResearchBrief.model_validate(_json_payload(_message_text(response)))

def _prompt(request: ResearchSynthesisRequest) -> str:
    schema = ResearchBrief.model_json_schema()
    evidence = [item.model_dump(mode="json") for item in request.evidence]
    language_rule = (
        "Write every user-facing field in Simplified Chinese."
        if request.language == "zh-CN"
        else "Write every user-facing field in English."
    )
    return (
        "You synthesize a concise research brief. You cannot call tools and "
        "must use only the admitted evidence supplied below. "
        f"{language_rule}\n"
        "Return one JSON object only, with no Markdown fences or explanation. "
        "It must validate against the supplied JSON Schema. Use at most five "
        "findings. Every finding must cite one or more exact source_id values "
        "from the evidence. Do not invent titles, ratings, publication dates, "
        "consensus, or facts. Do not copy long source passages. If one source "
        "only mentions an item, describe it as that source's statement rather "
        "than independent consensus. Preserve uncertainty in caveat.\n\n"
        f"Objective: {request.objective}\n"
        f"Known limitations: {json.dumps(request.limitations, ensure_ascii=False)}\n"
        f"Admitted evidence: {json.dumps(evidence, ensure_ascii=False)}\n"
        f"JSON Schema: {json.dumps(schema, ensure_ascii=False)}"
    )


def _message_text(response: Any) -> str:
    """Extract assistant text; fall back to thinking blocks.

    Some thinking-configured providers return the whole answer inside
    content parts of type "thinking" with an empty final text part.
    """

    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        thinking: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "text":
                text = str(item.get("text") or "")
                if text.strip():
                    parts.append(text)
            elif item_type == "thinking":
                thinking.append(str(item.get("thinking") or ""))
        if parts:
            return "".join(parts).strip()
        if thinking:
            return "".join(thinking).strip()
        return ""
    return str(content or "").strip()


def _json_payload(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("research synthesis must return a JSON object")
    return payload
