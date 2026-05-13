"""
PostgreSQL Row-Level Security helper (ADR-008 §2, ADR-019 §2).

각 connection 생애에 SET LOCAL app.current_tenant 주입. RLS policy가 이 setting을
참조해 cross-tenant data leak 차단.
"""

from sqlalchemy.ext.asyncio import AsyncSession


async def set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """RLS context 설정.

    transaction-local로 적용 (다른 transaction에 영향 없음).
    quote escaping은 SQLAlchemy text bind로 처리 (SQL injection 방지).
    """
    from sqlalchemy import text

    await session.execute(
        text("SELECT set_config('app.current_tenant', :tid, true)"),
        {"tid": tenant_id},
    )
