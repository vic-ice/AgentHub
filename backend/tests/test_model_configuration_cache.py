from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.api.v1.models import _commit_configuration_and_refresh


class ModelConfigurationCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_commit_precedes_runtime_cache_refresh(self) -> None:
        events: list[str] = []
        session = AsyncMock()
        session.commit.side_effect = lambda: events.append("commit")

        manager = Mock()
        manager.refresh = AsyncMock(side_effect=lambda: events.append("refresh"))

        with patch("app.api.v1.models.get_model_manager", return_value=manager):
            await _commit_configuration_and_refresh(session)

        self.assertEqual(events, ["commit", "refresh"])

    async def test_failed_commit_never_publishes_uncommitted_configuration(self) -> None:
        session = AsyncMock()
        session.commit.side_effect = RuntimeError("commit failed")
        manager = Mock()
        manager.refresh = AsyncMock()

        with patch("app.api.v1.models.get_model_manager", return_value=manager):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                await _commit_configuration_and_refresh(session)

        manager.refresh.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
