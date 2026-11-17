from __future__ import annotations

from typing import Any

from app.services.agent_core.contracts import PublishedAnswer


MAX_SOURCES = 10


def merge_research_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge every confirmed research report into one source-backed output.

    Sources are de-duplicated by URL (the first attributable copy wins, with
    title/snippet enriched from later copies). Findings are de-duplicated by
    normalized text. The merged output keeps the same shape as a single
    ``research_report_v1`` action so renderers stay single-purpose.
    """

    seen_urls: dict[str, dict[str, Any]] = {}
    findings: list[str] = []
    seen_findings: set[str] = set()
    limitations: list[str] = []
    objective = ""

    for report in reports:
        if not isinstance(report, dict):
            continue
        objective = objective or str(report.get("objective") or "").strip()
        for item in report.get("sources", []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            current = seen_urls.get(url)
            if current is None:
                seen_urls[url] = dict(item)
                continue
            for key in ("title", "snippet", "published_date"):
                if not str(current.get(key) or "").strip() and str(
                    item.get(key) or ""
                ).strip():
                    current[key] = item[key]
        for item in report.get("findings", []):
            text = " ".join(str(item or "").split()).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen_findings:
                continue
            seen_findings.add(key)
            findings.append(text)
        for item in report.get("limitations", []):
            text = " ".join(str(item or "").split()).strip()
            if text and text not in limitations:
                limitations.append(text)

    merged: dict[str, Any] = {
        "objective": objective,
        "sources": list(seen_urls.values()),
        "findings": findings,
    }
    if limitations:
        merged["limitations"] = limitations
    return merged


def research_terminal_answer(
    output: dict[str, Any],
    *,
    receipt_refs: list[str] | None = None,
) -> PublishedAnswer:
    """Publish one deterministic, source-backed research answer.

    The research workflow's report step is already deterministic evidence
    (admitted snippets + sources); the controller is not asked to re-synthesize
    in a new round, which keeps the turn bounded and model-independent.
    """

    sources = [
        item
        for item in output.get("sources", [])
        if isinstance(item, dict)
    ]
    findings = [
        str(item).strip()
        for item in output.get("findings", [])
        if str(item).strip()
    ]
    if not sources and not findings:
        return PublishedAnswer(
            status="failed",
            content=(
                "深度检索未获取到符合条件的高质量资料：搜索服务未返回可用书源。"
                "请稍后重试，或检查搜索供应商配置。"
            ),
            receipt_backed=True,
            receipt_refs=list(receipt_refs or []),
        )

    lines: list[str] = []
    seen_urls: set[str] = set()
    for source in sources[:MAX_SOURCES]:
        url = str(source.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = str(source.get("title") or "").strip()
        snippet = str(source.get("snippet") or "").strip()
        line = f"- {title}" if title else "- 资料"
        if snippet:
            line += f"：{snippet[:120]}"
        line += f"\n  来源：{url}"
        lines.append(line)
    for finding in findings[:MAX_SOURCES]:
        if finding and finding not in lines:
            lines.append(f"- {finding[:160]}")

    content = "深度检索结果：\n\n" + "\n".join(lines)
    return PublishedAnswer(
        status="completed",
        content=content,
        receipt_backed=True,
        receipt_refs=list(receipt_refs or []),
        publication_mode="deterministic_receipt",
    )


__all__ = ["merge_research_reports", "research_terminal_answer"]
