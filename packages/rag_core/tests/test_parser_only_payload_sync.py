"""ADR-012 §3-8 — parser_only reindex가 Qdrant payload도 동기화하는지 검증.

IndexingService._reindex_parser_only 가 chunk_repo.update_metadata 외에 vector_store.
set_payload({"section_title", "heading_path"})도 호출해야 한다.
"""

from __future__ import annotations

from datetime import date

from rag_core.clients.in_memory import (
    InMemoryChunker,
    InMemoryChunkRepository,
    InMemoryDocumentRepository,
    InMemoryEmbedder,
    InMemoryIndexingJobRepository,
    InMemoryParser,
    InMemoryVectorStore,
)
from rag_core.pii import RegexPIIDetector
from rag_core.services.indexing_service import (
    IndexingRequest,
    IndexingService,
    ReindexMode,
)
from rag_core.services.pii_service import PIIService

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RULES_DIR = REPO_ROOT / "packages" / "rag_core" / "rag_core" / "pii" / "rules"


def _build_service(*, parser_v1: InMemoryParser, parser_v2: InMemoryParser) -> tuple[
    IndexingService, InMemoryVectorStore, InMemoryChunkRepository
]:
    """첫 색인은 parser_v1, parser_only reindex는 parser_v2(같은 parser_version 유지)로
    section_title을 새로 산출하도록 두 InMemoryParser 인스턴스를 분리한다."""
    embedder = InMemoryEmbedder(dense_dim=64)
    vector_store = InMemoryVectorStore()
    chunk_repo = InMemoryChunkRepository()
    doc_repo = InMemoryDocumentRepository()
    job_repo = InMemoryIndexingJobRepository()
    pii_service = PIIService(detector=RegexPIIDetector(rules_dir=RULES_DIR))
    chunker = InMemoryChunker(max_tokens=10, overlap_tokens=0)
    svc = IndexingService(
        parser=parser_v1,
        chunker=chunker,
        embedder=embedder,
        vector_store=vector_store,
        pii_service=pii_service,
        chunk_repo=chunk_repo,
        document_repo=doc_repo,
        job_repo=job_repo,
    )
    return svc, vector_store, chunk_repo


async def test_parser_only_reindex_calls_vector_store_set_payload():
    """첫 색인 후 parser_only로 section_title이 바뀌면 Qdrant payload에도 반영."""
    # 첫 색인 — page 1에 section_title "Old"
    parser_v1 = InMemoryParser(
        parser_version="p1",
        inline_pages={"doc.txt": ["The quick brown fox jumps over the lazy dog"]},
    )
    parser_v2 = InMemoryParser(parser_version="p1", inline_pages={
        "doc.txt": ["The quick brown fox jumps over the lazy dog"]
    })
    svc, vector_store, chunk_repo = _build_service(
        parser_v1=parser_v1, parser_v2=parser_v2
    )
    await vector_store.create_collection(domain_id="t1", dense_dim=64)

    request = IndexingRequest(
        domain_id="t1",
        doc_id="doc-1",
        doc_version="v1",
        file_path="doc.txt",
        title="Test",
        mode=ReindexMode.FULL,
        approval_status="approved",
        acl=["group:any"],
    )
    await svc.run(request, job_id="job-1")

    # parser_only 진입 전: payload에 section_title 비어있음
    pre = vector_store._collections["t1"].points  # type: ignore[attr-defined]
    pre_section_titles = {pid: p.payload.get("section_title") for pid, p in pre.items()}
    assert all(v is None for v in pre_section_titles.values())

    # parser v2 — 같은 page 텍스트로 새 section_title 산출 (테스트용 monkeypatch)
    pages = await parser_v2.parse("doc.txt")
    for p in pages:
        p.section_title = "Updated section"
        p.heading_path = ["chapter 1", "section 2"]

    async def _patched_parse(_path):
        return pages

    parser_v2.parse = _patched_parse  # type: ignore[method-assign]
    svc.parser = parser_v2

    reindex_req = IndexingRequest(
        domain_id="t1",
        doc_id="doc-1",
        doc_version="v1",
        file_path="doc.txt",
        title="Test",
        mode=ReindexMode.PARSER_ONLY,
        approval_status="approved",
        acl=["group:any"],
    )
    await svc.run(reindex_req, job_id="job-2")

    # 검증: vector store payload에 section_title/heading_path가 반영됨
    points = vector_store._collections["t1"].points  # type: ignore[attr-defined]
    section_titles = {pid: p.payload.get("section_title") for pid, p in points.items()}
    assert all(
        v == "Updated section" for v in section_titles.values()
    ), f"set_payload 미반영: {section_titles}"
    headings = {pid: p.payload.get("heading_path") for pid, p in points.items()}
    assert all(
        v == ["chapter 1", "section 2"] for v in headings.values()
    ), f"heading_path 미반영: {headings}"
