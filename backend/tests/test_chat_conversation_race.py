from __future__ import annotations

import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.exc import IntegrityError

from app.crud.chat import get_or_create_conversation_by_thread_id
from app.models.chat import Conversation


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class ConversationRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_insert_returns_winning_conversation(self) -> None:
        thread_id = uuid.uuid4()
        user_id = uuid.uuid4()
        winner = Conversation(
            thread_id=thread_id,
            user_id=user_id,
            title="winner",
        )
        db = MagicMock()
        db.execute = AsyncMock(
            side_effect=[_ScalarResult(None), _ScalarResult(winner)]
        )
        db.begin_nested.return_value = _NestedTransaction()
        db.flush = AsyncMock(
            side_effect=IntegrityError("insert", {}, RuntimeError("duplicate"))
        )
        db.refresh = AsyncMock()

        result = await get_or_create_conversation_by_thread_id(
            db,
            thread_id=thread_id,
            user_id=user_id,
            title="requested",
        )

        self.assertIs(result, winner)
        self.assertEqual(db.execute.await_count, 2)
        db.refresh.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
