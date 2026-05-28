"""ledger + chat_log_writer dead-letter metric 검증 (ADR-021 후속)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.chat_log_writer import (
    _BestEffortChatLogWriter,
    get_chat_log_failure_metrics,
    reset_chat_log_failure_metrics,
)
from app.services.ledger_client import (
    HttpxLedgerClient,
    LedgerEvent,
    get_ledger_failure_metrics,
    now_iso,
    reset_ledger_failure_metrics,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset():
    reset_ledger_failure_metrics()
    reset_chat_log_failure_metrics()
    yield
    reset_ledger_failure_metrics()
    reset_chat_log_failure_metrics()


def test_ledger_retry_with_backoff(monkeypatch):
    """publish가 max_retries 만큼 시도하고 최종 실패 시 dead-letter 적재."""
    calls = []

    async def fake_post(self, url, *, json=None, headers=None):  # noqa: ARG001
        calls.append(url)
        raise httpx.ConnectError("conn refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = HttpxLedgerClient(
        endpoint="http://ledger.local:8089",
        max_retries=2,
        backoff_base_seconds=0.001,  # 빠른 테스트
    )
    event = LedgerEvent(
        event_type="auth_failure",
        domain_id="t1",
        actor="u",
        reason="bad",
        details={},
        timestamp=now_iso(),
    )

    result = _run(client.publish(event))
    assert result is False
    assert len(calls) == 3  # initial + 2 retries
    metrics = get_ledger_failure_metrics()
    assert metrics["publish_failures_total"] == 1
    assert metrics["dead_letter_count"] == 1
    assert metrics["recent_dead_letters"][0]["event_type"] == "auth_failure"
    assert metrics["recent_dead_letters"][0]["attempts"] == 3


def test_ledger_success_no_metric(monkeypatch):
    async def fake_post(self, url, *, json=None, headers=None):  # noqa: ARG001
        return httpx.Response(202)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = HttpxLedgerClient(
        endpoint="http://ledger.local:8089", max_retries=0
    )
    event = LedgerEvent(
        event_type="x",
        domain_id="t1",
        actor=None,
        reason=None,
        details={},
        timestamp=now_iso(),
    )
    assert _run(client.publish(event)) is True
    assert get_ledger_failure_metrics()["publish_failures_total"] == 0


def test_chat_log_writer_dead_letter_on_failure():
    inner = MagicMock()
    inner.write = AsyncMock(side_effect=RuntimeError("db down"))
    wrapper = _BestEffortChatLogWriter(inner=inner)

    from rag_core.services.chat_log_writer import ChatLogPayload

    payload = ChatLogPayload(
        domain_id="security",
        user_id="u1",
        conversation_id=None,
        request_id="req-1",
        question="안녕",
        answer="안녕",
        retrieved_chunks=[],
        citations=[],
        citation_types=[],
        confidence=0.8,
        ui_mode="chat_structured",
    )

    conv_id = _run(wrapper.write(payload))
    assert conv_id.startswith("unsaved-")
    metrics = get_chat_log_failure_metrics()
    assert metrics["write_failures_total"] == 1
    dl = metrics["recent_dead_letters"][0]
    assert dl["domain_id"] == "security"
    assert dl["request_id"] == "req-1"
    assert dl["exc_type"] == "RuntimeError"


def test_chat_log_writer_success_no_metric():
    inner = MagicMock()
    inner.write = AsyncMock(return_value="conv-123")
    wrapper = _BestEffortChatLogWriter(inner=inner)

    from rag_core.services.chat_log_writer import ChatLogPayload

    payload = ChatLogPayload(
        domain_id="security",
        user_id="u1",
        conversation_id="conv-pre",
        request_id="req-1",
        question="q",
        answer="a",
        retrieved_chunks=[],
        citations=[],
        citation_types=[],
        confidence=0.7,
        ui_mode="chat_structured",
    )

    conv_id = _run(wrapper.write(payload))
    assert conv_id == "conv-123"
    assert get_chat_log_failure_metrics()["write_failures_total"] == 0
