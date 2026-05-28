"""InMemoryFeedbackWriter unit tests — ADR-017 §5 본인 message UPDATE."""

from __future__ import annotations

import pytest

from rag_core.interfaces.feedback_writer import InMemoryFeedbackWriter
from rag_core.services.chat_log_writer import ChatLogPayload, InMemoryChatLogWriter


def _payload(*, domain_id: str, request_id: str, user_id: str | None) -> ChatLogPayload:
    return ChatLogPayload(
        domain_id=domain_id,
        request_id=request_id,
        user_id=user_id,
        conversation_id="c1",
        question="q",
        answer="a",
    )


async def _seed(writer: InMemoryChatLogWriter) -> None:
    await writer.write(_payload(domain_id="t1", request_id="m1", user_id="u-alice"))
    await writer.write(_payload(domain_id="t1", request_id="m2", user_id="u-bob"))
    await writer.write(_payload(domain_id="t2", request_id="m3", user_id="u-alice"))


async def test_record_updates_owner_message():
    writer = InMemoryChatLogWriter()
    await _seed(writer)
    fb = InMemoryFeedbackWriter(writer=writer)

    result = await fb.record(
        domain_id="t1", message_id="m1", user_id="u-alice",
        feedback="good", comment="helpful",
    )
    assert result.affected == 1
    target = next(r for r in writer.records if r.request_id == "m1")
    assert target.feedback == "good"
    assert target.feedback_comment == "helpful"


async def test_record_cross_user_returns_zero_affected():
    """다른 user_id의 message에는 피드백 못 단다 — 404 변환."""
    writer = InMemoryChatLogWriter()
    await _seed(writer)
    fb = InMemoryFeedbackWriter(writer=writer)

    result = await fb.record(
        domain_id="t1", message_id="m2", user_id="u-alice",  # m2의 owner는 u-bob
        feedback="bad", comment=None,
    )
    assert result.affected == 0
    # u-bob의 message는 변동 없음
    bobs = [r for r in writer.records if r.request_id == "m2"]
    assert bobs[0].feedback is None


async def test_record_cross_tenant_returns_zero():
    writer = InMemoryChatLogWriter()
    await _seed(writer)
    fb = InMemoryFeedbackWriter(writer=writer)
    # m3은 tenant t2 — t1 scope에서 못 찾는다
    result = await fb.record(
        domain_id="t1", message_id="m3", user_id="u-alice",
        feedback="bad", comment=None,
    )
    assert result.affected == 0


async def test_record_unknown_message_returns_zero():
    writer = InMemoryChatLogWriter()
    await _seed(writer)
    fb = InMemoryFeedbackWriter(writer=writer)
    result = await fb.record(
        domain_id="t1", message_id="nope", user_id="u-alice",
        feedback="good", comment=None,
    )
    assert result.affected == 0


async def test_record_idempotent_overwrites_previous():
    writer = InMemoryChatLogWriter()
    await _seed(writer)
    fb = InMemoryFeedbackWriter(writer=writer)
    await fb.record(
        domain_id="t1", message_id="m1", user_id="u-alice",
        feedback="good", comment="first",
    )
    await fb.record(
        domain_id="t1", message_id="m1", user_id="u-alice",
        feedback="bad", comment="reconsidered",
    )
    target = next(r for r in writer.records if r.request_id == "m1")
    assert target.feedback == "bad"
    assert target.feedback_comment == "reconsidered"
