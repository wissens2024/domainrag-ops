"""InMemoryConversationRepository unit tests — ADR-017 §4 CRUD.

writer.records 위에서 grouping/title override/delete가 작동하는지 검증.
"""

from __future__ import annotations

import pytest

from rag_core.interfaces.conversation_repository import (
    InMemoryConversationRepository,
)
from rag_core.services.chat_log_writer import ChatLogPayload, InMemoryChatLogWriter


def _payload(
    *,
    tenant_id: str = "t1",
    request_id: str,
    user_id: str | None = "u-alice",
    conversation_id: str,
    question: str = "기본 질문",
) -> ChatLogPayload:
    return ChatLogPayload(
        tenant_id=tenant_id,
        request_id=request_id,
        user_id=user_id,
        conversation_id=conversation_id,
        question=question,
        answer="답변",
    )


@pytest.fixture
async def repo():
    writer = InMemoryChatLogWriter()
    # conv-A: 2 messages (alice)
    await writer.write(_payload(request_id="r1", conversation_id="conv-A",
                                question="패스워드 정책"))
    await writer.write(_payload(request_id="r2", conversation_id="conv-A",
                                question="추가 질문"))
    # conv-B: 1 message (alice)
    await writer.write(_payload(request_id="r3", conversation_id="conv-B",
                                question="VPN 설정"))
    # 다른 user
    await writer.write(_payload(request_id="r4", conversation_id="conv-C",
                                user_id="u-bob", question="bob 질문"))
    # 다른 tenant
    await writer.write(_payload(request_id="r5", conversation_id="conv-D",
                                tenant_id="t2", question="t2 질문"))
    return InMemoryConversationRepository(writer=writer)


async def test_list_returns_user_conversations_latest_first(repo):
    result = await repo.list_by_user(tenant_id="t1", user_id="u-alice")
    assert result.total == 2
    ids = [c.id for c in result.items]
    # 최신순(records의 마지막 append가 first) — conv-B가 더 최근
    assert ids == ["conv-B", "conv-A"]
    # 다른 user/tenant 제외
    assert all(c.user_id == "u-alice" for c in result.items)
    assert all(c.tenant_id == "t1" for c in result.items)


async def test_list_message_count(repo):
    result = await repo.list_by_user(tenant_id="t1", user_id="u-alice")
    by_id = {c.id: c for c in result.items}
    assert by_id["conv-A"].message_count == 2
    assert by_id["conv-B"].message_count == 1


async def test_default_title_is_first_question_truncated(repo):
    result = await repo.list_by_user(tenant_id="t1", user_id="u-alice")
    by_id = {c.id: c for c in result.items}
    assert by_id["conv-A"].title == "패스워드 정책"
    assert by_id["conv-B"].title == "VPN 설정"


async def test_get_returns_record(repo):
    record = await repo.get(tenant_id="t1", user_id="u-alice", conversation_id="conv-A")
    assert record is not None
    assert record.id == "conv-A"
    assert record.message_count == 2


async def test_get_returns_none_for_other_user(repo):
    """conv-C는 u-bob 소유 — alice가 조회 시 None."""
    record = await repo.get(
        tenant_id="t1", user_id="u-alice", conversation_id="conv-C"
    )
    assert record is None


async def test_get_returns_none_for_other_tenant(repo):
    record = await repo.get(
        tenant_id="t1", user_id="u-alice", conversation_id="conv-D"
    )
    assert record is None


async def test_update_title_overrides_default(repo):
    record = await repo.update_title(
        tenant_id="t1", user_id="u-alice",
        conversation_id="conv-A", title="새 제목",
    )
    assert record is not None
    assert record.title == "새 제목"
    # 재조회 시도 동일
    again = await repo.get(
        tenant_id="t1", user_id="u-alice", conversation_id="conv-A"
    )
    assert again.title == "새 제목"


async def test_update_title_returns_none_for_other_user(repo):
    record = await repo.update_title(
        tenant_id="t1", user_id="u-alice",
        conversation_id="conv-C", title="hijack",
    )
    assert record is None


async def test_delete_hides_conversation(repo):
    affected = await repo.delete(
        tenant_id="t1", user_id="u-alice", conversation_id="conv-A"
    )
    assert affected == 1
    # 이후 list/get에서 미노출
    result = await repo.list_by_user(tenant_id="t1", user_id="u-alice")
    assert "conv-A" not in [c.id for c in result.items]
    assert await repo.get(
        tenant_id="t1", user_id="u-alice", conversation_id="conv-A"
    ) is None


async def test_delete_other_user_returns_zero(repo):
    affected = await repo.delete(
        tenant_id="t1", user_id="u-alice", conversation_id="conv-C"
    )
    assert affected == 0


async def test_delete_idempotent(repo):
    await repo.delete(tenant_id="t1", user_id="u-alice", conversation_id="conv-A")
    affected = await repo.delete(
        tenant_id="t1", user_id="u-alice", conversation_id="conv-A"
    )
    assert affected == 0
