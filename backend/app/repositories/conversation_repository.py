"""PostgresConversationRepository — conversations CRUD (ADR-017 §4 + ADR-008 RLS).

흐름:
  1. set_tenant_context (RLS) — 매 호출
  2. SQL: SELECT/UPDATE/DELETE — tenant_id 필터는 RLS가 자동, 추가로 user_id
     명시(다른 user의 conversation 격리).
  3. PostgreSQL이 messages를 CASCADE 삭제. chat_logs.conversation_id는 FK가
     없으므로 보존된다 (audit truth — ADR-020 §10 right-to-erasure가 별도).

message_count는 chat_logs JOIN으로 산출. messages 테이블은 미사용(현 시점).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.conversation_repository import (
    ConversationListResult,
    ConversationRecord,
)

from app.core.rls import set_tenant_context


class PostgresConversationRepository:
    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def list_by_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> ConversationListResult:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        offset = (page - 1) * page_size

        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)

            total_row = (
                await session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM conversations
                         WHERE tenant_id = :tenant_id AND user_id = :user_id
                        """
                    ),
                    {"tenant_id": tenant_id, "user_id": user_id},
                )
            ).first()
            total = int(total_row[0]) if total_row else 0

            rows = (
                await session.execute(
                    text(
                        """
                        SELECT c.id, c.title, c.created_at, c.updated_at,
                               (SELECT COUNT(*)
                                  FROM chat_logs cl
                                 WHERE cl.tenant_id = c.tenant_id
                                   AND cl.conversation_id = c.id) AS msg_count
                          FROM conversations c
                         WHERE c.tenant_id = :tenant_id AND c.user_id = :user_id
                         ORDER BY c.updated_at DESC
                         LIMIT :limit OFFSET :offset
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "limit": page_size,
                        "offset": offset,
                    },
                )
            ).all()

        items = [
            ConversationRecord(
                id=str(r[0]),
                tenant_id=tenant_id,
                user_id=user_id,
                title=r[1],
                created_at=_as_dt(r[2]),
                updated_at=_as_dt(r[3]),
                message_count=int(r[4] or 0),
            )
            for r in rows
        ]
        return ConversationListResult(
            items=items, total=total, page=page, page_size=page_size
        )

    async def get(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> ConversationRecord | None:
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            row = (
                await session.execute(
                    text(
                        """
                        SELECT c.id, c.title, c.created_at, c.updated_at,
                               (SELECT COUNT(*)
                                  FROM chat_logs cl
                                 WHERE cl.tenant_id = c.tenant_id
                                   AND cl.conversation_id = c.id) AS msg_count
                          FROM conversations c
                         WHERE c.tenant_id = :tenant_id
                           AND c.user_id = :user_id
                           AND c.id = CAST(:conversation_id AS UUID)
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "conversation_id": conversation_id,
                    },
                )
            ).first()
        if row is None:
            return None
        return ConversationRecord(
            id=str(row[0]),
            tenant_id=tenant_id,
            user_id=user_id,
            title=row[1],
            created_at=_as_dt(row[2]),
            updated_at=_as_dt(row[3]),
            message_count=int(row[4] or 0),
        )

    async def update_title(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        title: str,
    ) -> ConversationRecord | None:
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            result = await session.execute(
                text(
                    """
                    UPDATE conversations
                       SET title = :title, updated_at = NOW()
                     WHERE tenant_id = :tenant_id
                       AND user_id = :user_id
                       AND id = CAST(:conversation_id AS UUID)
                    """
                ),
                {
                    "title": title,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                },
            )
            await session.commit()
        if (result.rowcount or 0) == 0:
            return None
        return await self.get(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )

    async def delete(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
    ) -> int:
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            result = await session.execute(
                text(
                    """
                    DELETE FROM conversations
                     WHERE tenant_id = :tenant_id
                       AND user_id = :user_id
                       AND id = CAST(:conversation_id AS UUID)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                },
            )
            await session.commit()
        return result.rowcount or 0


def _as_dt(value) -> datetime | None:
    return value if isinstance(value, datetime) else None
