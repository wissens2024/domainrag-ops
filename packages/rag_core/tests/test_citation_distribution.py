"""InMemoryCitationDistributionAnalytics unit tests — ADR-017 §9 분포."""

from __future__ import annotations

import pytest

from rag_core.interfaces.citation_distribution import (
    InMemoryCitationDistributionAnalytics,
)
from rag_core.services.chat_log_writer import ChatLogPayload, InMemoryChatLogWriter


def _payload(*, tenant_id: str, request_id: str, citation_types: list[str]) -> ChatLogPayload:
    return ChatLogPayload(
        tenant_id=tenant_id,
        request_id=request_id,
        user_id="u1",
        conversation_id="c1",
        question="q",
        answer="a",
        citation_types=citation_types,
    )


async def test_distribution_aggregates_tenant_records_in_all_bucket():
    writer = InMemoryChatLogWriter()
    await writer.write(_payload(tenant_id="t1", request_id="r1", citation_types=["direct"]))
    await writer.write(
        _payload(tenant_id="t1", request_id="r2", citation_types=["direct", "synthesis"])
    )
    await writer.write(
        _payload(tenant_id="t1", request_id="r3", citation_types=["inference"])
    )
    # 다른 tenant — 격리
    await writer.write(_payload(tenant_id="t2", request_id="r4", citation_types=["direct"]))

    analytics = InMemoryCitationDistributionAnalytics(writer=writer)
    result = await analytics.distribution(
        tenant_id="t1", from_date=None, to_date=None, group_by="day",
    )
    assert result.granularity == "all"
    assert result.total_messages == 3
    assert len(result.buckets) == 1
    assert result.buckets[0].counts == {
        "direct": 2, "synthesis": 1, "inference": 1
    }


async def test_distribution_empty_tenant_returns_no_buckets():
    writer = InMemoryChatLogWriter()
    analytics = InMemoryCitationDistributionAnalytics(writer=writer)
    result = await analytics.distribution(
        tenant_id="ghost", from_date=None, to_date=None, group_by="hour",
    )
    assert result.total_messages == 0
    assert result.buckets == []
