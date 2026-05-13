"""InMemoryChatLogReader unit tests — ADR-017 §8 list/get + filters.

InMemoryChatLogWriter.records 위에서 동작하는 reader가 tenant/필터/페이징을 모두
정상 처리하는지 검증. created_at은 InMemory에서 None — date filter는 skip 동작.
"""

from __future__ import annotations

import pytest

from rag_core.interfaces.chat_log_reader import (
    ChatLogListFilters,
    InMemoryChatLogReader,
)
from rag_core.services.chat_log_writer import ChatLogPayload, InMemoryChatLogWriter


def _payload(
    *,
    tenant_id: str,
    request_id: str,
    user_id: str | None = "u-alice",
    question: str = "기본 질문",
    answer: str = "기본 답변",
    citation_types: list[str] | None = None,
    confidence: float = 0.7,
    fallback_reason: str | None = None,
    ui_mode: str = "chat_structured",
    conversation_id: str | None = "conv-1",
) -> ChatLogPayload:
    return ChatLogPayload(
        tenant_id=tenant_id,
        request_id=request_id,
        user_id=user_id,
        conversation_id=conversation_id,
        question=question,
        answer=answer,
        rewritten_query=None,
        citations=[{"chunk_id": "c1"}],
        citation_types=citation_types or [],
        retrieved_chunks=[{"chunk_id": "c1", "content": "..."}],
        verifier_metrics={"tier2_avg_similarity": 0.81},
        routing_decision={"endpoint": "tenant_slm"},
        classifier_decision={"query_type": "document_qa"},
        confidence=confidence,
        fallback_reason=fallback_reason,
        ui_mode=ui_mode,
    )


@pytest.fixture
async def reader() -> InMemoryChatLogReader:
    writer = InMemoryChatLogWriter()
    # 시간순 (오래된 → 최신) — list_by_tenant는 reverse하여 최신순 반환
    await writer.write(_payload(tenant_id="t1", request_id="r1", question="비밀번호 정책"))
    await writer.write(
        _payload(tenant_id="t1", request_id="r2", question="VPN 설정",
                 citation_types=["direct"])
    )
    await writer.write(
        _payload(tenant_id="t1", request_id="r3", question="암호화 절차",
                 citation_types=["direct", "synthesis"], user_id="u-bob")
    )
    await writer.write(
        _payload(tenant_id="t1", request_id="r4", question="유출 대응",
                 fallback_reason="low_retrieval", confidence=0.2)
    )
    await writer.write(
        _payload(tenant_id="t1", request_id="r5", question="감사 로그",
                 ui_mode="chat_streaming")
    )
    # 다른 tenant — 격리 검증
    await writer.write(_payload(tenant_id="t2", request_id="r6", question="법무 가이드"))
    return InMemoryChatLogReader(writer=writer)


async def test_list_returns_tenant_scoped_latest_first(reader):
    result = await reader.list_by_tenant(
        tenant_id="t1", filters=ChatLogListFilters(), page=1, page_size=10
    )
    assert result.total == 5
    assert result.page == 1
    request_ids = [r.request_id for r in result.items]
    # 최신순 (마지막 write가 first)
    assert request_ids == ["r5", "r4", "r3", "r2", "r1"]
    # 다른 tenant 포함 없음
    assert all(r.tenant_id == "t1" for r in result.items)


async def test_list_filters_user_id(reader):
    result = await reader.list_by_tenant(
        tenant_id="t1",
        filters=ChatLogListFilters(user_id="u-bob"),
        page=1, page_size=10,
    )
    assert result.total == 1
    assert result.items[0].request_id == "r3"


async def test_list_filters_fallback_only(reader):
    result = await reader.list_by_tenant(
        tenant_id="t1",
        filters=ChatLogListFilters(fallback_only=True),
        page=1, page_size=10,
    )
    assert result.total == 1
    assert result.items[0].request_id == "r4"
    assert result.items[0].fallback_reason == "low_retrieval"


async def test_list_filters_citation_type(reader):
    result = await reader.list_by_tenant(
        tenant_id="t1",
        filters=ChatLogListFilters(citation_type="synthesis"),
        page=1, page_size=10,
    )
    assert result.total == 1
    assert result.items[0].request_id == "r3"


async def test_list_filters_ui_mode(reader):
    result = await reader.list_by_tenant(
        tenant_id="t1",
        filters=ChatLogListFilters(ui_mode="chat_streaming"),
        page=1, page_size=10,
    )
    assert result.total == 1
    assert result.items[0].request_id == "r5"


async def test_list_filters_keyword_matches_question_or_answer(reader):
    result = await reader.list_by_tenant(
        tenant_id="t1",
        filters=ChatLogListFilters(keyword="VPN"),
        page=1, page_size=10,
    )
    assert result.total == 1
    assert result.items[0].request_id == "r2"


async def test_list_filters_min_confidence(reader):
    result = await reader.list_by_tenant(
        tenant_id="t1",
        filters=ChatLogListFilters(min_confidence=0.5),
        page=1, page_size=10,
    )
    # r4(0.2)는 제외, 나머지 4건(0.7)
    assert result.total == 4
    assert "r4" not in [r.request_id for r in result.items]


async def test_list_pagination(reader):
    page1 = await reader.list_by_tenant(
        tenant_id="t1", filters=ChatLogListFilters(), page=1, page_size=2
    )
    assert page1.total == 5
    assert [r.request_id for r in page1.items] == ["r5", "r4"]

    page2 = await reader.list_by_tenant(
        tenant_id="t1", filters=ChatLogListFilters(), page=2, page_size=2
    )
    assert [r.request_id for r in page2.items] == ["r3", "r2"]

    page3 = await reader.list_by_tenant(
        tenant_id="t1", filters=ChatLogListFilters(), page=3, page_size=2
    )
    assert [r.request_id for r in page3.items] == ["r1"]


async def test_get_returns_record_with_all_columns(reader):
    record = await reader.get(tenant_id="t1", request_id="r2")
    assert record is not None
    assert record.question == "VPN 설정"
    assert record.citations == [{"chunk_id": "c1"}]
    assert record.citation_types == ["direct"]
    assert record.routing_decision == {"endpoint": "tenant_slm"}
    assert record.verifier_metrics == {"tier2_avg_similarity": 0.81}


async def test_get_returns_none_on_unknown_request_id(reader):
    assert await reader.get(tenant_id="t1", request_id="r-nope") is None


async def test_get_isolates_tenants(reader):
    # r6은 tenant t2 — t1 scope에서 조회 시 미발견
    assert await reader.get(tenant_id="t1", request_id="r6") is None
    other = await reader.get(tenant_id="t2", request_id="r6")
    assert other is not None
    assert other.tenant_id == "t2"
