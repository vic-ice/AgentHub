import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
from app.services.research.source_visit import fetch_research_source_document

QUERY = "machine learning books for beginners 2024"
URLS = [
    "http://ptpress.com.cn:8989/p/news/1730182463972.html",
    "https://www.zhihu.com/tardis/bd/art/415035356",
    "https://www.zhihu.com/tardis/zm/art/415035356",
    "https://hub.baai.ac.cn/view/43072",
    "https://www.tableau.com/zh-cn/learn/articles/books-about-machine-learning",
    "https://book.douban.com/tag/%E6%9C%BA%E5%99%A8%E5%AD%A6%E4%B9%A0",
]

async def main() -> None:
    for url in URLS:
        try:
            visit = await fetch_research_source_document(url=url, query=QUERY, subquestion=QUERY)
            doc = visit.source_document
            if doc is None:
                print(f"{url}\n  -> status={visit.status} error={str(visit.error)[:120]}")
            else:
                content = str(doc.content or "")
                print(f"{url}\n  -> status={visit.status} title={doc.source_title[:60]} chars={len(content)}")
        except Exception as exc:
            print(f"{url}\n  -> EXC {type(exc).__name__}: {str(exc)[:150]}")

asyncio.run(main())