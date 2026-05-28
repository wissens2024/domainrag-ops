"""
Async SQLAlchemy session — domainrag database (ADR-019).

두 engine:
  - app_engine: domainrag_app role (RLS 적용)
  - admin_engine: domainrag_platform_admin role (BYPASSRLS, cross-tenant 분석 한정)
"""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.rls import set_tenant_context

settings = get_settings()

app_engine = create_async_engine(settings.db_dsn, pool_size=2, max_overflow=18)
admin_engine = create_async_engine(settings.db_admin_dsn, pool_size=1, max_overflow=3)

AppSessionLocal = async_sessionmaker(app_engine, expire_on_commit=False)
AdminSessionLocal = async_sessionmaker(admin_engine, expire_on_commit=False)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Tenant-scope DB session — RLS 적용. RLS context는 caller가 set_tenant_context로 주입."""
    async with AppSessionLocal() as session:
        yield session


async def get_admin_db_session() -> AsyncGenerator[AsyncSession, None]:
    """platform_admin 전용 — BYPASSRLS. cross-tenant 분석에만 사용."""
    async with AdminSessionLocal() as session:
        yield session


async def get_tenant_session(
    domain_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncGenerator[AsyncSession, None]:
    """tenant context가 자동 주입된 session — `/api/{domain_id}/...` endpoint용."""
    async with AppSessionLocal() as session:
        await set_tenant_context(session, domain_id)
        yield session
