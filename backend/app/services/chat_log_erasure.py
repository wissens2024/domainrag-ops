"""PostgresChatLogEraser — chat_logs right-to-erasure (ADR-020 §10).

흐름:
  1. set_tenant_context (RLS — cross-tenant 보호)
  2. mode=mask_only: UPDATE chat_logs SET question=NULL, answer=NULL, retrieved_chunks='[]',
     citations='[]', input_pii_found='[]', output_pii_masked='[]', pii_storage_policy='erased',
     user_id=NULL, rewritten_query=NULL
     WHERE tenant_id = :tid AND user_id = :uid
  3. mode=hard_delete: DELETE FROM chat_logs WHERE tenant_id=:tid AND user_id=:uid
  4. tenant_lifecycle_logs에 audit row (user_right_to_erasure_executed) — admin engine.

mask_only는 운영 지표(latency/routing/verifier 등)를 보존해 시스템 통계의 의미를 잃지
않으면서 PII·발화 본문만 제거한다 — ADR-020 §10 정합. hard_delete는 사용자 요구가
강한 경우의 백업.
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.services.chat_log_erasure import ErasureMode, ErasureResult

from app.core.rls import set_tenant_context


class PostgresChatLogEraser:
    """SQLAlchemy + RLS 구현체.

    Args:
        app_session_factory: domainrag_app role (RLS 적용). chat_logs DML에 사용.
        admin_session_factory: domainrag_platform_admin role (BYPASSRLS). audit log
            (tenant_lifecycle_logs)에 사용 — RLS 정책상 cross-tenant audit가 필요해도
            본 erase 호출은 tenant 단위이므로 admin engine은 단지 BYPASSRLS만 활용한다.
    """

    def __init__(
        self,
        *,
        app_session_factory: async_sessionmaker,
        admin_session_factory: async_sessionmaker,
    ) -> None:
        self._app = app_session_factory
        self._admin = admin_session_factory

    async def erase_my_logs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        mode: ErasureMode,
        reason: str,
    ) -> ErasureResult:
        async with self._app() as session:
            await set_tenant_context(session, tenant_id)
            if mode == ErasureMode.HARD_DELETE:
                result = await session.execute(
                    text(
                        """
                        DELETE FROM chat_logs
                         WHERE tenant_id = :tenant_id AND user_id = :user_id
                        """
                    ),
                    {"tenant_id": tenant_id, "user_id": user_id},
                )
            else:
                result = await session.execute(
                    text(
                        """
                        UPDATE chat_logs
                           SET question = NULL,
                               rewritten_query = NULL,
                               answer = NULL,
                               retrieved_chunks = CAST('[]' AS JSONB),
                               citations = CAST('[]' AS JSONB),
                               citation_types = CAST('[]' AS JSONB),
                               input_pii_found = CAST('[]' AS JSONB),
                               output_pii_masked = CAST('[]' AS JSONB),
                               pii_storage_policy = 'erased',
                               user_id = NULL
                         WHERE tenant_id = :tenant_id AND user_id = :user_id
                        """
                    ),
                    {"tenant_id": tenant_id, "user_id": user_id},
                )
            affected = result.rowcount or 0
            await session.commit()

        # audit (tenant_lifecycle_logs — admin engine, BYPASSRLS)
        async with self._admin() as audit_session:
            await audit_session.execute(
                text(
                    """
                    INSERT INTO tenant_lifecycle_logs (
                        tenant_id, action, from_state, to_state, actor, reason, details
                    )
                    VALUES (
                        :tenant_id, 'user_right_to_erasure_executed',
                        NULL, NULL, :actor, :reason, CAST(:details AS JSONB)
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "actor": user_id,
                    "reason": reason,
                    "details": json.dumps(
                        {"mode": mode.value, "affected_rows": affected},
                        ensure_ascii=False,
                    ),
                },
            )
            await audit_session.commit()

        return ErasureResult(
            tenant_id=tenant_id,
            user_id=user_id,
            mode=mode,
            affected_rows=affected,
            reason=reason,
        )
