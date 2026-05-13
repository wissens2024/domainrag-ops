"""FeedbackWriter — ADR-017 §5 POST /feedback의 chat_logs UPDATE 진입점.

Protocol은 rag_core, Postgres 구현은 backend. tenant_id 격리는 구현체 책임(RLS).

본인 message만 UPDATE 가능 — caller가 user_id를 함께 전달해 service-level에서 검증.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class FeedbackResult:
    tenant_id: str
    message_id: str
    user_id: str | None
    feedback: str  # 'good' | 'bad'
    affected: int  # 0이면 message_not_found 또는 cross-user — caller가 404 변환


class FeedbackWriter(Protocol):
    async def record(
        self,
        *,
        tenant_id: str,
        message_id: str,
        user_id: str | None,
        feedback: str,
        comment: str | None,
    ) -> FeedbackResult: ...


# --------------------------------------------------------------------------- #
# InMemory — InMemoryChatLogWriter.records 공유
# --------------------------------------------------------------------------- #


class InMemoryFeedbackWriter:
    """InMemoryChatLogWriter.records의 request_id 매칭 row를 UPDATE.

    Args:
        writer: InMemoryChatLogWriter (records 공유).
    """

    def __init__(self, *, writer) -> None:
        self._writer = writer

    async def record(
        self,
        *,
        tenant_id: str,
        message_id: str,
        user_id: str | None,
        feedback: str,
        comment: str | None,
    ) -> FeedbackResult:
        affected = 0
        for rec in self._writer.records:
            if rec.tenant_id != tenant_id:
                continue
            if rec.request_id != message_id:
                continue
            if user_id is not None and rec.user_id != user_id:
                # cross-user — 본인 logs만 UPDATE 가능 (ADR-017 §5 본인 message 한정)
                continue
            rec.feedback = feedback
            rec.feedback_comment = comment
            affected += 1
        return FeedbackResult(
            tenant_id=tenant_id,
            message_id=message_id,
            user_id=user_id,
            feedback=feedback,
            affected=affected,
        )
