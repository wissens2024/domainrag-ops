"""DocumentApprovalService — ADR-012 §3-8 payload-only sync.

approval_status를 documents + chunks + Qdrant payload 3개 layer에 동시에 반영한다.
reindex 없음 — chunk content / embedding은 그대로, 메타데이터만 갱신.

흐름:
  1. document_repo.update_approval(domain_id, doc_id, version, approval_status)
     - documents row UPDATE
     - 같은 (domain_id, doc_id, doc_version) chunks rows도 함께 업데이트하지는 않음 — 본 service가 별도 처리
  2. chunks_for_update = chunk_repo.list_by_doc(tenant, doc_id, version)
  3. chunk_repo.update_metadata(chunk_ids, {"approval_status": ...})
  4. vector_store.set_payload(domain_id, chunk_ids, {"approval_status": ...})
  5. tenant_lifecycle_logs audit (document_approval_changed)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.chunk_repository import (
    ChunkRepository,
    DocumentRecord,
    DocumentRepository,
)
from rag_core.interfaces.vector_store import VectorStore

from app.core.rls import set_tenant_context

logger = logging.getLogger(__name__)


@dataclass
class ApprovalResult:
    document: DocumentRecord
    affected_chunks: int


class DocumentApprovalService:
    """ADR-012 §3-8 payload_only_sync 구현체.

    Args:
        document_repo / chunk_repo / vector_store: rag_core protocols
        admin_session_factory: BYPASSRLS admin engine (audit log)
    """

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        chunk_repo: ChunkRepository,
        vector_store: VectorStore,
        admin_session_factory: async_sessionmaker | None = None,
    ) -> None:
        self._docs = document_repo
        self._chunks = chunk_repo
        self._vs = vector_store
        self._admin = admin_session_factory

    async def set_approval(
        self,
        *,
        domain_id: str,
        doc_id: str,
        version: str,
        approval_status: str,
        actor: str,
        reason: str | None = None,
    ) -> ApprovalResult | None:
        """document approval 전이 + chunks/payload 동기화 + audit. None이면 doc 없음."""
        updated_doc = await self._docs.update_approval(
            domain_id=domain_id,
            doc_id=doc_id,
            version=version,
            approval_status=approval_status,
        )
        if updated_doc is None:
            return None

        existing_chunks = await self._chunks.list_by_doc(
            domain_id=domain_id, doc_id=doc_id, doc_version=version
        )
        chunk_ids = [c.chunk_id for c in existing_chunks]
        if chunk_ids:
            await self._chunks.update_metadata(
                domain_id=domain_id,
                chunk_ids=chunk_ids,
                metadata={"approval_status": approval_status},
            )
            try:
                await self._vs.set_payload(
                    domain_id=domain_id,
                    chunk_ids=chunk_ids,
                    payload={"approval_status": approval_status},
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "set_payload failed during approval sync: tenant=%s doc=%s err=%s",
                    domain_id, doc_id, exc,
                )

        await self._audit(
            domain_id=domain_id,
            actor=actor,
            details={
                "doc_id": doc_id,
                "version": version,
                "approval_status": approval_status,
                "affected_chunks": len(chunk_ids),
                "reason": reason,
            },
        )
        return ApprovalResult(document=updated_doc, affected_chunks=len(chunk_ids))

    async def _audit(
        self,
        *,
        domain_id: str,
        actor: str,
        details: dict[str, Any],
    ) -> None:
        if self._admin is None:
            return
        async with self._admin() as session:
            await set_tenant_context(session, domain_id)
            await session.execute(
                text(
                    """
                    INSERT INTO tenant_lifecycle_logs (
                        domain_id, action, from_state, to_state, actor, reason, details
                    )
                    VALUES (
                        :domain_id, 'document_approval_changed',
                        NULL, :to_state, :actor, :reason, CAST(:details AS JSONB)
                    )
                    """
                ),
                {
                    "domain_id": domain_id,
                    "to_state": details.get("approval_status"),
                    "actor": actor,
                    "reason": details.get("reason") or "approval_status_changed",
                    "details": json.dumps(details, ensure_ascii=False, default=str),
                },
            )
            await session.commit()
