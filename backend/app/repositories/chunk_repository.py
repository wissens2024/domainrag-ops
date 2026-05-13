"""PostgresChunkRepository — chunks 테이블 SQLAlchemy 구현 (ADR-007/012).

rag_core.interfaces.chunk_repository.ChunkRepository Protocol을 만족. replace_chunks는
(tenant_id, doc_id, doc_version, parser_version) 키 단위로 DELETE+INSERT를 한 트랜잭션에
처리한다. update_metadata는 chunk_id list에 대한 부분 UPDATE.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.chunk_repository import ChunkRecord

from app.core.rls import set_tenant_context


_KNOWN_COLUMNS = {
    "title",
    "page_number",
    "section_title",
    "heading_path",
    "content",
    "content_hash",
    "start_char",
    "end_char",
    "token_count",
    "department",
    "doc_type",
    "security_level",
    "acl",
    "approval_status",
    "tags",
    "valid_from",
    "valid_until",
    "embedding_model",
    "embedding_version",
    "vector_id",
    "pii_warnings",
}
_JSONB_COLUMNS = {"heading_path", "acl", "tags", "pii_warnings"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class PostgresChunkRepository:
    """chunks 테이블 SQLAlchemy 구현체.

    replace_chunks: 같은 (tenant_id, doc_id, doc_version, parser_version) 묶음의 기존
    rows를 DELETE 후 INSERT — full/chunk_re_split/parser_only 모드의 chunks 갱신 진입점.
    update_metadata: chunk_ids 리스트의 컬럼 부분 UPDATE (parser_only·embedding_only 모드).
    """

    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def replace_chunks(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        doc_version: str,
        parser_version: str,
        chunks: list[ChunkRecord],
    ) -> None:
        async with self._session_factory() as session:
            await set_tenant_context(session, tenant_id)
            await session.execute(
                text(
                    """
                    DELETE FROM chunks
                     WHERE tenant_id = :tenant_id
                       AND doc_id = :doc_id
                       AND doc_version = :doc_version
                       AND parser_version = :parser_version
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "doc_id": doc_id,
                    "doc_version": doc_version,
                    "parser_version": parser_version,
                },
            )
            if chunks:
                rows = [
                    {
                        "tenant_id": c.tenant_id,
                        "chunk_id": c.chunk_id,
                        "doc_id": c.doc_id,
                        "doc_version": c.doc_version,
                        "parser_version": c.parser_version,
                        "chunk_strategy": c.chunk_strategy,
                        "chunk_index": c.chunk_index,
                        "title": c.title,
                        "page_number": c.page_number,
                        "section_title": c.section_title,
                        "heading_path": _json(c.heading_path),
                        "content": c.content,
                        "content_hash": c.content_hash,
                        "start_char": c.start_char,
                        "end_char": c.end_char,
                        "token_count": c.token_count,
                        "department": c.department,
                        "doc_type": c.doc_type,
                        "security_level": c.security_level,
                        "acl": _json(c.acl),
                        "approval_status": c.approval_status,
                        "tags": _json(c.tags),
                        "valid_from": c.valid_from,
                        "valid_until": c.valid_until,
                        "embedding_model": c.embedding_model,
                        "embedding_version": c.embedding_version,
                        "vector_id": c.vector_id,
                        "pii_warnings": _json(c.pii_warnings),
                    }
                    for c in chunks
                ]
                await session.execute(
                    text(
                        """
                        INSERT INTO chunks (
                            tenant_id, chunk_id, doc_id, doc_version, parser_version,
                            chunk_strategy, chunk_index, title, page_number,
                            section_title, heading_path, content, content_hash,
                            start_char, end_char, token_count,
                            department, doc_type, security_level, acl,
                            approval_status, tags, valid_from, valid_until,
                            embedding_model, embedding_version, vector_id, pii_warnings
                        )
                        VALUES (
                            :tenant_id, :chunk_id, :doc_id, :doc_version, :parser_version,
                            :chunk_strategy, :chunk_index, :title, :page_number,
                            :section_title, CAST(:heading_path AS JSONB),
                            :content, :content_hash,
                            :start_char, :end_char, :token_count,
                            :department, :doc_type, :security_level,
                            CAST(:acl AS JSONB),
                            :approval_status, CAST(:tags AS JSONB),
                            :valid_from, :valid_until,
                            :embedding_model, :embedding_version, :vector_id,
                            CAST(:pii_warnings AS JSONB)
                        )
                        """
                    ),
                    rows,
                )
            await session.commit()

    async def update_metadata(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str],
        metadata: dict[str, Any],
    ) -> None:
        if not chunk_ids or not metadata:
            return
        # 알 수 없는 컬럼은 silently drop — Protocol 문서대로 chunks 테이블의 컬럼만 갱신.
        columns = {k: v for k, v in metadata.items() if k in _KNOWN_COLUMNS}
        if not columns:
            return

        set_clauses: list[str] = []
        params: dict[str, Any] = {"tenant_id": tenant_id}
        for k, v in columns.items():
            if k in _JSONB_COLUMNS:
                set_clauses.append(f"{k} = CAST(:val_{k} AS JSONB)")
                params[f"val_{k}"] = _json(v)
            else:
                set_clauses.append(f"{k} = :val_{k}")
                params[f"val_{k}"] = v

        stmt = (
            text(
                f"""
                UPDATE chunks
                   SET {", ".join(set_clauses)}
                 WHERE tenant_id = :tenant_id
                   AND chunk_id IN :chunk_ids
                """
            ).bindparams(bindparam("chunk_ids", expanding=True))
        )
        params["chunk_ids"] = list(chunk_ids)

        async with self._session_factory() as session:
            await set_tenant_context(session, tenant_id)
            await session.execute(stmt, params)
            await session.commit()

    async def delete_by_doc(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        doc_version: str | None = None,
    ) -> list[str]:
        """ADR-007/012 hard delete — chunks + chunks_archive 동시 삭제. chunk_id 목록 반환."""
        params: dict[str, Any] = {"tenant_id": tenant_id, "doc_id": doc_id}
        version_clause = ""
        if doc_version is not None:
            version_clause = " AND doc_version = :doc_version"
            params["doc_version"] = doc_version

        async with self._session_factory() as session:
            await set_tenant_context(session, tenant_id)
            ids_rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT chunk_id FROM chunks
                         WHERE tenant_id = :tenant_id AND doc_id = :doc_id
                         {version_clause}
                        """
                    ),
                    params,
                )
            ).all()
            chunk_ids = [str(r[0]) for r in ids_rows]
            if chunk_ids:
                await session.execute(
                    text(
                        f"""
                        DELETE FROM chunks
                         WHERE tenant_id = :tenant_id AND doc_id = :doc_id
                         {version_clause}
                        """
                    ),
                    params,
                )
                await session.execute(
                    text(
                        f"""
                        DELETE FROM chunks_archive
                         WHERE tenant_id = :tenant_id AND doc_id = :doc_id
                         {version_clause}
                        """
                    ),
                    params,
                )
            await session.commit()
            return chunk_ids

    async def list_by_doc(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        doc_version: str,
        parser_version: str | None = None,
    ) -> list[ChunkRecord]:
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "doc_id": doc_id,
            "doc_version": doc_version,
        }
        sql = """
            SELECT chunk_id, parser_version, chunk_strategy, chunk_index,
                   title, page_number, section_title, heading_path, content,
                   content_hash, start_char, end_char, token_count,
                   department, doc_type, security_level, acl, approval_status,
                   tags, valid_from, valid_until,
                   embedding_model, embedding_version, vector_id, pii_warnings
              FROM chunks
             WHERE tenant_id = :tenant_id
               AND doc_id = :doc_id
               AND doc_version = :doc_version
        """
        if parser_version is not None:
            sql += " AND parser_version = :parser_version"
            params["parser_version"] = parser_version
        sql += " ORDER BY chunk_index"

        async with self._session_factory() as session:
            await set_tenant_context(session, tenant_id)
            rows = (await session.execute(text(sql), params)).all()
            return [
                ChunkRecord(
                    tenant_id=tenant_id,
                    chunk_id=r[0],
                    doc_id=doc_id,
                    doc_version=doc_version,
                    parser_version=r[1],
                    chunk_strategy=r[2],
                    chunk_index=r[3],
                    title=r[4],
                    page_number=r[5],
                    section_title=r[6],
                    heading_path=list(r[7] or []),
                    content=r[8],
                    content_hash=r[9],
                    start_char=r[10],
                    end_char=r[11],
                    token_count=r[12],
                    department=r[13],
                    doc_type=r[14],
                    security_level=r[15],
                    acl=list(r[16] or []),
                    approval_status=r[17] or "draft",
                    tags=list(r[18] or []),
                    valid_from=r[19].isoformat() if r[19] else None,
                    valid_until=r[20].isoformat() if r[20] else None,
                    embedding_model=r[21],
                    embedding_version=r[22],
                    vector_id=r[23],
                    pii_warnings=list(r[24] or []),
                )
                for r in rows
            ]
