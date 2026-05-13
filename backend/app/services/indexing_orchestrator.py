"""IndexingOrchestrator — backend가 rag_core IndexingService를 호출할 때의 진입점.

흐름:
  1. API endpoint가 `prepare_upload` 또는 `prepare_reindex`를 호출 →
     storage.save (upload 한정) + job_repo.create 동기 처리. 충돌(IndexingConflictError)은
     이 시점에서 raise → API가 409 Conflict로 응답한다.
  2. 동일 endpoint가 BackgroundTasks로 `execute(job_id)`를 스케줄.
  3. execute는 미리 등록된 IndexingRequest를 꺼내 IndexingService.run_with_existing_job
     로 위임. 결과는 indexing_jobs 테이블에 기록되며, 호출자에게는 노출되지 않는다.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date
from typing import IO, Any

from rag_core.interfaces.chunk_repository import (
    IndexingConflictError,
    IndexingJobRecord,
)
from rag_core.services.indexing_service import (
    IndexingRequest,
    IndexingService,
    ReindexMode,
)

from app.services.document_storage import DocumentStorage, StoredDocument

logger = logging.getLogger(__name__)


@dataclass
class PreparedIndexing:
    """prepare_upload / prepare_reindex 결과 — caller가 BackgroundTask 스케줄에 사용."""

    job_id: str
    doc_id: str
    version: str
    stored: StoredDocument | None  # reindex(file 재업로드 없음)에서는 None


class IndexingOrchestrator:
    """upload + reindex 진입을 IndexingService에 위임하는 backend wrapper.

    Args:
        indexing_service: rag_core IndexingService 인스턴스
        storage: 업로드 원본 영속화 (LocalFilesystemStorage / MinIOStorage)
        config_loader: tenant_id → tenant_config dict (PII config 추출용)
    """

    def __init__(
        self,
        *,
        indexing_service: IndexingService,
        storage: DocumentStorage,
        config_loader,
    ) -> None:
        self._service = indexing_service
        self._storage = storage
        self._config_loader = config_loader
        # job_id → IndexingRequest. prepare_*에서 적재되고 execute에서 소비된다.
        self._pending: dict[str, IndexingRequest] = {}

    @property
    def service(self) -> IndexingService:
        return self._service

    async def prepare_upload(
        self,
        *,
        tenant_id: str,
        doc_id: str | None,
        version: str,
        title: str,
        filename: str,
        stream: IO[bytes],
        input_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PreparedIndexing:
        """파일 저장 + indexing_jobs row 동기 INSERT. 충돌이면 IndexingConflictError raise."""
        effective_doc_id = doc_id or f"DOC-{uuid.uuid4().hex[:12].upper()}"
        stored = await self._storage.save(
            tenant_id=tenant_id,
            doc_id=effective_doc_id,
            version=version,
            filename=filename,
            stream=stream,
        )

        job_id = await self._register_job(
            tenant_id=tenant_id,
            doc_id=effective_doc_id,
            version=version,
            filename=stored.local_path,
        )

        meta = metadata or {}
        # ADR-015 — input_type은 documents.input_type 컬럼에 별도 보존(metadata와 분리).
        # 도메인 필드(common이 아닌 input_type별 속성)는 metadata JSONB에 저장된다.
        domain_metadata = {
            k: v for k, v in meta.items()
            if k not in {
                "department", "doc_type", "security_level", "acl", "approval_status",
                "tags", "valid_from", "valid_until", "owner", "title", "input_type",
            }
        }
        request = IndexingRequest(
            tenant_id=tenant_id,
            doc_id=effective_doc_id,
            doc_version=version,
            file_path=stored.local_path,
            title=title,
            mode=ReindexMode.FULL,
            department=meta.get("department"),
            doc_type=meta.get("doc_type") or input_type,
            input_type=input_type,
            security_level=meta.get("security_level", "internal"),
            acl=list(meta.get("acl") or []),
            approval_status=meta.get("approval_status", "draft"),
            tags=list(meta.get("tags") or []),
            valid_from=_to_iso_date(meta.get("valid_from")),
            valid_until=_to_iso_date(meta.get("valid_until")),
            object_storage_path=stored.object_storage_path,
            owner=meta.get("owner"),
            metadata=domain_metadata,
        )
        self._pending[job_id] = request
        return PreparedIndexing(
            job_id=job_id, doc_id=effective_doc_id, version=version, stored=stored
        )

    async def prepare_reindex(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        version: str,
        mode: ReindexMode,
        metadata: dict[str, Any] | None = None,
    ) -> PreparedIndexing:
        """기존 document에 대한 재색인. doc_repo.get으로 source_path/title을 가져온다."""
        existing = await self._service.document_repo.get(
            tenant_id=tenant_id, doc_id=doc_id, version=version
        )
        if existing is None:
            raise FileNotFoundError(
                f"document not found: {tenant_id}/{doc_id}/{version}"
            )

        file_path = existing.source_path or ""
        job_id = await self._register_job(
            tenant_id=tenant_id,
            doc_id=doc_id,
            version=version,
            filename=file_path,
        )

        meta = metadata or {}
        request = IndexingRequest(
            tenant_id=tenant_id,
            doc_id=doc_id,
            doc_version=version,
            file_path=file_path,
            title=existing.title,
            mode=mode,
            department=meta.get("department", existing.department),
            doc_type=meta.get("doc_type", existing.doc_type),
            security_level=meta.get("security_level", existing.security_level),
            acl=list(meta.get("acl") or []),
            approval_status=meta.get("approval_status", existing.approval_status),
            tags=list(meta.get("tags") or list(existing.tags)),
            valid_from=_to_iso_date(meta.get("valid_from", existing.valid_from)),
            valid_until=_to_iso_date(meta.get("valid_until", existing.valid_until)),
            object_storage_path=existing.object_storage_path,
            owner=meta.get("owner", existing.owner),
        )
        self._pending[job_id] = request
        return PreparedIndexing(
            job_id=job_id, doc_id=doc_id, version=version, stored=None
        )

    async def retry_job(
        self, *, tenant_id: str, job_id: str
    ) -> "PreparedIndexing":
        """ADR-017 §7 — failed/partial job을 in-place 재실행.

        흐름:
          1. job_repo.get(job_id) — 존재 확인 + tenant 일치 확인
          2. status가 'failed' 또는 'partial'이 아니면 ValueError('invalid_status')
          3. document_repo.get(doc_id, doc_version) — 원본 메타 회수. 없으면 FileNotFoundError
          4. IndexingRequest 재구성 + job_repo.update(status='pending', error_message=None, progress=0, indexed_chunks=0)
          5. _pending[job_id] = request → caller가 background로 execute(job_id) 호출
        """
        job = await self._service.job_repo.get(job_id)
        if job is None or job.tenant_id != tenant_id:
            raise FileNotFoundError(f"job not found: {job_id}")
        if job.status not in {"failed", "partial"}:
            raise ValueError("invalid_status")
        existing = await self._service.document_repo.get(
            tenant_id=tenant_id, doc_id=job.doc_id, version=job.doc_version,
        )
        if existing is None:
            raise FileNotFoundError(
                f"document not found: {tenant_id}/{job.doc_id}/{job.doc_version}"
            )
        request = IndexingRequest(
            tenant_id=tenant_id,
            doc_id=job.doc_id,
            doc_version=job.doc_version,
            file_path=existing.source_path or "",
            title=existing.title,
            mode=ReindexMode.FULL,
            department=existing.department,
            doc_type=existing.doc_type,
            security_level=existing.security_level,
            acl=[],
            approval_status=existing.approval_status,
            tags=list(existing.tags),
            valid_from=_to_iso_date(existing.valid_from),
            valid_until=_to_iso_date(existing.valid_until),
            object_storage_path=existing.object_storage_path,
            owner=existing.owner,
        )
        self._pending[job_id] = request
        # reset job state — re-run 시 IndexingService.run_with_existing_job이 pending 가정
        await self._service.job_repo.update(
            job_id=job_id, status="pending", error_message=None,
            progress=0, step=None, indexed_chunks=0, failed_chunks=[],
        )
        return PreparedIndexing(
            job_id=job_id, doc_id=job.doc_id, version=job.doc_version, stored=None,
        )

    async def execute(self, *, job_id: str) -> None:
        """BackgroundTask 진입. _pending에서 request를 꺼내 IndexingService에 위임.

        예외는 IndexingService 내부에서 indexing_jobs.status='failed'로 기록되므로 별도
        처리 없이 swallow한다 (FastAPI BackgroundTasks가 raise해도 사용자 응답엔 영향 없음).
        """
        request = self._pending.pop(job_id, None)
        if request is None:
            logger.warning("execute called with unknown job_id=%s", job_id)
            return
        await self._ensure_collection_exists(request.tenant_id)
        pii_config = await self._load_pii_config(request.tenant_id)
        try:
            await self._service.run_with_existing_job(
                request, job_id=job_id, pii_config=pii_config
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "background indexing job failed: job_id=%s err=%s", job_id, exc
            )

    async def _ensure_collection_exists(self, tenant_id: str) -> None:
        """vector store에 collection이 없으면 lazy 생성.

        운영(Qdrant)에서는 tenant_register.sh가 collection을 만들어 두지만, 만일의
        누락에 대비해 idempotent하게 처리한다. InMemory backend는 본 호출이 매 tenant
        첫 업로드 시 컬렉션을 만든다.
        """
        try:
            await self._service.vector_store.create_collection(
                tenant_id=tenant_id,
                dense_dim=self._service.embedder.dense_dim,
            )
        except Exception:  # noqa: BLE001 — 이미 존재 또는 backend별 에러 모두 무해
            pass

    async def _register_job(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        version: str,
        filename: str,
    ) -> str:
        job_id = f"IDX-{uuid.uuid4().hex[:16].upper()}"
        try:
            await self._service.job_repo.create(
                IndexingJobRecord(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    doc_id=doc_id,
                    doc_version=version,
                    status="pending",
                    filename=filename,
                )
            )
        except IndexingConflictError:
            raise
        return job_id

    async def _load_pii_config(self, tenant_id: str) -> dict[str, Any] | None:
        cfg = self._config_loader(tenant_id)
        if hasattr(cfg, "__await__"):
            cfg = await cfg  # type: ignore[assignment]
        if not isinstance(cfg, dict):
            return None
        pii = cfg.get("pii")
        return pii if isinstance(pii, dict) else None


def _to_iso_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
