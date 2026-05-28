"""PostgresFeedbackWriter — ADR-017 §5 POST /feedback의 chat_logs UPDATE.

흐름:
  1. set_tenant_context (RLS — cross-tenant 보호)
  2. UPDATE chat_logs SET feedback=:fb, feedback_comment=:cmt
        WHERE domain_id=:tid AND request_id=:mid
          AND (user_id IS NOT DISTINCT FROM :uid)
  3. RETURNING으로 영향 rowcount 산출 — 0이면 caller가 404 변환

본인 message 한정. service_account 또는 admin이 타인 message에 feedback을 다는
시나리오는 본 endpoint에서 차단(분리 endpoint 필요 시 별도 ADR).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.feedback_writer import FeedbackResult

from app.core.rls import set_tenant_context


class PostgresFeedbackWriter:
    def __init__(self, *, app_session_factory: async_sessionmaker) -> None:
        self._sf = app_session_factory

    async def record(
        self,
        *,
        domain_id: str,
        message_id: str,
        user_id: str | None,
        feedback: str,
        comment: str | None,
    ) -> FeedbackResult:
        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            result = await session.execute(
                text(
                    """
                    UPDATE chat_logs
                       SET feedback = :feedback,
                           feedback_comment = :comment
                     WHERE domain_id = :domain_id
                       AND request_id = :message_id
                       AND user_id IS NOT DISTINCT FROM :user_id
                    """
                ),
                {
                    "feedback": feedback,
                    "comment": comment,
                    "domain_id": domain_id,
                    "message_id": message_id,
                    "user_id": user_id,
                },
            )
            await session.commit()
        return FeedbackResult(
            domain_id=domain_id,
            message_id=message_id,
            user_id=user_id,
            feedback=feedback,
            affected=result.rowcount or 0,
        )
