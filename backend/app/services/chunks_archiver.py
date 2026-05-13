"""PostgresChunksArchiver — chunks_archive 테이블 적재 + chunks.archived_at set
(ADR-012 §3).

흐름 (tenant 단위):
  1. set_tenant_context (RLS)
  2. find_candidates: SELECT chunks WHERE valid_until < :threshold AND archived_at IS NULL
  3. archive:
       a. INSERT INTO chunks_archive (...) SELECT ... FROM chunks WHERE chunk_id IN (...)
       b. UPDATE chunks SET archived_at = NOW() WHERE chunk_id IN (...)

vector store payload 갱신(`archived=true`)은 ArchivalWorker가 호출 (분리 — service는 DB만 책임).
"""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.chunk_repository import ChunkRecord
from rag_core.services.chunks_archiver import ArchivalBatchResult

from app.core.rls import set_tenant_context


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class PostgresChunksArchiver:
    """SQLAlchemy + RLS 구현체.

    Args:
        app_session_factory: domainrag_app role (RLS 적용)
    """

    def __init__(self, *, app_session_factory: async_sessionmaker) -> None:
        self._sf = app_session_factory

    async def find_candidates(
        self,
        *,
        tenant_id: str,
        valid_until_before: date,
    ) -> list[ChunkRecord]:
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT chunk_id, doc_id, doc_version, parser_version,
                               chunk_strategy, chunk_index, title, page_number,
                               section_title, heading_path, content, content_hash,
                               start_char, end_char, token_count, department, doc_type,
                               security_level, acl, approval_status, tags,
                               valid_from, valid_until, embedding_model, embedding_version,
                               vector_id, pii_warnings
                          FROM chunks
                         WHERE tenant_id = :tenant_id
                           AND archived_at IS NULL
                           AND valid_until IS NOT NULL
                           AND valid_until < :threshold
                         ORDER BY valid_until ASC
                        """
                    ),
                    {"tenant_id": tenant_id, "threshold": valid_until_before},
                )
            ).all()
            return [
                ChunkRecord(
                    tenant_id=tenant_id,
                    chunk_id=r[0],
                    doc_id=r[1],
                    doc_version=r[2] or "",
                    parser_version=r[3] or "p1",
                    chunk_strategy=r[4],
                    chunk_index=r[5],
                    title=r[6],
                    page_number=r[7],
                    section_title=r[8],
                    heading_path=list(r[9] or []),
                    content=r[10],
                    content_hash=r[11],
                    start_char=r[12],
                    end_char=r[13],
                    token_count=r[14],
                    department=r[15],
                    doc_type=r[16],
                    security_level=r[17],
                    acl=list(r[18] or []),
                    approval_status=r[19] or "draft",
                    tags=list(r[20] or []),
                    valid_from=r[21].isoformat() if r[21] else None,
                    valid_until=r[22].isoformat() if r[22] else None,
                    embedding_model=r[23],
                    embedding_version=r[24],
                    vector_id=r[25],
                    pii_warnings=list(r[26] or []),
                )
                for r in rows
            ]

    async def archive(
        self,
        *,
        tenant_id: str,
        chunks: list[ChunkRecord],
        reason: str = "valid_until_threshold",
    ) -> ArchivalBatchResult:
        if not chunks:
            return ArchivalBatchResult(tenant_id=tenant_id, reason=reason)

        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)

            chunk_ids = [c.chunk_id for c in chunks]
            # INSERT INTO chunks_archive ... SELECT FROM chunks (단일 트랜잭션 — atomic move)
            insert_stmt = (
                text(
                    """
                    INSERT INTO chunks_archive (
                        tenant_id, chunk_id, doc_id, doc_version, parser_version,
                        chunk_strategy, chunk_index, title, page_number, section_title,
                        heading_path, content, content_hash, start_char, end_char,
                        token_count, department, doc_type, security_level, acl,
                        approval_status, tags, valid_from, valid_until,
                        embedding_model, embedding_version, vector_id, pii_warnings,
                        created_at, archive_reason
                    )
                    SELECT
                        tenant_id, chunk_id, doc_id, doc_version, parser_version,
                        chunk_strategy, chunk_index, title, page_number, section_title,
                        heading_path, content, content_hash, start_char, end_char,
                        token_count, department, doc_type, security_level, acl,
                        approval_status, tags, valid_from, valid_until,
                        embedding_model, embedding_version, vector_id, pii_warnings,
                        created_at, :reason
                      FROM chunks
                     WHERE tenant_id = :tenant_id
                       AND chunk_id IN :chunk_ids
                       AND archived_at IS NULL
                    """
                )
                .bindparams(bindparam("chunk_ids", expanding=True))
            )
            await session.execute(
                insert_stmt,
                {"tenant_id": tenant_id, "chunk_ids": chunk_ids, "reason": reason},
            )

            update_stmt = (
                text(
                    """
                    UPDATE chunks
                       SET archived_at = NOW()
                     WHERE tenant_id = :tenant_id
                       AND chunk_id IN :chunk_ids
                       AND archived_at IS NULL
                    RETURNING chunk_id
                    """
                )
                .bindparams(bindparam("chunk_ids", expanding=True))
            )
            updated = (
                await session.execute(
                    update_stmt,
                    {"tenant_id": tenant_id, "chunk_ids": chunk_ids},
                )
            ).all()
            await session.commit()

            archived_ids = [r[0] for r in updated]
            skipped = [cid for cid in chunk_ids if cid not in set(archived_ids)]
            return ArchivalBatchResult(
                tenant_id=tenant_id,
                archived_chunk_ids=archived_ids,
                skipped_chunk_ids=skipped,
                reason=reason,
            )
