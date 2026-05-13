"""InMemoryDashboardAnalytics unit tests — ADR-017 §10 12 메트릭.

InMemory 4-store(chat_log_writer + document_repo + chunk_repo + job_repo) 위에서
aggregate 정확성 검증. InMemory는 created_at 없음 — 모두 'today'로 간주.
"""

from __future__ import annotations

import pytest

from rag_core.clients.in_memory import (
    InMemoryChunkRepository,
    InMemoryDocumentRepository,
    InMemoryIndexingJobRepository,
)
from rag_core.interfaces.chunk_repository import (
    ChunkRecord,
    DocumentRecord,
    IndexingJobRecord,
)
from rag_core.interfaces.dashboard_analytics import (
    InMemoryDashboardAnalytics,
    _routing_distribution_key,
)
from rag_core.services.chat_log_writer import ChatLogPayload, InMemoryChatLogWriter


def _doc(tenant_id: str, doc_id: str, version: str = "v1") -> DocumentRecord:
    return DocumentRecord(tenant_id=tenant_id, doc_id=doc_id, title=doc_id, version=version)


def _chunk(tenant_id: str, doc_id: str, idx: int, *, archived: bool = False) -> ChunkRecord:
    rec = ChunkRecord(
        tenant_id=tenant_id, chunk_id=f"{doc_id}:c{idx}", doc_id=doc_id,
        doc_version="v1", parser_version="p1", chunk_strategy="fixed",
        chunk_index=idx, content="x", title=None, page_number=None,
        section_title=None, heading_path=[], content_hash=None,
        start_char=None, end_char=None, token_count=10,
        department=None, doc_type=None, security_level="internal",
    )
    if archived:
        rec.archived_at = "2026-05-01T00:00:00+09:00"  # truthy
    return rec


def _payload(
    *,
    tenant_id: str,
    request_id: str,
    citation_types: list[str] | None = None,
    citations: list | None = None,
    fallback_reason: str | None = None,
    routing_decision: dict | None = None,
    latency_ms: int | None = 100,
) -> ChatLogPayload:
    return ChatLogPayload(
        tenant_id=tenant_id, request_id=request_id, user_id="u1",
        conversation_id="c1", question="q", answer="a",
        citation_types=citation_types or [],
        citations=citations if citations is not None else [{"chunk_id": "c1"}],
        retrieved_chunks=[{"chunk_id": "c1"}],
        latency_ms=latency_ms,
        fallback_reason=fallback_reason,
        routing_decision=routing_decision or {
            "selected_model": "tenant_slm",
            "selected_lora": "security-policy-v1",
        },
    )


@pytest.fixture
async def analytics():
    writer = InMemoryChatLogWriter()
    docs = InMemoryDocumentRepository()
    chunks = InMemoryChunkRepository()
    jobs = InMemoryIndexingJobRepository()

    # documents — 같은 doc_id의 v1/v2 둘 다 카운트 (total = 4 rows)
    await docs.upsert(_doc("t1", "DOC-1"))
    await docs.upsert(_doc("t1", "DOC-2"))
    await docs.upsert(_doc("t1", "DOC-3"))
    await docs.upsert(_doc("t2", "DOC-X"))  # 다른 tenant — 격리

    # chunks — DOC-1: 3 (1개 archived), DOC-2: 2
    await chunks.replace_chunks(
        tenant_id="t1", doc_id="DOC-1", doc_version="v1",
        parser_version="p1",
        chunks=[_chunk("t1", "DOC-1", 0), _chunk("t1", "DOC-1", 1, archived=True),
                _chunk("t1", "DOC-1", 2)],
    )
    await chunks.replace_chunks(
        tenant_id="t1", doc_id="DOC-2", doc_version="v1",
        parser_version="p1",
        chunks=[_chunk("t1", "DOC-2", 0), _chunk("t1", "DOC-2", 1)],
    )
    # 다른 tenant chunks
    await chunks.replace_chunks(
        tenant_id="t2", doc_id="DOC-X", doc_version="v1",
        parser_version="p1",
        chunks=[_chunk("t2", "DOC-X", 0)],
    )

    # indexing jobs — 2 completed, 1 failed
    await jobs.create(IndexingJobRecord(
        job_id="J-1", tenant_id="t1", doc_id="DOC-1", doc_version="v1", status="completed"
    ))
    await jobs.create(IndexingJobRecord(
        job_id="J-2", tenant_id="t1", doc_id="DOC-2", doc_version="v1", status="completed"
    ))
    await jobs.create(IndexingJobRecord(
        job_id="J-3", tenant_id="t1", doc_id="DOC-3", doc_version="v1", status="failed"
    ))
    # 다른 tenant
    await jobs.create(IndexingJobRecord(
        job_id="J-X", tenant_id="t2", doc_id="DOC-X", doc_version="v1", status="completed"
    ))

    # chat_logs
    await writer.write(_payload(
        tenant_id="t1", request_id="r1", citation_types=["direct"], latency_ms=100,
    ))
    await writer.write(_payload(
        tenant_id="t1", request_id="r2", citation_types=["direct", "synthesis"],
        latency_ms=200,
    ))
    # 인용 없는 답변
    await writer.write(_payload(
        tenant_id="t1", request_id="r3", citation_types=[], citations=[],
        latency_ms=300,
    ))
    # fallback (citations 빈 — answers_without_citation 누적)
    await writer.write(_payload(
        tenant_id="t1", request_id="r4", citation_types=[], citations=[],
        fallback_reason="low_retrieval", latency_ms=50,
    ))
    await writer.write(_payload(
        tenant_id="t1", request_id="r5", citation_types=[], citations=[],
        fallback_reason="low_retrieval", latency_ms=80,
    ))
    # shared_llm routing
    await writer.write(_payload(
        tenant_id="t1", request_id="r6", citation_types=["inference"],
        routing_decision={"selected_model": "shared_llm", "selected_lora": None},
        latency_ms=400,
    ))
    # 다른 tenant
    await writer.write(_payload(tenant_id="t2", request_id="rX"))

    return InMemoryDashboardAnalytics(
        chat_log_writer=writer,
        document_repo=docs,
        chunk_repo=chunks,
        indexing_job_repo=jobs,
    )


async def test_total_documents_tenant_scope(analytics):
    snap = await analytics.get_snapshot(tenant_id="t1")
    assert snap.total_documents == 3
    # t2의 DOC-X는 미포함
    snap_t2 = await analytics.get_snapshot(tenant_id="t2")
    assert snap_t2.total_documents == 1


async def test_total_chunks_excludes_archived(analytics):
    snap = await analytics.get_snapshot(tenant_id="t1")
    # DOC-1: 2 active (1 archived) + DOC-2: 2 → 4
    assert snap.total_chunks == 4


async def test_indexing_jobs_counts_by_status(analytics):
    snap = await analytics.get_snapshot(tenant_id="t1")
    assert snap.indexing_completed_today == 2
    assert snap.indexing_failed_today == 1


async def test_questions_today_and_latency(analytics):
    snap = await analytics.get_snapshot(tenant_id="t1")
    # r1..r6 = 6건
    assert snap.questions_today == 6
    # avg = (100+200+300+50+80+400)/6 ≈ 188.33
    assert snap.avg_latency_ms == pytest.approx((100 + 200 + 300 + 50 + 80 + 400) / 6)


async def test_answers_without_citation(analytics):
    snap = await analytics.get_snapshot(tenant_id="t1")
    # r3, r4, r5 = 3건 (citations 빈 배열)
    assert snap.answers_without_citation == 3


async def test_citation_type_distribution_row_unique(analytics):
    snap = await analytics.get_snapshot(tenant_id="t1")
    # r1: direct / r2: direct+synthesis / r6: inference
    assert snap.citation_type_distribution == {
        "direct": 2, "synthesis": 1, "inference": 1
    }


async def test_fallback_distribution(analytics):
    snap = await analytics.get_snapshot(tenant_id="t1")
    assert snap.fallback_distribution == {"low_retrieval": 2}


async def test_routing_distribution_with_lora_combos(analytics):
    snap = await analytics.get_snapshot(tenant_id="t1")
    # tenant_slm + lora 5건(r1~r5), shared_llm 1건(r6)
    assert snap.routing_distribution == {
        "tenant_slm_lora": 5, "shared_llm": 1
    }


async def test_negative_feedback_rate_zero_without_feedback_storage(analytics):
    """InMemoryChatLogPayload는 feedback 컬럼 없음 — 0.0."""
    snap = await analytics.get_snapshot(tenant_id="t1")
    assert snap.negative_feedback_rate == 0.0


def test_routing_distribution_key_handles_none():
    assert _routing_distribution_key(None) is None
    assert _routing_distribution_key({}) is None
    assert _routing_distribution_key({"selected_model": "tenant_slm"}) == "tenant_slm_no_lora"
    assert _routing_distribution_key(
        {"selected_model": "tenant_slm", "selected_lora": "x"}
    ) == "tenant_slm_lora"
    assert _routing_distribution_key(
        {"selected_model": "shared_llm", "selected_lora": None}
    ) == "shared_llm"


async def test_empty_tenant_returns_zeros():
    """데이터 없는 tenant → 모든 카운트 0, 분포 빈 dict."""
    writer = InMemoryChatLogWriter()
    docs = InMemoryDocumentRepository()
    chunks = InMemoryChunkRepository()
    jobs = InMemoryIndexingJobRepository()
    analytics = InMemoryDashboardAnalytics(
        chat_log_writer=writer, document_repo=docs,
        chunk_repo=chunks, indexing_job_repo=jobs,
    )
    snap = await analytics.get_snapshot(tenant_id="ghost")
    assert snap.total_documents == 0
    assert snap.total_chunks == 0
    assert snap.questions_today == 0
    assert snap.avg_latency_ms == 0.0
    assert snap.citation_type_distribution == {}
    assert snap.fallback_distribution == {}
    assert snap.routing_distribution == {}
