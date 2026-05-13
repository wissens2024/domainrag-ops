"""InMemoryChatLogEraser unit tests — ADR-020 §10 mask_only/hard_delete 분기."""

from __future__ import annotations

from rag_core.services.chat_log_erasure import (
    ErasureMode,
    InMemoryChatLogEraser,
)
from rag_core.services.chat_log_writer import ChatLogPayload, InMemoryChatLogWriter


def _payload(*, tenant_id: str, user_id: str | None, question: str) -> ChatLogPayload:
    return ChatLogPayload(
        tenant_id=tenant_id,
        request_id=f"req-{question}",
        user_id=user_id,
        conversation_id=None,
        question=question,
        answer=f"answer for {question}",
        citations=[{"chunk_id": "c1"}],
        retrieved_chunks=[{"chunk_id": "c1"}],
        input_pii_found=[{"category": "email"}],
        output_pii_masked=[{"category": "email"}],
        latency_ms=120,
        routing_decision={"endpoint": "tenant_slm"},
    )


async def _seed(writer: InMemoryChatLogWriter) -> None:
    await writer.write(_payload(tenant_id="t1", user_id="u-alice", question="q1"))
    await writer.write(_payload(tenant_id="t1", user_id="u-alice", question="q2"))
    await writer.write(_payload(tenant_id="t1", user_id="u-bob", question="q3"))
    await writer.write(_payload(tenant_id="t2", user_id="u-alice", question="q4"))


async def test_mask_only_preserves_other_users_and_other_tenants():
    writer = InMemoryChatLogWriter()
    await _seed(writer)
    eraser = InMemoryChatLogEraser(writer=writer)

    result = await eraser.erase_my_logs(
        tenant_id="t1",
        user_id="u-alice",
        mode=ErasureMode.MASK_ONLY,
        reason="user request",
    )

    assert result.affected_rows == 2
    assert result.mode == ErasureMode.MASK_ONLY

    # u-alice / t1 logs는 마스킹 적용. user_id NULL, question/answer NULL.
    alice_t1 = [r for r in writer.records if r.tenant_id == "t1" and r.user_id is None]
    assert len(alice_t1) == 2
    for r in alice_t1:
        assert r.question is None
        assert r.answer is None
        assert r.citations == []
        assert r.retrieved_chunks == []
        assert r.input_pii_found == []
        assert r.output_pii_masked == []
        assert r.pii_storage_policy == "erased"
        # 운영 지표는 보존
        assert r.latency_ms == 120
        assert r.routing_decision == {"endpoint": "tenant_slm"}

    # u-bob / t1 logs는 손대지 않음
    bob = [r for r in writer.records if r.user_id == "u-bob"]
    assert len(bob) == 1
    assert bob[0].question == "q3"

    # 다른 tenant의 u-alice도 보존
    alice_t2 = [r for r in writer.records if r.tenant_id == "t2"]
    assert len(alice_t2) == 1
    assert alice_t2[0].user_id == "u-alice"
    assert alice_t2[0].question == "q4"


async def test_hard_delete_removes_rows():
    writer = InMemoryChatLogWriter()
    await _seed(writer)
    eraser = InMemoryChatLogEraser(writer=writer)

    result = await eraser.erase_my_logs(
        tenant_id="t1",
        user_id="u-alice",
        mode=ErasureMode.HARD_DELETE,
        reason="hard purge",
    )

    assert result.affected_rows == 2
    assert result.mode == ErasureMode.HARD_DELETE
    assert all(
        not (r.tenant_id == "t1" and r.user_id == "u-alice") for r in writer.records
    )
    # u-bob t1 + u-alice t2 = 2건 남음
    assert len(writer.records) == 2


async def test_erase_with_no_matches_returns_zero():
    writer = InMemoryChatLogWriter()
    await _seed(writer)
    eraser = InMemoryChatLogEraser(writer=writer)

    result = await eraser.erase_my_logs(
        tenant_id="t1",
        user_id="u-charlie",
        mode=ErasureMode.MASK_ONLY,
        reason="no match",
    )
    assert result.affected_rows == 0
    # 다른 records는 변동 없음
    assert len([r for r in writer.records if r.question == "q1"]) == 1
