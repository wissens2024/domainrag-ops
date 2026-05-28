"""ChunkRepository / DocumentRepository / IndexingJobRepository — Protocols (ADR-012).

IndexingService가 Postgres write 책임을 위임하는 인터페이스. Backend의 SQLAlchemy
구현체와 InMemory 구현체가 같은 Protocol을 충족.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class DocumentRecord:
    """documents 테이블에 INSERT 또는 UPSERT할 메타."""

    domain_id: str
    doc_id: str
    title: str
    version: str = "v1"
    input_type: str | None = None
    source_type: str | None = None
    source_path: str | None = None
    object_storage_path: str | None = None
    department: str | None = None
    doc_type: str | None = None
    security_level: str = "internal"
    owner: str | None = None
    tags: list[str] = field(default_factory=list)
    language: str = "ko"
    valid_from: str | None = None  # ISO date
    valid_until: str | None = None
    approval_status: str = "draft"
    file_hash: str | None = None
    parser_version: str = "p1"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkRecord:
    """chunks 테이블에 INSERT할 row — Chunker.Chunk + lifecycle/PII 메타 결합."""

    domain_id: str
    chunk_id: str
    doc_id: str
    doc_version: str
    parser_version: str
    chunk_strategy: str
    chunk_index: int
    content: str
    title: str | None
    page_number: int | None
    section_title: str | None
    heading_path: list[str]
    content_hash: str | None
    start_char: int | None
    end_char: int | None
    token_count: int | None
    department: str | None
    doc_type: str | None
    security_level: str | None
    acl: list[str] = field(default_factory=list)
    approval_status: str = "draft"
    tags: list[str] = field(default_factory=list)
    valid_from: str | None = None
    valid_until: str | None = None
    embedding_model: str | None = None
    embedding_version: str | None = None
    vector_id: str | None = None
    pii_warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class IndexingJobRecord:
    job_id: str
    domain_id: str
    doc_id: str
    doc_version: str
    status: str = "pending"
    step: str | None = None
    progress: int = 0
    total_chunks: int = 0
    indexed_chunks: int = 0
    failed_chunks: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None
    failure_rate: float | None = None
    retry_count: int = 0
    filename: str | None = None


class DocumentRepository(Protocol):
    async def upsert(self, doc: DocumentRecord) -> None: ...

    async def get(self, *, domain_id: str, doc_id: str, version: str) -> DocumentRecord | None: ...

    async def list_by_tenant(
        self,
        *,
        domain_id: str,
        keyword: str | None = None,
        approval_status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        """ADR-017 §6.2 — admin documents 목록 페이징.

        keyword: title 또는 doc_id 부분 일치 (대소문자 무시).
        approval_status: 'draft'/'approved'/'archived' 등 정확 일치 필터.
        가장 최신 version만 반환 (운영 의도: list에는 active version만 노출).
        """
        ...

    async def update_approval(
        self,
        *,
        domain_id: str,
        doc_id: str,
        version: str,
        approval_status: str,
    ) -> DocumentRecord | None:
        """ADR-012 §3-8 — approval_status payload-only update.

        documents row + 같은 (domain_id, doc_id, version)에 속한 chunks row의
        approval_status를 함께 update. None이면 doc 없음(404).
        """
        ...

    async def delete(
        self,
        *,
        domain_id: str,
        doc_id: str,
        version: str | None = None,
    ) -> int:
        """ADR-007/012 hard delete — documents row 삭제. version None이면 모든 version.

        Returns: 삭제된 row 수.
        """
        ...

    async def update_partial(
        self,
        *,
        domain_id: str,
        doc_id: str,
        version: str,
        patch: dict[str, Any],
    ) -> DocumentRecord | None:
        """ADR-017 §6.4 — documents 필드 부분 갱신. payload-only (chunks 재생성 없음).

        허용 컬럼: title / input_type / source_type / object_storage_path / department /
        doc_type / security_level / owner / tags / language / valid_from / valid_until /
        metadata. None이면 doc 없음 (404).

        approval_status는 본 메서드로 갱신하지 않는다 (별도 `update_approval` 사용 — ADR-012 §3-8).
        """
        ...


class ChunkRepository(Protocol):
    async def replace_chunks(
        self,
        *,
        domain_id: str,
        doc_id: str,
        doc_version: str,
        parser_version: str,
        chunks: list[ChunkRecord],
    ) -> None:
        """기존 (domain_id, doc_id, doc_version, parser_version) 조합 chunks 제거 후 INSERT.

        full / chunk_re_split / parser_only 모드에서 chunks 갱신.
        """
        ...

    async def update_metadata(
        self,
        *,
        domain_id: str,
        chunk_ids: list[str],
        metadata: dict[str, Any],
    ) -> None:
        """metadata-only 갱신 (ADR-012 §12). embedding_only 모드에서 vector_id/embedding_model만 갱신."""
        ...

    async def list_by_doc(
        self,
        *,
        domain_id: str,
        doc_id: str,
        doc_version: str,
        parser_version: str | None = None,
    ) -> list[ChunkRecord]: ...

    async def delete_by_doc(
        self,
        *,
        domain_id: str,
        doc_id: str,
        doc_version: str | None = None,
    ) -> list[str]:
        """ADR-007/012 hard delete — 해당 doc의 chunks(+chunks_archive) rows 삭제.

        Args:
            doc_version: None이면 모든 version 삭제.
        Returns:
            삭제된 chunk_id 목록 — caller가 vector_store.delete_points에 그대로 전달.
        """
        ...


class IndexingJobRepository(Protocol):
    async def create(self, job: IndexingJobRecord) -> None:
        """unique constraint 위반 시 ConflictError raise (동시 indexing 방지 — ADR-012 §10)."""
        ...

    async def update(
        self,
        *,
        job_id: str,
        status: str | None = None,
        step: str | None = None,
        progress: int | None = None,
        total_chunks: int | None = None,
        indexed_chunks: int | None = None,
        failed_chunks: list[dict[str, Any]] | None = None,
        error_message: str | None = None,
        failure_rate: float | None = None,
    ) -> None: ...

    async def get(self, job_id: str) -> IndexingJobRecord | None: ...

    async def list_by_tenant(
        self,
        *,
        domain_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IndexingJobRecord]:
        """ADR-017 §7 — Tenant Admin 콘솔 인덱싱 목록 조회.

        Protocol에 노출되는 read-only 페이징 API. status 필터 None이면 전 상태 반환.
        InMemory 구현체는 내부 dict를 직렬화해 반환하면 충분.
        """
        ...


class IndexingConflictError(Exception):
    """ADR-012 §10 — 같은 (domain_id, doc_id, doc_version) 활성 job 존재."""

    def __init__(self, existing_job_id: str):
        super().__init__(f"active indexing job already exists: {existing_job_id}")
        self.existing_job_id = existing_job_id
