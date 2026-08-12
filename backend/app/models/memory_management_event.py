import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.database.base import Base
from app.models.base import utc_now


class MemoryManagementEvent(Base):
    """Append-only user-initiated memory action used as a write source."""

    __tablename__ = "memory_management_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("conversations.thread_id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    memory_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    subject: Mapped[str] = mapped_column(
        String(64), nullable=False, default="self"
    )
    predicate: Mapped[str] = mapped_column(String(256), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    qualifiers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    evidence_quote: Mapped[str] = mapped_column(Text, nullable=False)
    previous_value: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    @property
    def content(self) -> str:
        """Unified source text consumed by the version-store evidence check."""

        return self.evidence_quote
