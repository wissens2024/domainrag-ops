"""IndexingService — 문서 색인 오케스트레이션 (ADR-007/012 lifecycle + ADR-020 §5 Layer 3).

책임:
  1. parse → chunk → PII Layer 3 → embed → upsert (Qdrant) → DB write
  2. 4-mode reindex 트리거 (parser_only / chunk_re_split / embedding_only / full)
  3. 30% chunk fail-fast (ADR-007/012 부분 실패 정책)
  4. indexing_jobs 상태 갱신 (pending → parsing → chunking → embedding → indexing → completed/failed)

비책임 (caller / 별도 모듈):
  - 동시성 제한 (worker_pool — backend 책임)
  - MinIO 업로드 (별도 storage helper)
  - tenant_id RLS context (caller의 DB session에서 set)
  - hard delete (별도 hard_delete_workflow)
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rag_core.interfaces.chunk_repository import (
    ChunkRecord,
    ChunkRepository,
    DocumentRecord,
    DocumentRepository,
    IndexingConflictError,
    IndexingJobRecord,
    IndexingJobRepository,
)
from rag_core.interfaces.chunker import Chunk, Chunker
from rag_core.interfaces.embedder import Embedder
from rag_core.interfaces.parser import DocumentParser, ParsedPage
from rag_core.interfaces.vector_store import VectorStore
from rag_core.services.pii_service import ChunkPIIWarning, PIIService

logger = logging.getLogger(__name__)


class ReindexMode(str, Enum):
    """ADR-012 §11 — 재색인 4모드."""

    PARSER_ONLY = "parser_only"
    CHUNK_RE_SPLIT = "chunk_re_split"
    EMBEDDING_ONLY = "embedding_only"
    FULL = "full"


@dataclass
class IndexingRequest:
    """upload 또는 reindex 진입점에 전달되는 요청 객체."""

    tenant_id: str
    doc_id: str
    doc_version: str
    file_path: str  # 로컬 경로 또는 InMemoryParser.inline 키
    title: str
    mode: ReindexMode = ReindexMode.FULL

    # 메타
    department: str | None = None
    doc_type: str | None = None
    input_type: str | None = None
    security_level: str = "internal"
    acl: list[str] = field(default_factory=list)
    approval_status: str = "draft"
    tags: list[str] = field(default_factory=list)
    valid_from: str | None = None
    valid_until: str | None = None
    object_storage_path: str | None = None
    owner: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IndexingResult:
    """IndexingService.run() 결과."""

    job_id: str
    status: str  # 'completed' | 'failed' | 'partial'
    total_chunks: int
    indexed_chunks: int
    failed_chunks: list[dict[str, Any]]
    high_pii_chunks: int  # ADR-020 §5 high severity 발견 chunk 수
    failure_rate: float


class IndexingService:
    """4-mode reindex orchestrator.

    Configurable failure threshold (default 0.3 = ADR-007/012 30% fail-fast).
    """

    def __init__(
        self,
        *,
        parser: DocumentParser,
        chunker: Chunker,
        embedder: Embedder,
        vector_store: VectorStore,
        pii_service: PIIService,
        chunk_repo: ChunkRepository,
        document_repo: DocumentRepository,
        job_repo: IndexingJobRepository,
        failure_threshold: float = 0.30,
    ) -> None:
        self.parser = parser
        self.chunker = chunker
        self.embedder = embedder
        self.vector_store = vector_store
        self.pii_service = pii_service
        self.chunk_repo = chunk_repo
        self.document_repo = document_repo
        self.job_repo = job_repo
        self.failure_threshold = failure_threshold

    # ------------------------------------------------------------------ #
    # entry point
    # ------------------------------------------------------------------ #

    async def run(
        self,
        request: IndexingRequest,
        *,
        job_id: str,
        pii_config: dict[str, Any] | None = None,
    ) -> IndexingResult:
        """단일 indexing job 실행 — job_repo.create + dispatch."""
        job = IndexingJobRecord(
            job_id=job_id,
            tenant_id=request.tenant_id,
            doc_id=request.doc_id,
            doc_version=request.doc_version,
            status="pending",
            filename=request.file_path,
        )
        try:
            await self.job_repo.create(job)
        except IndexingConflictError:
            raise

        return await self.run_with_existing_job(
            request, job_id=job_id, pii_config=pii_config
        )

    async def run_with_existing_job(
        self,
        request: IndexingRequest,
        *,
        job_id: str,
        pii_config: dict[str, Any] | None = None,
    ) -> IndexingResult:
        """job_repo.create를 호출자(예: backend orchestrator)가 이미 수행한 경우의 진입점.

        API 요청-응답 안에서 충돌(IndexingConflictError)을 동기적으로 잡아야 하는 경우
        호출자가 job_repo.create를 먼저 실행하고 이 메서드로 background 실행을 위임한다.
        실패 시 indexing_jobs.status='failed'로 갱신하고 raise 한다.
        """
        try:
            if request.mode == ReindexMode.EMBEDDING_ONLY:
                return await self._reindex_embedding_only(
                    request, job_id=job_id, pii_config=pii_config
                )
            if request.mode == ReindexMode.PARSER_ONLY:
                return await self._reindex_parser_only(
                    request, job_id=job_id, pii_config=pii_config
                )
            # full / chunk_re_split — parse + chunk + embed + upsert
            return await self._index_full(
                request, job_id=job_id, pii_config=pii_config
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("indexing failed", extra={"job_id": job_id})
            await self.job_repo.update(
                job_id=job_id, status="failed", error_message=str(exc)
            )
            raise

    # ------------------------------------------------------------------ #
    # core: full / chunk_re_split
    # ------------------------------------------------------------------ #

    async def _index_full(
        self,
        request: IndexingRequest,
        *,
        job_id: str,
        pii_config: dict[str, Any] | None,
    ) -> IndexingResult:
        await self.job_repo.update(job_id=job_id, status="parsing", step="parse", progress=10)
        pages = await self.parser.parse(request.file_path)

        await self.job_repo.update(job_id=job_id, status="chunking", step="chunk", progress=30)
        chunks = await self.chunker.chunk(
            doc_id=request.doc_id,
            doc_version=request.doc_version,
            parser_version=self.parser.parser_version,
            pages=pages,
        )

        # PII Layer 3 — chunk content scan
        warnings_by_chunk = self._scan_pii(chunks, pii_config)

        # Embedding
        await self.job_repo.update(
            job_id=job_id, status="embedding", step="embed", progress=60, total_chunks=len(chunks)
        )
        embed_result = await self._embed_safely(chunks)

        # Upsert + DB write
        await self.job_repo.update(job_id=job_id, status="indexing", step="upsert", progress=80)
        records = self._build_records(
            request, chunks, embed_result.vectors, warnings_by_chunk
        )

        # 30% fail-fast
        failure_rate = (
            len(embed_result.failed) / max(1, len(chunks)) if chunks else 0.0
        )
        if failure_rate > self.failure_threshold:
            await self.job_repo.update(
                job_id=job_id,
                status="failed",
                failed_chunks=embed_result.failed,
                failure_rate=failure_rate,
                error_message=f"failure_rate {failure_rate:.2f} > threshold {self.failure_threshold}",
            )
            return IndexingResult(
                job_id=job_id,
                status="failed",
                total_chunks=len(chunks),
                indexed_chunks=0,
                failed_chunks=embed_result.failed,
                high_pii_chunks=sum(
                    1 for w in warnings_by_chunk.values() if w.has_high_severity
                ),
                failure_rate=failure_rate,
            )

        await self._upsert_to_qdrant(request, chunks, embed_result.vectors, warnings_by_chunk)
        await self._write_chunks_and_document(request, records)

        indexed_count = len(chunks) - len(embed_result.failed)
        status = "partial" if embed_result.failed else "completed"
        await self.job_repo.update(
            job_id=job_id,
            status=status,
            step="done",
            progress=100,
            indexed_chunks=indexed_count,
            failed_chunks=embed_result.failed,
            failure_rate=failure_rate,
        )

        return IndexingResult(
            job_id=job_id,
            status=status,
            total_chunks=len(chunks),
            indexed_chunks=indexed_count,
            failed_chunks=embed_result.failed,
            high_pii_chunks=sum(
                1 for w in warnings_by_chunk.values() if w.has_high_severity
            ),
            failure_rate=failure_rate,
        )

    # ------------------------------------------------------------------ #
    # parser_only — re-parse, metadata UPDATE only (chunks 보존)
    # ------------------------------------------------------------------ #

    async def _reindex_parser_only(
        self,
        request: IndexingRequest,
        *,
        job_id: str,
        pii_config: dict[str, Any] | None,
    ) -> IndexingResult:
        await self.job_repo.update(job_id=job_id, status="parsing", step="parse", progress=20)
        pages = await self.parser.parse(request.file_path)

        # 기존 chunks 가져와서 page_number/section_title만 업데이트.
        # InMemory 구현에서는 update_metadata로 처리.
        existing = await self.chunk_repo.list_by_doc(
            tenant_id=request.tenant_id,
            doc_id=request.doc_id,
            doc_version=request.doc_version,
            parser_version=self.parser.parser_version,
        )
        if not existing:
            # 없으면 full로 처리
            return await self._index_full(request, job_id=job_id, pii_config=pii_config)

        # page index → page meta 매핑 (chunk가 어느 page에 속하는지 chunk_index로는 알 수 없음.
        # InMemory 단순 구현은 1:1 — 실제 운영은 parser가 chunk별 page를 별도로 산출)
        page_map = {p.page_number: p for p in pages}
        await self.job_repo.update(job_id=job_id, status="indexing", step="metadata_update", progress=70)
        # chunk별로 DB chunks UPDATE 한 뒤, 동일 메타를 Qdrant payload에도 동기화 — ADR-012 §3-8 payload_only_sync 정합.
        for c in existing:
            page = page_map.get(c.page_number or 1)
            if page is None:
                continue
            meta = {
                "section_title": page.section_title,
                "heading_path": list(page.heading_path or []),
            }
            await self.chunk_repo.update_metadata(
                tenant_id=request.tenant_id,
                chunk_ids=[c.chunk_id],
                metadata=meta,
            )
            try:
                await self.vector_store.set_payload(
                    tenant_id=request.tenant_id,
                    chunk_ids=[c.chunk_id],
                    payload=meta,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "vector_store.set_payload failed during parser_only reindex: "
                    "chunk=%s err=%s",
                    c.chunk_id,
                    exc,
                )

        await self.job_repo.update(
            job_id=job_id,
            status="completed",
            step="done",
            progress=100,
            total_chunks=len(existing),
            indexed_chunks=len(existing),
            failure_rate=0.0,
        )
        return IndexingResult(
            job_id=job_id,
            status="completed",
            total_chunks=len(existing),
            indexed_chunks=len(existing),
            failed_chunks=[],
            high_pii_chunks=0,
            failure_rate=0.0,
        )

    # ------------------------------------------------------------------ #
    # embedding_only — chunks 보존, vectors만 재계산 + Qdrant upsert
    # ------------------------------------------------------------------ #

    async def _reindex_embedding_only(
        self,
        request: IndexingRequest,
        *,
        job_id: str,
        pii_config: dict[str, Any] | None,
    ) -> IndexingResult:
        existing = await self.chunk_repo.list_by_doc(
            tenant_id=request.tenant_id,
            doc_id=request.doc_id,
            doc_version=request.doc_version,
        )
        if not existing:
            return await self._index_full(request, job_id=job_id, pii_config=pii_config)

        # ChunkRecord → Chunk 임시 변환 (embed 호출용)
        chunks_for_embed = [
            Chunk(
                chunk_id=r.chunk_id,
                chunk_index=r.chunk_index,
                content=r.content,
                page_number=r.page_number,
                section_title=r.section_title,
                heading_path=list(r.heading_path),
                start_char=r.start_char or 0,
                end_char=r.end_char or 0,
                token_count=r.token_count or 0,
            )
            for r in existing
        ]
        # PII 정보는 기존 record에서 그대로 가져온다 (재스캔하지 않음 — 모드 정의)
        warnings_by_chunk = {
            r.chunk_id: ChunkPIIWarning(
                pii_warnings=list(r.pii_warnings),
                has_high_severity=any(
                    w.get("severity") == "high" for w in r.pii_warnings
                ),
            )
            for r in existing
        }

        await self.job_repo.update(
            job_id=job_id, status="embedding", step="embed", progress=40, total_chunks=len(existing)
        )
        embed_result = await self._embed_safely(chunks_for_embed)

        failure_rate = len(embed_result.failed) / max(1, len(existing))
        if failure_rate > self.failure_threshold:
            await self.job_repo.update(
                job_id=job_id,
                status="failed",
                failed_chunks=embed_result.failed,
                failure_rate=failure_rate,
                error_message=f"failure_rate {failure_rate:.2f} > threshold",
            )
            return IndexingResult(
                job_id=job_id,
                status="failed",
                total_chunks=len(existing),
                indexed_chunks=0,
                failed_chunks=embed_result.failed,
                high_pii_chunks=sum(
                    1 for w in warnings_by_chunk.values() if w.has_high_severity
                ),
                failure_rate=failure_rate,
            )

        await self._upsert_to_qdrant(request, chunks_for_embed, embed_result.vectors, warnings_by_chunk)

        # chunks.embedding_model / embedding_version / vector_id 갱신
        await self.chunk_repo.update_metadata(
            tenant_id=request.tenant_id,
            chunk_ids=[c.chunk_id for c in chunks_for_embed if c.chunk_id not in {f["chunk_id"] for f in embed_result.failed}],
            metadata={
                "embedding_model": self.embedder.model_name,
                "embedding_version": self.embedder.model_name,
            },
        )

        indexed = len(existing) - len(embed_result.failed)
        status = "partial" if embed_result.failed else "completed"
        await self.job_repo.update(
            job_id=job_id,
            status=status,
            step="done",
            progress=100,
            indexed_chunks=indexed,
            failed_chunks=embed_result.failed,
            failure_rate=failure_rate,
        )
        return IndexingResult(
            job_id=job_id,
            status=status,
            total_chunks=len(existing),
            indexed_chunks=indexed,
            failed_chunks=embed_result.failed,
            high_pii_chunks=sum(
                1 for w in warnings_by_chunk.values() if w.has_high_severity
            ),
            failure_rate=failure_rate,
        )

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _scan_pii(
        self,
        chunks: list[Chunk],
        pii_config: dict[str, Any] | None,
    ) -> dict[str, ChunkPIIWarning]:
        warnings: dict[str, ChunkPIIWarning] = {}
        for c in chunks:
            warnings[c.chunk_id] = self.pii_service.check_chunk_pii(c.content, pii_config)
        return warnings

    @dataclass
    class _EmbedResult:
        vectors: dict[str, tuple[list[float], dict[int, float]]]
        failed: list[dict[str, Any]]

    async def _embed_safely(self, chunks: list[Chunk]) -> "_EmbedResult":
        """batch embed — 실패 시 chunk_id별로 격리해 failed list 누적.

        bge-m3는 단일 호출로 dense+sparse를 동시에 산출 (interfaces/embedder.py).
        실패 시 batch 전체가 throw — chunk별 retry는 caller가 결정.
        """
        vectors: dict[str, tuple[list[float], dict[int, float]]] = {}
        failed: list[dict[str, Any]] = []
        if not chunks:
            return self._EmbedResult(vectors=vectors, failed=failed)

        try:
            embed_pairs = await self.embedder.embed_batch([c.content for c in chunks])
            for chunk, pair in zip(chunks, embed_pairs):
                vectors[chunk.chunk_id] = pair
        except Exception as exc:  # noqa: BLE001
            logger.warning("batch embed failed; recording all chunks as failed: %s", exc)
            failed.extend(
                {"chunk_id": c.chunk_id, "step": "embed", "error": str(exc)} for c in chunks
            )
        return self._EmbedResult(vectors=vectors, failed=failed)

    def _build_records(
        self,
        request: IndexingRequest,
        chunks: list[Chunk],
        vectors: dict[str, tuple[list[float], dict[int, float]]],
        warnings_by_chunk: dict[str, ChunkPIIWarning],
    ) -> list[ChunkRecord]:
        records: list[ChunkRecord] = []
        for c in chunks:
            if c.chunk_id not in vectors:
                continue  # embed 실패 — chunk DB row도 만들지 않음 (재indexing 시 다시 시도)
            warning = warnings_by_chunk.get(c.chunk_id, ChunkPIIWarning())
            records.append(
                ChunkRecord(
                    tenant_id=request.tenant_id,
                    chunk_id=c.chunk_id,
                    doc_id=request.doc_id,
                    doc_version=request.doc_version,
                    parser_version=self.parser.parser_version,
                    chunk_strategy=self.chunker.chunk_strategy,
                    chunk_index=c.chunk_index,
                    content=c.content,
                    title=request.title,
                    page_number=c.page_number,
                    section_title=c.section_title,
                    heading_path=list(c.heading_path),
                    content_hash=hashlib.sha256(c.content.encode("utf-8")).hexdigest(),
                    start_char=c.start_char,
                    end_char=c.end_char,
                    token_count=c.token_count,
                    department=request.department,
                    doc_type=request.doc_type,
                    security_level=request.security_level,
                    acl=list(request.acl),
                    approval_status=request.approval_status,
                    tags=list(request.tags),
                    valid_from=request.valid_from,
                    valid_until=request.valid_until,
                    embedding_model=self.embedder.model_name,
                    embedding_version=self.embedder.model_name,
                    vector_id=c.chunk_id,
                    pii_warnings=list(warning.pii_warnings),
                )
            )
        return records

    async def _upsert_to_qdrant(
        self,
        request: IndexingRequest,
        chunks: list[Chunk],
        vectors: dict[str, tuple[list[float], dict[int, float]]],
        warnings_by_chunk: dict[str, ChunkPIIWarning],
    ) -> None:
        points: list[dict[str, Any]] = []
        for c in chunks:
            if c.chunk_id not in vectors:
                continue
            dense, sparse = vectors[c.chunk_id]
            warning = warnings_by_chunk.get(c.chunk_id, ChunkPIIWarning())
            payload: dict[str, Any] = {
                "tenant_id": request.tenant_id,
                "doc_id": request.doc_id,
                "doc_version": request.doc_version,
                "parser_version": self.parser.parser_version,
                "chunk_strategy": self.chunker.chunk_strategy,
                "chunk_index": c.chunk_index,
                "content": c.content,
                "title": request.title,
                "page_number": c.page_number,
                "section_title": c.section_title,
                "heading_path": list(c.heading_path),
                "department": request.department,
                "doc_type": request.doc_type,
                "security_level": request.security_level,
                "acl": list(request.acl),
                "approval_status": request.approval_status,
                "tags": list(request.tags),
                "valid_from": request.valid_from,
                "valid_until": request.valid_until,
                "pii_warnings": list(warning.pii_warnings),
            }
            points.append(
                {
                    "id": c.chunk_id,
                    "dense_vector": dense,
                    "sparse_vector": sparse,
                    "payload": payload,
                }
            )
        if points:
            await self.vector_store.upsert_chunks(
                tenant_id=request.tenant_id, points=points
            )

    async def _write_chunks_and_document(
        self,
        request: IndexingRequest,
        records: list[ChunkRecord],
    ) -> None:
        await self.document_repo.upsert(
            DocumentRecord(
                tenant_id=request.tenant_id,
                doc_id=request.doc_id,
                title=request.title,
                version=request.doc_version,
                input_type=request.input_type,
                source_path=request.file_path,
                object_storage_path=request.object_storage_path,
                department=request.department,
                doc_type=request.doc_type,
                security_level=request.security_level,
                owner=request.owner,
                tags=list(request.tags),
                valid_from=request.valid_from,
                valid_until=request.valid_until,
                approval_status=request.approval_status,
                parser_version=self.parser.parser_version,
                metadata=dict(request.metadata or {}),
            )
        )
        await self.chunk_repo.replace_chunks(
            tenant_id=request.tenant_id,
            doc_id=request.doc_id,
            doc_version=request.doc_version,
            parser_version=self.parser.parser_version,
            chunks=records,
        )
