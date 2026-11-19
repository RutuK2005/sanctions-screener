"""Database tables (SQLAlchemy 2.0 typed ORM models).

The schema itself is created by Alembic (alembic/versions/*). These classes
must stay in sync with the migrations; tests run the real migrations.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

CASE_STATUSES = ("open", "escalated", "cleared", "confirmed_match")


class ListVersion(Base):
    """One row per ingestion run of a source list (provenance for every hit)."""

    __tablename__ = "list_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    version: Mapped[str] = mapped_column(String(64))  # publish date / checksum of the file
    entity_count: Mapped[int] = mapped_column(Integer)
    name_count: Mapped[int] = mapped_column(Integer)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SanctionedEntity(Base):
    __tablename__ = "sanctioned_entities"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_entity_source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # OFAC_SDN | UN_SC
    source_id: Mapped[str] = mapped_column(String(64))  # id inside that list
    entity_type: Mapped[str] = mapped_column(String(16))  # individual | entity | vessel | aircraft
    primary_name: Mapped[str] = mapped_column(Text)
    programs: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    countries: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    dobs: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)  # YYYY | YYYY-MM | YYYY-MM-DD
    remarks: Mapped[str | None] = mapped_column(Text)
    list_version: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    names: Mapped[list[EntityName]] = relationship(back_populates="entity", cascade="all, delete-orphan")


class EntityName(Base):
    """Primary name + every alias, each with its normalised form (the search key)."""

    __tablename__ = "entity_names"
    __table_args__ = (
        Index("ix_entity_names_norm_trgm", "name_norm", postgresql_using="gin",
              postgresql_ops={"name_norm": "gin_trgm_ops"}),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("sanctioned_entities.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Text)
    name_norm: Mapped[str] = mapped_column(Text)
    name_type: Mapped[str] = mapped_column(String(16))  # primary | aka | fka | nka | weak_aka

    entity: Mapped[SanctionedEntity] = relationship(back_populates="names")


class Screening(Base):
    __tablename__ = "screenings"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    query_name: Mapped[str] = mapped_column(Text)
    query_type: Mapped[str | None] = mapped_column(String(16))
    query_dob: Mapped[str | None] = mapped_column(String(10))
    query_country: Mapped[str | None] = mapped_column(Text)
    threshold: Mapped[float] = mapped_column(Float)
    top_score: Mapped[float] = mapped_column(Float)
    hit_count: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    hits: Mapped[list[ScreeningHit]] = relationship(back_populates="screening", order_by="ScreeningHit.score.desc()")
    case: Mapped[Case | None] = relationship(back_populates="screening")


class ScreeningHit(Base):
    __tablename__ = "screening_hits"

    id: Mapped[int] = mapped_column(primary_key=True)
    screening_id: Mapped[int] = mapped_column(ForeignKey("screenings.id", ondelete="CASCADE"), index=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("sanctioned_entities.id"))
    score: Mapped[float] = mapped_column(Float)
    matched_name: Mapped[str] = mapped_column(Text)
    explanation: Mapped[dict] = mapped_column(JSONB)

    screening: Mapped[Screening] = relationship(back_populates="hits")
    entity: Mapped[SanctionedEntity] = relationship()


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (CheckConstraint(f"status IN {CASE_STATUSES}", name="ck_case_status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    screening_id: Mapped[int] = mapped_column(ForeignKey("screenings.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    top_score: Mapped[float] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    summary_source: Mapped[str | None] = mapped_column(String(16))  # llm | template
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    screening: Mapped[Screening] = relationship(back_populates="case")
    audit: Mapped[list[AuditLog]] = relationship(order_by="AuditLog.id")


class AuditLog(Base):
    """Append-only: a DB trigger (see migration 0001) rejects UPDATE and DELETE."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
