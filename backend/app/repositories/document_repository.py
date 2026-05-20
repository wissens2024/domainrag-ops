"""PostgresDocumentRepository — documents UPSERT/GET (ADR-012).

rag_core.interfaces.chunk_repository.DocumentRepository Protocol 구현. tenant_id
RLS context는 매 호출마다 set_tenant_context로 주입.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.chunk_repository import DocumentRecord

from app.core.timezone import iso_kst
from app.core.rls import set_tenant_context


class PostgresDocumentRepository:
    """documents 테이블 SQLAlchemy 구현체.

    UPSERT는 (tenant_id, doc_id, version) 유니크 키 기반.
    """

    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def upsert(self, doc: DocumentRecord) -> None:
        async with self._session_factory() as session:
            await set_tenant_context(session, doc.tenant_id)
            await session.execute(
                text(
                    """
                    INSERT INTO documents (
                        tenant_id, doc_id, title, input_type, source_type,
                        source_path, object_storage_path, department, doc_type,
                        security_level, version, owner, tags, language,
                        valid_from, valid_until, approval_status, file_hash,
                        parser_version, metadata
                    )
                    VALUES (
                        :tenant_id, :doc_id, :title, :input_type, :source_type,
                        :source_path, :object_storage_path, :department, :doc_type,
                        :security_level, :version, :owner,
                        CAST(:tags AS JSONB), :language,
                        :valid_from, :valid_until, :approval_status, :file_hash,
                        :parser_version, CAST(:metadata AS JSONB)
                    )
                    ON CONFLICT (tenant_id, doc_id, version) DO UPDATE SET
                        title = EXCLUDED.title,
                        input_type = EXCLUDED.input_type,
                        source_type = EXCLUDED.source_type,
                        source_path = EXCLUDED.source_path,
                        object_storage_path = EXCLUDED.object_storage_path,
                        department = EXCLUDED.department,
                        doc_type = EXCLUDED.doc_type,
                        security_level = EXCLUDED.security_level,
                        owner = EXCLUDED.owner,
                        tags = EXCLUDED.tags,
                        language = EXCLUDED.language,
                        valid_from = EXCLUDED.valid_from,
                        valid_until = EXCLUDED.valid_until,
                        approval_status = EXCLUDED.approval_status,
                        file_hash = EXCLUDED.file_hash,
                        parser_version = EXCLUDED.parser_version,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                    """
                ),
                {
                    "tenant_id": doc.tenant_id,
                    "doc_id": doc.doc_id,
                    "title": doc.title,
                    "input_type": doc.input_type,
                    "source_type": doc.source_type,
                    "source_path": doc.source_path,
                    "object_storage_path": doc.object_storage_path,
                    "department": doc.department,
                    "doc_type": doc.doc_type,
                    "security_level": doc.security_level,
                    "version": doc.version,
                    "owner": doc.owner,
                    "tags": _json(doc.tags),
                    "language": doc.language,
                    "valid_from": doc.valid_from,
                    "valid_until": doc.valid_until,
                    "approval_status": doc.approval_status,
                    "file_hash": doc.file_hash,
                    "parser_version": doc.parser_version,
                    "metadata": _json(doc.metadata),
                },
            )
            await session.commit()

    async def list_by_tenant(
        self,
        *,
        tenant_id: str,
        keyword: str | None = None,
        approval_status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        # 같은 doc_id의 가장 최신 version 1건만 노출 — version은 'v1','v2'... 패턴 가정
        sql = """
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY doc_id
                    ORDER BY (regexp_replace(version, '\\D', '', 'g'))::int DESC NULLS LAST,
                             version DESC
                ) AS rn
                  FROM documents
                 WHERE tenant_id = :tenant_id
                   {keyword_clause}
                   {status_clause}
            )
            SELECT doc_id, title, input_type, source_type, source_path,
                   object_storage_path, department, doc_type, security_level,
                   version, owner, tags, language, valid_from, valid_until,
                   approval_status, file_hash, parser_version, metadata
              FROM ranked
             WHERE rn = 1
             ORDER BY doc_id
             LIMIT :limit OFFSET :offset
        """
        params: dict = {"tenant_id": tenant_id, "limit": limit, "offset": offset}
        keyword_clause = ""
        if keyword:
            keyword_clause = "AND (LOWER(title) LIKE :keyword OR LOWER(doc_id) LIKE :keyword)"
            params["keyword"] = f"%{keyword.lower()}%"
        status_clause = ""
        if approval_status:
            status_clause = "AND approval_status = :approval_status"
            params["approval_status"] = approval_status
        sql = sql.format(keyword_clause=keyword_clause, status_clause=status_clause)

        async with self._session_factory() as session:
            await set_tenant_context(session, tenant_id)
            rows = (await session.execute(text(sql), params)).all()
            return [_row_to_doc(tenant_id, r) for r in rows]

    async def update_approval(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        version: str,
        approval_status: str,
    ) -> DocumentRecord | None:
        async with self._session_factory() as session:
            await set_tenant_context(session, tenant_id)
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE documents
                           SET approval_status = :approval_status,
                               updated_at = NOW()
                         WHERE tenant_id = :tenant_id
                           AND doc_id = :doc_id
                           AND version = :version
                        RETURNING title, input_type, source_type, source_path,
                                  object_storage_path, department, doc_type,
                                  security_level, owner, tags, language,
                                  valid_from, valid_until, approval_status,
                                  file_hash, parser_version, metadata
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "doc_id": doc_id,
                        "version": version,
                        "approval_status": approval_status,
                    },
                )
            ).first()
            if row is None:
                await session.rollback()
                return None
            await session.commit()
            # row to record (version 보강)
            return DocumentRecord(
                tenant_id=tenant_id,
                doc_id=doc_id,
                title=row[0],
                input_type=row[1],
                source_type=row[2],
                source_path=row[3],
                object_storage_path=row[4],
                department=row[5],
                doc_type=row[6],
                security_level=row[7] or "internal",
                version=version,
                owner=row[8],
                tags=list(row[9] or []),
                language=row[10] or "ko",
                valid_from=iso_kst(row[11]),
                valid_until=iso_kst(row[12]),
                approval_status=row[13] or "draft",
                file_hash=row[14],
                parser_version=row[15] or "p1",
                metadata=dict(row[16] or {}),
            )

    _PARTIAL_COLUMNS = {
        "title": "string",
        "input_type": "string",
        "source_type": "string",
        "object_storage_path": "string",
        "department": "string",
        "doc_type": "string",
        "security_level": "string",
        "owner": "string",
        "language": "string",
        "valid_from": "date",
        "valid_until": "date",
        "tags": "jsonb",
        "metadata": "jsonb",
    }

    async def update_partial(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        version: str,
        patch: dict,
    ) -> DocumentRecord | None:
        """ADR-017 §6.4 — documents 부분 갱신.

        patch dict의 키 중 _PARTIAL_COLUMNS에 정의된 것만 적용. 모르는 키는 silently drop.
        approval_status는 update_approval로 분리 (ADR-012 §3-8).
        """
        cols: list[str] = []
        params: dict = {"tenant_id": tenant_id, "doc_id": doc_id, "version": version}
        for key, kind in self._PARTIAL_COLUMNS.items():
            if key not in patch:
                continue
            value = patch[key]
            if kind == "jsonb":
                cols.append(f"{key} = CAST(:val_{key} AS JSONB)")
                params[f"val_{key}"] = _json(value)
            else:
                cols.append(f"{key} = :val_{key}")
                params[f"val_{key}"] = value

        if not cols:
            # 변경할 컬럼이 없으면 현재 row만 반환 (멱등)
            return await self.get(tenant_id=tenant_id, doc_id=doc_id, version=version)

        sql = f"""
            UPDATE documents
               SET {", ".join(cols)},
                   updated_at = NOW()
             WHERE tenant_id = :tenant_id
               AND doc_id = :doc_id
               AND version = :version
            RETURNING title, input_type, source_type, source_path, object_storage_path,
                      department, doc_type, security_level, owner, tags, language,
                      valid_from, valid_until, approval_status, file_hash, parser_version,
                      metadata
        """
        async with self._session_factory() as session:
            await set_tenant_context(session, tenant_id)
            row = (await session.execute(text(sql), params)).first()
            if row is None:
                await session.rollback()
                return None
            await session.commit()
            return DocumentRecord(
                tenant_id=tenant_id,
                doc_id=doc_id,
                title=row[0],
                input_type=row[1],
                source_type=row[2],
                source_path=row[3],
                object_storage_path=row[4],
                department=row[5],
                doc_type=row[6],
                security_level=row[7] or "internal",
                version=version,
                owner=row[8],
                tags=list(row[9] or []),
                language=row[10] or "ko",
                valid_from=iso_kst(row[11]),
                valid_until=iso_kst(row[12]),
                approval_status=row[13] or "draft",
                file_hash=row[14],
                parser_version=row[15] or "p1",
                metadata=dict(row[16] or {}),
            )

    async def delete(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        version: str | None = None,
    ) -> int:
        """ADR-007/012 hard delete — documents row 삭제."""
        async with self._session_factory() as session:
            await set_tenant_context(session, tenant_id)
            if version is None:
                result = await session.execute(
                    text(
                        """
                        DELETE FROM documents
                         WHERE tenant_id = :tenant_id AND doc_id = :doc_id
                        """
                    ),
                    {"tenant_id": tenant_id, "doc_id": doc_id},
                )
            else:
                result = await session.execute(
                    text(
                        """
                        DELETE FROM documents
                         WHERE tenant_id = :tenant_id
                           AND doc_id = :doc_id
                           AND version = :version
                        """
                    ),
                    {"tenant_id": tenant_id, "doc_id": doc_id, "version": version},
                )
            count = result.rowcount or 0
            await session.commit()
            return count

    async def get(
        self, *, tenant_id: str, doc_id: str, version: str
    ) -> DocumentRecord | None:
        async with self._session_factory() as session:
            await set_tenant_context(session, tenant_id)
            row = (
                await session.execute(
                    text(
                        """
                        SELECT title, input_type, source_type, source_path,
                               object_storage_path, department, doc_type,
                               security_level, owner, tags, language,
                               valid_from, valid_until, approval_status,
                               file_hash, parser_version, metadata
                          FROM documents
                         WHERE tenant_id = :tenant_id
                           AND doc_id = :doc_id
                           AND version = :version
                        """
                    ),
                    {"tenant_id": tenant_id, "doc_id": doc_id, "version": version},
                )
            ).first()
            if row is None:
                return None
            return DocumentRecord(
                tenant_id=tenant_id,
                doc_id=doc_id,
                title=row[0],
                input_type=row[1],
                source_type=row[2],
                source_path=row[3],
                object_storage_path=row[4],
                department=row[5],
                doc_type=row[6],
                security_level=row[7] or "internal",
                version=version,
                owner=row[8],
                tags=list(row[9] or []),
                language=row[10] or "ko",
                valid_from=iso_kst(row[11]),
                valid_until=iso_kst(row[12]),
                approval_status=row[13] or "draft",
                file_hash=row[14],
                parser_version=row[15] or "p1",
                metadata=dict(row[16] or {}),
            )


def _json(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _row_to_doc(tenant_id: str, r) -> DocumentRecord:
    """list_by_tenant SELECT 컬럼 순서 정합 헬퍼."""
    return DocumentRecord(
        tenant_id=tenant_id,
        doc_id=r[0],
        title=r[1],
        input_type=r[2],
        source_type=r[3],
        source_path=r[4],
        object_storage_path=r[5],
        department=r[6],
        doc_type=r[7],
        security_level=r[8] or "internal",
        version=r[9] or "v1",
        owner=r[10],
        tags=list(r[11] or []),
        language=r[12] or "ko",
        valid_from=iso_kst(r[13]),
        valid_until=iso_kst(r[14]),
        approval_status=r[15] or "draft",
        file_hash=r[16],
        parser_version=r[17] or "p1",
        metadata=dict(r[18] or {}),
    )
