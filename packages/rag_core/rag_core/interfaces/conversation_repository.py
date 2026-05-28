"""ConversationRepository — ADR-017 §4 conversations CRUD Protocol.

운영 구현은 backend의 PostgresConversationRepository (SQLAlchemy + RLS).
InMemory variant는 InMemoryChatLogWriter.records 위에서 동작:
  - 대화 목록은 records로부터 (domain_id, user_id, conversation_id) grouping
  - 제목은 별도 in-process override 사전 (PATCH가 채움). override 없으면 첫
    질문의 첫 80자.
  - 삭제는 in-process set. 다시 list/get할 때 제외.
  - chat_logs는 audit truth이므로 conversation 삭제 시 함께 삭제하지 않는다
    (ADR-020 §10 right-to-erasure는 별도 endpoint).

본 모듈은 단순 CRUD Protocol과 dataclass만 정의. 메시지 상세 노출은 caller가
ChatLogReader로 별도 lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class ConversationRecord:
    id: str
    domain_id: str
    user_id: str
    title: str | None
    created_at: datetime | None
    updated_at: datetime | None
    message_count: int = 0  # admin/list에 노출, detail에서는 무의미


@dataclass
class ConversationListResult:
    items: list[ConversationRecord]
    total: int
    page: int
    page_size: int


class ConversationRepository(Protocol):
    async def list_by_user(
        self,
        *,
        domain_id: str,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> ConversationListResult: ...

    async def get(
        self, *, domain_id: str, user_id: str, conversation_id: str
    ) -> ConversationRecord | None: ...

    async def update_title(
        self,
        *,
        domain_id: str,
        user_id: str,
        conversation_id: str,
        title: str,
    ) -> ConversationRecord | None:
        """본인 conversation 제목 갱신. None이면 미발견(404).

        UPDATE conversations SET title=:t, updated_at=NOW() WHERE id+tenant+user
        """
        ...

    async def delete(
        self,
        *,
        domain_id: str,
        user_id: str,
        conversation_id: str,
    ) -> int:
        """본인 conversation 삭제. RETURNING rowcount. 0이면 미발견.

        Postgres는 conversations row만 삭제(CASCADE로 messages 삭제). chat_logs는
        보존(audit truth + admin /logs/chat에서 여전히 조회 가능).
        """
        ...


# --------------------------------------------------------------------------- #
# InMemory variant — InMemoryChatLogWriter.records 공유
# --------------------------------------------------------------------------- #


class InMemoryConversationRepository:
    """InMemoryChatLogWriter.records 위에서 동작하는 in-memory variant.

    Args:
        writer: InMemoryChatLogWriter — records 공유.

    제목 override는 dict, 삭제는 set으로 추적. records 자체는 chat_log_writer가
    관리한다.
    """

    def __init__(self, *, writer) -> None:
        self._writer = writer
        self._title_overrides: dict[str, str] = {}
        self._deleted: set[str] = set()

    def _conversations_for(self, domain_id: str, user_id: str) -> dict[str, list]:
        groups: dict[str, list] = {}
        for r in self._writer.records:
            if r.domain_id != domain_id or r.user_id != user_id:
                continue
            if not r.conversation_id or r.conversation_id in self._deleted:
                continue
            groups.setdefault(r.conversation_id, []).append(r)
        return groups

    def _record_from_group(
        self, domain_id: str, user_id: str, cid: str, recs: list
    ) -> ConversationRecord:
        title = self._title_overrides.get(cid)
        if title is None and recs:
            first_q = recs[0].question or ""
            title = first_q[:80] or None
        return ConversationRecord(
            id=cid,
            domain_id=domain_id,
            user_id=user_id,
            title=title,
            created_at=None,
            updated_at=None,
            message_count=len(recs),
        )

    async def list_by_user(
        self,
        *,
        domain_id: str,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> ConversationListResult:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        groups = self._conversations_for(domain_id, user_id)
        rows = [
            self._record_from_group(domain_id, user_id, cid, recs)
            for cid, recs in groups.items()
        ]
        # InMemory에는 timestamp 없음 — 등장 순(records append 순)의 역순
        # 즉 records 마지막에 추가된 conversation이 가장 위로 오도록.
        ordering = []
        for r in self._writer.records:
            if (
                r.domain_id == domain_id
                and r.user_id == user_id
                and r.conversation_id in groups
                and r.conversation_id not in ordering
            ):
                ordering.append(r.conversation_id)
        ordering.reverse()
        # 정렬
        rows.sort(
            key=lambda x: ordering.index(x.id) if x.id in ordering else len(ordering)
        )
        total = len(rows)
        start = (page - 1) * page_size
        return ConversationListResult(
            items=rows[start : start + page_size],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get(
        self, *, domain_id: str, user_id: str, conversation_id: str
    ) -> ConversationRecord | None:
        if conversation_id in self._deleted:
            return None
        groups = self._conversations_for(domain_id, user_id)
        recs = groups.get(conversation_id)
        if not recs:
            return None
        return self._record_from_group(domain_id, user_id, conversation_id, recs)

    async def update_title(
        self,
        *,
        domain_id: str,
        user_id: str,
        conversation_id: str,
        title: str,
    ) -> ConversationRecord | None:
        if conversation_id in self._deleted:
            return None
        groups = self._conversations_for(domain_id, user_id)
        if conversation_id not in groups:
            return None
        self._title_overrides[conversation_id] = title
        return self._record_from_group(
            domain_id, user_id, conversation_id, groups[conversation_id]
        )

    async def delete(
        self,
        *,
        domain_id: str,
        user_id: str,
        conversation_id: str,
    ) -> int:
        if conversation_id in self._deleted:
            return 0
        groups = self._conversations_for(domain_id, user_id)
        if conversation_id not in groups:
            return 0
        self._deleted.add(conversation_id)
        return 1
