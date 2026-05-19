"""documents — ADR-001/012 lifecycle. Migration 001과 1:1 대응."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import JSON, Date, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    doc_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    input_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    doc_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    security_level: Mapped[str] = mapped_column(
        String(50), nullable=False, default="internal", server_default="internal"
    )
    version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="v1", server_default="v1"
    )
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
        server_default="[]",
    )
    language: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ko", server_default="ko"
    )
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="draft", server_default="draft"
    )
    file_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parser_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="p1", server_default="p1"
    )
    doc_metadata: Mapped[dict] = mapped_column(
        "metadata",  # SQL column name — Python attribute renamed to avoid SQLAlchemy reserved word
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
