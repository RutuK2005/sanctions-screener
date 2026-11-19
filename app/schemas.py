"""Pydantic v2 request/response models (the API contract shown in /docs)."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.cases import Decision, Status

T = TypeVar("T")


class ScreeningRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300, examples=["Ayman al-Zawahiri"])
    entity_type: Literal["individual", "entity"] | None = Field(
        default=None, description="Customer type, if known. A mismatch with the listing costs 0.10.")
    dob: str | None = Field(default=None, pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$",
                            description="YYYY, YYYY-MM or YYYY-MM-DD", examples=["1951-06-19"])
    country: str | None = Field(default=None, max_length=100, examples=["Egypt"])
    threshold: float | None = Field(default=None, ge=0.5, le=1.0,
                                    description="Override the server's default match threshold")


class HitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    entity_id: int
    source: str
    source_id: str
    primary_name: str
    entity_type: str
    programs: list[str]
    matched_name: str
    score: float
    explanation: dict


class ScreeningOut(BaseModel):
    id: int
    query_name: str
    query_type: str | None
    query_dob: str | None
    query_country: str | None
    threshold: float
    top_score: float
    hit_count: int
    hits: list[HitOut]
    case_id: int | None
    latency_ms: float
    created_at: datetime


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    actor: str
    from_status: str | None
    to_status: str | None
    note: str | None
    details: dict
    created_at: datetime


class CaseSummaryOut(BaseModel):
    id: int
    status: Status
    top_score: float
    query_name: str
    hit_count: int
    created_at: datetime
    updated_at: datetime


class CaseDetailOut(CaseSummaryOut):
    screening: ScreeningOut
    allowed_decisions: list[str]
    summary: str | None
    summary_source: str | None
    audit: list[AuditOut]


class DecisionRequest(BaseModel):
    decision: Decision
    reviewer: str = Field(min_length=1, max_length=64, examples=["analyst.priya"])
    note: str = Field(min_length=3, max_length=2000, examples=["DOB and nationality differ from listing."])


class SummaryOut(BaseModel):
    case_id: int
    summary: str
    source: Literal["llm", "template"]


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
