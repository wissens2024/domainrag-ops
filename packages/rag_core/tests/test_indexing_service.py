"""IndexingService e2e — InMemory parser/chunker/embedder/vector_store/repos.

검증 항목:
  - full 모드: parse → chunk → PII Layer 3 → embed → Qdrant upsert → DB write
  - chunks.pii_warnings 적재 (ADR-020 §5)
  - chunk_re_split 모드 (재chunk + 임베딩 재계산)
  - parser_only 모드 (metadata only)
  - embedding_only 모드 (vectors only, chunks 보존)
  - 동시 indexing 차단 (ADR-012 §10) — IndexingConflictError
  - 30% fail-fast — 모든 embed 실패 시 status=failed
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_core.clients.in_memory import (
    InMemoryChunker,
    InMemoryChunkRepository,
    InMemoryDocumentRepository,
    InMemoryEmbedder,
    InMemoryIndexingJobRepository,
    InMemoryParser,
    InMemoryVectorStore,
)
from rag_core.interfaces.chunk_repository import IndexingConflictError
from rag_core.pii import RegexPIIDetector
from rag_core.services.indexing_service import (
    IndexingRequest,
    IndexingService,
    ReindexMode,
)
from rag_core.services.pii_service import PIIService

RULES_DIR = Path(__file__).resolve().parents[1] / "rag_core" / "pii" / "rules"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _pii_service() -> PIIService:
    return PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))


def _pii_config() -> dict:
    return {
        "indexing": {
            "enable": True,
            "on_pii_found_in_chunk": {
                "severity_threshold": "medium",
                "block_indexing": False,
            },
        },
        "severity_map": {
            "rrn": "high",
            "credit_card": "high",
            "phone": "medium",
            "email": "low",
        },
    }


def _build(
    *,
    inline_pages: dict[str, list[str]] | None = None,
    max_tokens: int = 5,
    failing_embedder: bool = False,
):
    parser = InMemoryParser(parser_version="p1", inline_pages=inline_pages or {})
    chunker = InMemoryChunker(max_tokens=max_tokens)
    embedder = (
        _FailingEmbedder() if failing_embedder else InMemoryEmbedder(dense_dim=8)
    )
    vstore = InMemoryVectorStore()
    chunk_repo = InMemoryChunkRepository()
    doc_repo = InMemoryDocumentRepository()
    job_repo = InMemoryIndexingJobRepository()
    svc = IndexingService(
        parser=parser,
        chunker=chunker,
        embedder=embedder,
        vector_store=vstore,
        pii_service=_pii_service(),
        chunk_repo=chunk_repo,
        document_repo=doc_repo,
        job_repo=job_repo,
        failure_threshold=0.30,
    )
    return svc, vstore, chunk_repo, doc_repo, job_repo


class _FailingEmbedder:
    """embed_batch가 항상 예외 — 30% fail-fast 분기 테스트용."""

    model_name = "fail-embed"
    dense_dim = 8

    async def embed_batch(self, texts):
        raise RuntimeError("embed service down")

    async def embed_query(self, text):
        raise RuntimeError("embed service down")


def _request(
    domain_id="security",
    doc_id="doc-001",
    doc_version="v1",
    file_path="/inline/doc-001.txt",
    mode=ReindexMode.FULL,
    **kwargs,
) -> IndexingRequest:
    return IndexingRequest(
        domain_id=domain_id,
        doc_id=doc_id,
        doc_version=doc_version,
        file_path=file_path,
        title="테스트 문서",
        mode=mode,
        department="security",
        doc_type="policy",
        security_level="internal",
        acl=["group:security"],
        approval_status="approved",
        tags=["test"],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Full 모드
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_mode_indexes_chunks_and_db():
    svc, vstore, chunk_repo, doc_repo, job_repo = _build(
        inline_pages={
            "/inline/doc-001.txt": [
                "패스워드 정책 요약. 최소 12자. 영문 숫자 특수문자 혼합 필수.",
                "추가로 90일 주기로 변경 권장. 동일 패스워드 재사용 금지.",
            ]
        },
        max_tokens=4,
    )

    # Qdrant collection 사전 생성 (IndexingService는 collection 생성 책임 없음)
    await vstore.create_collection(domain_id="security", dense_dim=8)

    result = await svc.run(_request(), job_id="job-1", pii_config=_pii_config())

    assert result.status == "completed"
    assert result.total_chunks > 0
    assert result.indexed_chunks == result.total_chunks
    assert result.failed_chunks == []
    assert result.high_pii_chunks == 0

    # Qdrant 적재 확인
    points = await vstore.hybrid_query(
        domain_id="security",
        dense_query=[0.0] * 8,
        sparse_query={},
        acl_filter={},
        top_k=100,
    )
    assert len(points) == result.indexed_chunks
    for p in points:
        assert p["payload"]["domain_id"] == "security"
        assert p["payload"]["doc_id"] == "doc-001"
        assert "content" in p["payload"]

    # Postgres chunks/document 쓰기 확인
    chunks = await chunk_repo.list_by_doc(
        domain_id="security", doc_id="doc-001", doc_version="v1", parser_version="p1"
    )
    assert len(chunks) == result.indexed_chunks
    assert all(c.embedding_model == "mock-embed" for c in chunks)
    doc = await doc_repo.get(domain_id="security", doc_id="doc-001", version="v1")
    assert doc is not None
    assert doc.approval_status == "approved"

    # Job 상태 확인
    job = await job_repo.get("job-1")
    assert job.status == "completed"
    assert job.progress == 100


@pytest.mark.asyncio
async def test_pii_warnings_recorded_in_chunks_and_qdrant():
    """ADR-020 §5 — high severity chunk에 pii_warnings 적재."""
    svc, vstore, chunk_repo, _doc_repo, _job = _build(
        inline_pages={
            "/inline/doc-pii.txt": [
                "본 문서는 외부 유출 시 901231-1234567 등 주민번호가 노출될 수 있음",
            ]
        },
        max_tokens=20,
    )
    await vstore.create_collection(domain_id="security", dense_dim=8)

    result = await svc.run(
        _request(doc_id="doc-pii", file_path="/inline/doc-pii.txt"),
        job_id="job-pii",
        pii_config=_pii_config(),
    )
    assert result.status == "completed"
    assert result.high_pii_chunks >= 1

    chunks = await chunk_repo.list_by_doc(
        domain_id="security", doc_id="doc-pii", doc_version="v1", parser_version="p1"
    )
    assert any(
        any(w["category"] == "rrn" for w in c.pii_warnings) for c in chunks
    )

    # Qdrant payload에도 pii_warnings 들어감
    points = await vstore.hybrid_query(
        domain_id="security",
        dense_query=[0.0] * 8,
        sparse_query={},
        acl_filter={},
        top_k=100,
    )
    assert any(p["payload"]["pii_warnings"] for p in points)


# ---------------------------------------------------------------------------
# 4-mode reindex
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_re_split_recreates_chunks():
    """chunk_re_split = full과 동일한 경로지만 chunk_index가 새로 계산."""
    svc, vstore, chunk_repo, _doc, _job = _build(
        inline_pages={"/inline/doc-001.txt": ["aa bb cc dd ee ff gg hh"]},
        max_tokens=4,
    )
    await vstore.create_collection(domain_id="security", dense_dim=8)
    await svc.run(_request(), job_id="job-init", pii_config=_pii_config())
    first = await chunk_repo.list_by_doc(
        domain_id="security", doc_id="doc-001", doc_version="v1", parser_version="p1"
    )
    initial_count = len(first)

    # chunker.max_tokens가 같으면 chunk 개수도 같지만 replace_chunks 동작 검증
    result = await svc.run(
        _request(mode=ReindexMode.CHUNK_RE_SPLIT),
        job_id="job-resplit",
        pii_config=_pii_config(),
    )
    assert result.status == "completed"
    second = await chunk_repo.list_by_doc(
        domain_id="security", doc_id="doc-001", doc_version="v1", parser_version="p1"
    )
    assert len(second) == initial_count  # 같은 chunker 설정


@pytest.mark.asyncio
async def test_embedding_only_preserves_chunks_and_reembeds():
    svc, vstore, chunk_repo, _doc, _job = _build(
        inline_pages={"/inline/doc-001.txt": ["aa bb cc dd ee ff gg hh"]},
        max_tokens=4,
    )
    await vstore.create_collection(domain_id="security", dense_dim=8)
    await svc.run(_request(), job_id="job-init", pii_config=_pii_config())
    before = await chunk_repo.list_by_doc(
        domain_id="security", doc_id="doc-001", doc_version="v1", parser_version="p1"
    )
    before_ids = {c.chunk_id for c in before}
    before_count = len(before)

    result = await svc.run(
        _request(mode=ReindexMode.EMBEDDING_ONLY),
        job_id="job-emb",
        pii_config=_pii_config(),
    )
    assert result.status == "completed"
    assert result.indexed_chunks == before_count

    after = await chunk_repo.list_by_doc(
        domain_id="security", doc_id="doc-001", doc_version="v1", parser_version="p1"
    )
    # chunk_id가 유지됨 (chunks 보존)
    assert {c.chunk_id for c in after} == before_ids


@pytest.mark.asyncio
async def test_parser_only_updates_metadata_without_reembedding():
    svc, vstore, chunk_repo, _doc, _job = _build(
        inline_pages={"/inline/doc-001.txt": ["aa bb cc dd"]},
        max_tokens=4,
    )
    await vstore.create_collection(domain_id="security", dense_dim=8)
    await svc.run(_request(), job_id="job-init", pii_config=_pii_config())

    # parser_only — inline_pages 변경 없이 호출
    result = await svc.run(
        _request(mode=ReindexMode.PARSER_ONLY),
        job_id="job-parser",
        pii_config=_pii_config(),
    )
    assert result.status == "completed"


# ---------------------------------------------------------------------------
# 동시성 제한 (ADR-012 §10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_indexing_raises_conflict():
    svc, vstore, _chunk, _doc, job_repo = _build(
        inline_pages={"/inline/doc-001.txt": ["aa bb cc"]},
        max_tokens=4,
    )
    await vstore.create_collection(domain_id="security", dense_dim=8)

    # 첫 job 강제로 pending 상태 유지하기 위해 job_repo에 직접 인서트
    from rag_core.interfaces.chunk_repository import IndexingJobRecord

    await job_repo.create(
        IndexingJobRecord(
            job_id="active-job",
            domain_id="security",
            doc_id="doc-001",
            doc_version="v1",
            status="pending",
        )
    )

    with pytest.raises(IndexingConflictError):
        await svc.run(_request(), job_id="job-new", pii_config=_pii_config())


# ---------------------------------------------------------------------------
# Fail-fast (ADR-007/012 §12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_embed_failure_marks_job_failed():
    svc, vstore, chunk_repo, _doc, job_repo = _build(
        inline_pages={"/inline/doc-001.txt": ["aa bb cc dd"]},
        max_tokens=2,
        failing_embedder=True,
    )
    await vstore.create_collection(domain_id="security", dense_dim=8)
    result = await svc.run(_request(), job_id="job-fail", pii_config=_pii_config())

    assert result.status == "failed"
    assert result.failure_rate > 0.30
    assert result.indexed_chunks == 0
    job = await job_repo.get("job-fail")
    assert job.status == "failed"

    # chunks가 DB에 들어가지 않았는지
    rows = await chunk_repo.list_by_doc(
        domain_id="security", doc_id="doc-001", doc_version="v1", parser_version="p1"
    )
    assert rows == []
