"""tenants + tenant_delete_failures ORM — ADR-008/012. Migration 001/006과 1:1 대응."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    domain_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    embedding_model: Mapped[str] = mapped_column(
        String(100), nullable=False, default="bge-m3", server_default="bge-m3"
    )
    embedding_migration_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="idle", server_default="idle"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    # ADR-022 §4 — open(인증된 모든 user 자동 접근) | assigned(관리자 배정만)
    enrollment_policy: Mapped[str] = mapped_column(
        String(20), nullable=False, default="assigned", server_default="assigned"
    )
    modules: Mapped[list[str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=lambda: ["rag"],
        server_default='["rag"]',
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # ADR-012 §6 — hard delete 진행 상태
    delete_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TenantDeleteFailure(Base):
    """ADR-012 §6 — hard delete dead-letter queue."""

    __tablename__ = "tenant_delete_failures"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False)
    failed_step: Mapped[str] = mapped_column(String(50), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
