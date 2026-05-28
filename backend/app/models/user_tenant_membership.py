"""user_tenant_membership — ADR-018 §4.

DomainRAG 자체 테이블. AuthFusion JWT에 없는 clearance/department/domain_groups를 보강.
RLS 미적용 (cross-tenant 매핑 정의 — platform_admin이 관리).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserTenantMembership(Base):
    __tablename__ = "user_tenant_membership"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False)
    clearance: Mapped[str] = mapped_column(
        String(50), nullable=False, default="internal"
    )
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    domain_groups: Mapped[list[str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
        server_default="[]",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
