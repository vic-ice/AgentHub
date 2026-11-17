import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
from app.services.research.source_visit import fetch_research_source_document
from app.services.research.source_extraction import extract_research_source_records
from app.services.research.evidence_quality import assess_evidence_candidate

URL = "https://www.zhihu.com/tardis/bd/art/415035356"
QUERY = "machine learning books for beginners 2024"


async def main() -> None:
    print("=== FETCH ===")
    visit = await fetch_research_source_document(url=URL, query=QUERY, subquestion=QUERY)
    doc = visit.source_document
    if doc is None:
        print("no document; error:", visit.error)
        return
    content = str(doc.content or "")
    print("status:", visit.status, "| title:", doc.source_title, "| chars:", len(content))
    print("content head:", content[:1200].replace("\n", " | "))

    # 数一下内容里的书名模式（《...》 或 数字. 书名）
    titles = re.findall(r"[0-9]+[.、]\s*《([^》]+)》", content)
    print("title patterns found in content:", len(titles), titles[:25])

    doc_payload = doc.model_dump(mode="json") if hasattr(doc, "model_dump") else dict(doc)

    for limit in (1, 5):
        print(f"\n=== EXTRACTION (max_records_per_document={limit}) ===")
        extraction = extract_research_source_records(
            query=QUERY,
            subquestion=QUERY,
            documents=[doc_payload],
            provider_source="web_search",
            max_records_per_document=limit,
        )
        print("source_records:", len(extraction.source_records))
        for record in extraction.source_records:
            print(" -", str(record.claim)[:240])
        print("rejected_documents:", len(extraction.rejected_documents))

        print(f"\n=== ADMISSION (max={limit}) ===")
        admitted = 0
        for record in extraction.source_records:
            assessment = assess_evidence_candidate(
                claim=record.claim,
                query=QUERY,
                source_title=record.source_title,
                source_url=record.source_url,
                published_date=str((record.metadata or {}).get("published_date") or ""),
            )
            if assessment.publishable:
                admitted += 1
        print("publishable:", admitted, "/", len(extraction.source_records))


asyncio.run(main())