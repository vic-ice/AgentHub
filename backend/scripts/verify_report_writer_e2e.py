import asyncio
import os
import sys
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app.infra.database import init_database_connection
from app.infra.llm.manager import get_model_manager
from app.services.research.publication.report_writer import write_research_report
from app.services.research.report import build_research_report

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from probe_report_a_gate import MODEL_ID


async def main() -> None:
    await init_database_connection()
    await get_model_manager().refresh()

    run_id = os.environ.get("E2E_RUN_ID", "ee615768-0242-4b6d-9199-d752eefa634d")
    report = await build_research_report(
        user_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=UUID(run_id),
    )

    result = await write_research_report(report, model_id=MODEL_ID)
    print("attempts:", result.metadata.get("attempts"))
    print("status:", result.status)
    print("provider:", result.provider)
    print("model_id:", result.model_id)
    print("failure_cause:", result.metadata.get("failure_cause"))
    print("sections:", result.metadata.get("sections"))
    print("=== REPORT ===")
    print(result.report_markdown[:3000])


if __name__ == "__main__":
    asyncio.run(main())
