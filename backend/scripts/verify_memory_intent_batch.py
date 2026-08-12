"""Offline divergent verification for the memory intent state machine.

Run: python scripts/verify_memory_intent_batch.py
Every case prints its classification; the script exits non-zero when any
canonical memory-domain phrasing falls back to the model unexpectedly.
"""

from __future__ import annotations

import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.memory.intent import MemoryIntentClassifier


CASES: list[tuple[str, str]] = [
    ("我是谁", "current"),
    ("我叫什么", "current"),
    ("我的名字是什么", "current"),
    ("我之前叫什么", "previous"),
    ("我之前叫啥", "previous"),
    ("之前我叫什么来着", "previous"),
    ("我之前的名字呢", "previous"),
    ("我最早叫什么", "earliest"),
    ("我最早叫什么名字", "earliest"),
    ("我改过几次名字", "timeline"),
    ("名字历史", "timeline"),
    ("我什么时候改的名字", "timeline"),
    ("什么时候说的我叫冰露", "when"),
    ("我叫冰露", "write"),
    ("我现在叫冰露", "write"),
    ("我现在不叫黄仁，我叫冰露", "correct"),
    ("把名字改成小露", "correct"),
    ("我以后就叫冰露", "correct"),
    ("我不叫黄仁", "correct"),
    ("我喜欢科幻小说", "write"),
    ("我更喜欢推理小说", "write"),
    ("我不喜欢恐怖小说", "write"),
    ("我讨厌甜食", "write"),
    ("我想要一本推理小说", "write"),
    ("我有一只猫", "write"),
    ("我还有一只狗", "write"),
    ("我养了一只猫", "write"),
    ("帮我记住我叫冰露", "write"),
    ("忘了我叫冰露", "forget"),
    ("忘记我的名字", "forget"),
    ("那之前呢", "read"),
    ("那最早呢", "read"),
]

FALLBACK_CASES: list[str] = [
    "帮我查一下今天的天气",
    "深度搜索一下最新的好书",
    "你现在叫什么",
    "你好",
    "我今天想吃什么",
    "这本书怎么样",
]


def main() -> int:
    classifier = MemoryIntentClassifier()
    topic = classifier.classify("我叫冰露")
    failures = 0
    for text, expected in CASES:
        intent = classifier.classify(text, previous_topic=topic)
        actual = intent.kind if intent is not None else "NONE"
        mark = "ok" if actual == expected else "FAIL"
        if mark == "FAIL":
            failures += 1
        print(f"[{mark}] {text} -> {actual} (expected {expected})")
    for text in FALLBACK_CASES:
        intent = classifier.classify(text)
        actual = intent.kind if intent is not None else "NONE"
        mark = "ok" if actual == "NONE" else "FAIL"
        if mark == "FAIL":
            failures += 1
        print(f"[{mark}] {text} -> {actual} (expected model fallback)")
    print(f"failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
