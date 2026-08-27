from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MemoryAdminFact(BaseModel):
    """One canonical version-chain fact exposed to the memory panel."""

    schema_key: str
    memory_key: str
    subject: str
    predicate: str
    value: dict[str, Any] = Field(default_factory=dict)
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    evidence_quote: str = ""
    version_no: int = Field(ge=1)
    valid_from: datetime
    valid_to: datetime | None = None
    is_tombstone: bool = False
    domain: str = "general"
    kind: str = "fact"
    presentation_key: str = "general.fact"
    category_key: str = "fact"
    category_label: str = "其他事实"
    display_value: str = ""
    can_edit: bool = False
    can_forget: bool = False
    show_evidence: bool = True


class MemoryAdminCurrentResponse(BaseModel):
    facts: list[MemoryAdminFact] = Field(default_factory=list)


class MemoryAdminHistoryResponse(BaseModel):
    memory_key: str
    versions: list[MemoryAdminFact] = Field(default_factory=list)


class MemoryEditRequest(BaseModel):
    """Panel edit/create: a user statement expressed as one canonical fact."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    memory_key: str | None = Field(default=None, min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=256)
    value: dict[str, Any] = Field(default_factory=dict)
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    evidence_quote: str = Field(default="", max_length=4000)
    thread_id: UUID | None = None

    @field_validator("predicate", "evidence_quote", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return " ".join(str(value or "").split()).strip()


class MemoryForgetAdminRequest(BaseModel):
    """Panel forget: locate one existing fact semantically and tombstone it."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    memory_key: str | None = Field(default=None, min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=256)
    identity: dict[str, Any] = Field(default_factory=dict)
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    evidence_quote: str = Field(default="", max_length=4000)
    thread_id: UUID | None = None

    @field_validator("predicate", "evidence_quote", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return " ".join(str(value or "").split()).strip()
