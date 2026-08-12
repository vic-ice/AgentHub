import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
from app.services.research.source_visit import fetch_research_source_document
from app.services.research.evidence_quality import assess_evidence_candidate
from app.services.research.source_extraction import _clean_claim_text, _book_context

URL = "https://www.zhihu.com/tardis/bd/art/415035356"
QUERY = "machine learning books for beginners 2024"


async def main() -> None:
    visit = await fetch_research_source_document(url=URL, query=QUERY, subquestion=QUERY)
    doc = visit.source_document
    content = str(doc.content or "")
    titles = re.findall(r"\u300a([^\u300b]{2,80})\u300b", content)
    print("titles found:", len(titles))
    seen = set()
    for title in titles:
        t = title.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        claim = _clean_claim_text(_book_context(content, t))
        a = assess_evidence_candidate(
            claim=claim,
            query=QUERY,
            source_title=doc.source_title,
            source_url=doc.source_url,
            published_date="",
        )
        print(f"\n[{t}] publishable={a.publishable}")
        print("  claim:", claim[:150])
        if not a.publishable:
            print("  reasons:", a.reason_codes)

asyncio.run(main())