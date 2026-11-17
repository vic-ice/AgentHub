"""File-based research search reproduction (UTF-8 safe)."""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.infra.database import dispose_database, init_database
from app.services.external_search import SearchRequest, get_search_gateway
from app.services.research.search_policy import build_research_search_request


OBJECTIVES = [
    "类似《失控》的复杂系统与系统思维类书籍推荐",
    "类似《写给管理者的睡前故事》的管理故事与寓言类书籍",
    "凯文·凯利的其他代表作和风格特点",
]


async def main() -> int:
    await init_database()
    try:
        failures = 0
        for objective in OBJECTIVES:
            request = build_research_search_request(objective)
            args = request.tool_arguments()
            search_request = SearchRequest(
                query=args["query"],
                max_results=args["max_results"],
                detail=args["detail"],
                include_domains=args["include_domains"],
                include_url_prefixes=args["include_url_prefixes"],
                zone="cn",
                language="zh",
            )
            print("OBJECTIVE:", objective)
            print("QUERY:", args["query"])
            try:
                result = await get_search_gateway().search(search_request)
                print("OUTCOME:", result.outcome, "hits:", len(result.hits or []))
                for hit in (result.hits or [])[:4]:
                    print("  -", str(hit.title)[:50], "|", hit.url[:80])
                douban_hits = [
                    h for h in (result.hits or []) if "douban.com" in (h.url or "")
                ]
                if not douban_hits:
                    failures += 1
                    print("  NO_DOUBAN_HITS")
            except Exception as exc:
                failures += 1
                print("  SEARCH_ERROR:", str(exc)[:200])
        print("failures:", failures)
        return 1 if failures else 0
    finally:
        await dispose_database()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
