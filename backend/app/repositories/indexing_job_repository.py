"""PostgresIndexingJobRepository — indexing_jobs SQLAlchemy 구현 (ADR-007/012/019).

ADR-012 §10 — 같은 (domain_id, doc_id, doc_version)에서 active 상태(pending/parsing/
chunking/embedding/indexing) job은 1건만 허용. Migration 002의 부분 유니크 인덱스가
DB 차원의 race 방지를 담당하며, 본 모듈은 INSERT 실패 시 IndexingConflictError로
변환해 상위로 올린다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.chunk_repository import (
    IndexingConflictError,
    IndexingJobRecord,
)

from app.core.rls import set_tenant_context


_ACTIVE_STATES = ("pending", "parsing", "chunking", "embedding", "indexing")
_TERMINAL_STATES = {"completed", "partial", "failed"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class PostgresIndexingJobRepository:
    """indexing_jobs SQLAlchemy 구현체.

    Args:
        session_factory: app role async_sessionmaker (RLS 적용)
    """

    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def create(self, job: IndexingJobRecord) -> None:
        async with self._session_factory() as session:
            await set_tenant_context(session, job.domain_id)
            try:
                await session.execute(
                    text(
                        """
                        INSERT INTO indexing_jobs (
                            domain_id, job_id, doc_id, doc_version, filename,
                            status, progress, step, total_chunks, indexed_chunks,
                            failed_chunks, retry_count, started_at
                        )
                        VALUES (
                            :domain_id, :job_id, :doc_id, :doc_version, :filename,
                            :status, :progress, :step, :total_chunks, :indexed_chunks,
                            CAST(:failed_chunks AS JSONB), :retry_count, NOW()
                        )
                        """
                    ),
                    {
                        "domain_id": job.domain_id,
                        "job_id": job.job_id,
                        "doc_id": job.doc_id,
                        "doc_version": job.doc_version,
                        "filename": job.filename,
                        "status": job.status,
                        "progress": job.progress,
                        "step": job.step,
                        "total_chunks": job.total_chunks,
                        "indexed_chunks": job.indexed_chunks,
                        "failed_chunks": _json(job.failed_chunks),
                        "retry_count": job.retry_count,
                    },
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                # 부분 유니크 인덱스 충돌이면 ConflictError로 변환
                existing_id = await self._find_active_job_id(
                    domain_id=job.domain_id,
                    doc_id=job.doc_id,
                    doc_version=job.doc_version,
                )
                if existing_id:
                    raise IndexingConflictError(existing_id) from exc
                raise

    async def _find_active_job_id(
        self, *, domain_id: str, doc_id: str, doc_version: str
    ) -> str | None:
        async with self._session_factory() as session:
            await set_tenant_context(session, domain_id)
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT job_id FROM indexing_jobs
                         WHERE domain_id = :domain_id
                           AND doc_id = :doc_id
                           AND doc_version = :doc_version
                           AND status IN {_ACTIVE_STATES!r}
                         ORDER BY created_at DESC LIMIT 1
                        """
                    ),
                    {
                        "domain_id": domain_id,
                        "doc_id": doc_id,
                        "doc_version": doc_version,
                    },
                )
            ).first()
            return str(row[0]) if row else None

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
    ) -> None:
        set_clauses: list[str] = []
        params: dict[str, Any] = {"job_id": job_id}
        if status is not None:
            set_clauses.append("status = :status")
            params["status"] = status
            if status in _TERMINAL_STATES:
                set_clauses.append("finished_at = :finished_at")
                params["finished_at"] = datetime.now(timezone.utc)
        if step is not None:
            set_clauses.append("step = :step")
            params["step"] = step
        if progress is not None:
            set_clauses.append("progress = :progress")
            params["progress"] = progress
        if total_chunks is not None:
            set_clauses.append("total_chunks = :total_chunks")
            params["total_chunks"] = total_chunks
        if indexed_chunks is not None:
            set_clauses.append("indexed_chunks = :indexed_chunks")
            params["indexed_chunks"] = indexed_chunks
        if failed_chunks is not None:
            set_clauses.append("failed_chunks = CAST(:failed_chunks AS JSONB)")
            params["failed_chunks"] = _json(failed_chunks)
        if error_message is not None:
            set_clauses.append("error_message = :error_message")
            params["error_message"] = error_message
        if failure_rate is not None:
            set_clauses.append("failure_rate = :failure_rate")
            params["failure_rate"] = failure_rate

        if not set_clauses:
            return

        # update_metadata는 domain_id를 받지 않으므로 admin engine으로 update.
        # indexing_jobs의 RLS 정책 (domain_id = current_setting) 때문에 admin role 또는
        # job_id로 먼저 domain_id 조회 후 set_tenant_context. 후자가 RLS 무력화 없이 안전.
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT domain_id FROM indexing_jobs WHERE job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
            ).first()
            if row is None:
                # tenant context 없이도 RLS 정책 통과 — set_tenant_context 미적용 시 SELECT
                # 결과가 0건이 될 수 있음. caller가 active job을 update하는 케이스만
                # 가정되어 있으므로 None은 silently 무시.
                return
            domain_id = str(row[0])
            await set_tenant_context(session, domain_id)
            await session.execute(
                text(
                    f"UPDATE indexing_jobs SET {', '.join(set_clauses)} WHERE job_id = :job_id"
                ),
                params,
            )
            await session.commit()

    async def get(self, job_id: str) -> IndexingJobRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT domain_id, doc_id, doc_version, filename, status,
                               step, progress, total_chunks, indexed_chunks,
                               failed_chunks, error_message, failure_rate,
                               retry_count
                          FROM indexing_jobs
                         WHERE job_id = :job_id
                        """
                    ),
                    {"job_id": job_id},
                )
            ).first()
            if row is None:
                return None
            return IndexingJobRecord(
                job_id=job_id,
                domain_id=row[0],
                doc_id=row[1] or "",
                doc_version=row[2] or "",
                filename=row[3],
                status=row[4],
                step=row[5],
                progress=row[6] or 0,
                total_chunks=row[7] or 0,
                indexed_chunks=row[8] or 0,
                failed_chunks=list(row[9] or []),
                error_message=row[10],
                failure_rate=row[11],
                retry_count=row[12] or 0,
            )

    async def list_by_tenant(
        self,
        *,
        domain_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IndexingJobRecord]:
        """ADR-017 §7 — GET /api/{domain_id}/admin/indexing/jobs 페이징 조회."""
        sql = """
            SELECT job_id, doc_id, doc_version, filename, status, step, progress,
                   total_chunks, indexed_chunks, failed_chunks, error_message,
                   failure_rate, retry_count
              FROM indexing_jobs
             WHERE domain_id = :domain_id
        """
        params: dict[str, Any] = {"domain_id": domain_id, "limit": limit, "offset": offset}
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"

        async with self._session_factory() as session:
            await set_tenant_context(session, domain_id)
            rows = (await session.execute(text(sql), params)).all()
            return [
                IndexingJobRecord(
                    job_id=str(r[0]),
                    domain_id=domain_id,
                    doc_id=r[1] or "",
                    doc_version=r[2] or "",
                    filename=r[3],
                    status=r[4],
                    step=r[5],
                    progress=r[6] or 0,
                    total_chunks=r[7] or 0,
                    indexed_chunks=r[8] or 0,
                    failed_chunks=list(r[9] or []),
                    error_message=r[10],
                    failure_rate=r[11],
                    retry_count=r[12] or 0,
                )
                for r in rows
            ]
