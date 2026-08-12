import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app.infra.llm import get_llm
from app.services.research.publication import report_writer as rw


OBJECTIVE = "深度搜索：2024年值得推荐的机器学习入门书"
MODEL_ID = "11111111-aaaa-4bbb-8ccc-000000000017"

EVIDENCE = [
    {
        "source_id": "8780d46a-40e5-4dcc-bcba-6e746db3a833",
        "source_title": "有什么推荐的机器学习入门书籍",
        "source_url": "https://bbs.eeworld.com.cn/wdthread-1281306-1-1.html",
        "claim": "有什么推荐的机器学习入门书籍 (内容由AI生成 此帖出自[问答论坛 (https://bbs.",
    },
    {
        "source_id": "452cbec1-c9dc-4f7e-98a6-57ef2a2936d4",
        "source_title": "2024年人工智能初学者精选书籍推荐",
        "source_url": "https://cloud.baidu.com/article/3375582",
        "claim": "本文为初学者精选了20本人工智能领域的书籍，涵盖了从基础知识到高级算法的多个方面，包括《人工智能：现代方法》《机器学习入门》等经典之作，以及《动手学深度学习》《用于机器学习的Transformer》等实战导向的书籍，旨在帮助读者构建全面的人工智能知识体系。",
    },
    {
        "source_id": "34c6b169-88f2-4f51-ad41-ccc36a6a3f48",
        "source_title": "关于机器学习的 7 本入门级好书",
        "source_url": "https://www.tableau.com/zh-cn/learn/articles/books-about-machine-learning",
        "claim": "对于不太熟悉数学，但具有编程和编码语言经验的读者来说，《黑客的机器学习》是非常有用的读物。",
    },
]


async def main() -> None:
    from app.infra.database import init_database_connection
    await init_database_connection()
    from app.infra.llm.manager import get_model_manager
    await get_model_manager().refresh()

    prompt = rw._prompt_freeform(
        objective=OBJECTIVE,
        language="zh-CN",
        evidence=EVIDENCE,
        limitations=[],
        review=None,
    )
    print("=== PROMPT ===")
    print(prompt)
    print("\n=== CALLING MODEL ===")
    model = get_llm(MODEL_ID, thinking_mode=None)
    response = await model.ainvoke(prompt)
    raw = response.content
    if isinstance(raw, list):
        from collections import Counter
        kinds = Counter(
            b.get("type", "<str>") if isinstance(b, dict) else type(b).__name__
            for b in raw
        )
        print("raw blocks:", len(raw), "kinds:", dict(kinds))
        for i, block in enumerate(raw[-3:]):
            print(f"raw tail block {len(raw)-3+i}:", repr(block)[:400])
        str_parts = [b for b in raw if isinstance(b, str) and b.strip()]
        if str_parts:
            print("final text candidates:", [s[:200] for s in str_parts])
    body = rw._message_text(response)
    print(f"\n=== BODY via _message_text ({len(body)} chars) ===")
    print(body[:4000])

    print("\n=== GATE ===")
    extracted = rw._extract_report_body(body)
    print(f"extract_report_body -> {len(extracted)} chars")
    print(extracted[:1500])

    looks = rw._looks_like_report(extracted)
    print(f"looks_like_report -> {looks}")
    if not looks:
        lines = [line.strip() for line in extracted.splitlines() if line.strip()]
        print("first line:", repr(lines[0][:120]) if lines else "<empty>")
        seen: set[str] = set()
        for line in lines:
            if line.startswith("## "):
                seen.add(line[3:].strip()[:24])
        print("headings seen:", sorted(seen))

    joined = len(" ".join(extracted.split()))
    print(f"chars after join -> {joined} (min {rw.MIN_USABLE_BODY_CHARS}: {joined >= rw.MIN_USABLE_BODY_CHARS})")


if __name__ == "__main__":
    asyncio.run(main())
