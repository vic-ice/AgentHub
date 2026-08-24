"""Live HTTP verification for the explicit app-owned Deep Research mode."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import func, select, text


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
BASE_URL = "http://127.0.0.1:8080/api/v1"


async def _run(content: str, model_uuid: str) -> dict:
    from app.infra.database import get_database
    from app.models.research import (
        ResearchEvidenceRecord,
        ResearchRunRecord,
        ResearchStepRecord,
    )

    user_id, thread_id = uuid.uuid4(), uuid.uuid4()
    request_id = f"deep-research-{uuid.uuid4()}"
    database = get_database()
    async with database.session() as session:
        await session.execute(
            text(
                "INSERT INTO public.users (id, display_name, is_mock_user) "
                "VALUES (:user_id, 'Deep Research HTTP E2E', true)"
            ),
            {"user_id": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO public.conversations (thread_id, user_id, title) "
                "VALUES (:thread_id, :user_id, 'Deep Research HTTP E2E')"
            ),
            {"thread_id": thread_id, "user_id": user_id},
        )
    try:
        request = {
            "content": content,
            "user_id": str(user_id),
            "thread_id": str(thread_id),
            "request_id": request_id,
            "research_mode": "deep_research",
        }
        if model_uuid:
            request["model_uuid"] = model_uuid
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=360) as client:
            response = await client.post("/chat/invoke", json=request)
        response.raise_for_status()
        payload = response.json()
        answer = str(payload.get("content") or "")
        custom = payload.get("custom_data") or {}
        run_id = uuid.UUID(str(custom.get("research_run_id") or ""))
        assert answer.strip(), payload
        assert "CurrentMemory was used" not in answer, answer
        assert "长期记忆" not in answer, answer
        assert custom.get("research_status") in {"completed", "failed"}, custom

        async with database.session() as session:
            run = await session.get(ResearchRunRecord, run_id)
            step_count = await session.scalar(
                select(func.count()).select_from(ResearchStepRecord).where(
                    ResearchStepRecord.run_id == run_id
                )
            )
            evidence_count = await session.scalar(
                select(func.count()).select_from(ResearchEvidenceRecord).where(
                    ResearchEvidenceRecord.run_id == run_id
                )
            )
        assert run is not None, custom
        assert run.user_id == user_id and run.thread_id == thread_id, run
        assert run.mode == "deep_research", run.mode
        assert run.status != "active", run.status
        assert int(step_count or 0) > 0, step_count
        assert int(evidence_count or 0) == int(
            custom.get("research_evidence_count") or 0
        ), (evidence_count, custom)
        return {
            "status": "passed",
            "research_mode": run.mode,
            "run_status": run.status,
            "run_id": str(run_id),
            "step_count": int(step_count or 0),
            "evidence_count": int(evidence_count or 0),
            "answer_chars": len(answer),
        }
    finally:
        async with database.session() as session:
            await session.execute(
                text(
                    "DELETE FROM public.users WHERE id = :user_id "
                    "AND is_mock_user = true"
                ),
                {"user_id": user_id},
            )


async def _main(content: str, model_uuid: str) -> int:
    from app.infra.database import dispose_database, init_database_connection

    await init_database_connection()
    try:
        result = await _run(content, model_uuid)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        await dispose_database()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--content",
        default="独立深度研究近期值得阅读的个人成长图书，并给出有来源的报告。",
    )
    parser.add_argument("--model-uuid", default="")
    return parser.parse_args()


if __name__ == "__main__":
    args = _arguments()
    raise SystemExit(asyncio.run(_main(args.content, args.model_uuid)))
