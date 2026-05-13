"""HardDeleteService — ADR-007/012 cross-system hard delete.

순서 (best-effort + audit):
  1. chunks_archive + chunks DELETE → 삭제된 chunk_id 목록 확보
  2. vector_store.delete_points(chunk_ids) — Qdrant points 제거 (실패는 dead-letter)
  3. storage.delete(tenant, doc, version) — MinIO/Local 원본 삭제 (실패 시 dead-letter)
  4. documents DELETE
  5. chat_logs_action 처리:
      - keep_excerpts (default): no-op (ADR-010 excerpt가 audit truth, 그대로 보존)
      - mask_excerpts: chat_logs.citations 안의 해당 doc_id를 가진 entry의 excerpt만 마스킹
      - delete_logs: 해당 doc_id를 참조하는 chat_logs row 자체 DELETE
  6. tenant_lifecycle_logs document_hard_deleted audit + tenant_delete_failures(있으면) 기록

`affected_chat_logs` 카운트는 5단계에서 산출. 부분 실패는 result.dead_letters에 누적되어
caller가 운영 console에 노출 가능.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.chunk_repository import (
    ChunkRepository,
    DocumentRepository,
)
from rag_core.interfaces.vector_store import VectorStore

from app.core.rls import set_tenant_context
from app.services.document_storage import DocumentStorage

logger = logging.getLogger(__name__)


ChatLogsAction = Literal["keep_excerpts", "mask_excerpts", "delete_logs"]


@dataclass
class HardDeleteResult:
    tenant_id: str
    doc_id: str
    version: str | None
    removed_chunks: int
    removed_storage_files: int
    removed_documents: int
    affected_chat_logs: int
    chat_logs_action_applied: ChatLogsAction
    dead_letters: list[dict[str, Any]] = field(default_factory=list)


class HardDeleteService:
    """ADR-007/012 hard delete 책임 단일화."""

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        chunk_repo: ChunkRepository,
        vector_store: VectorStore,
        storage: DocumentStorage,
        app_session_factory: async_sessionmaker | None = None,
        admin_session_factory: async_sessionmaker | None = None,
        chat_logs_handler=None,
        ledger_audit=None,
    ) -> None:
        self._docs = document_repo
        self._chunks = chunk_repo
        self._vs = vector_store
        self._storage = storage
        self._app = app_session_factory      # chat_logs(RLS)
        self._admin = admin_session_factory  # audit log
        # InMemory backend에서 chat_logs.records를 대상으로 동작하는 핸들러를 받을 수 있다.
        # 주입 시 SQL 경로(_app) 대신 핸들러가 우선.
        self._chat_logs_handler = chat_logs_handler
        # ADR-020 §8 — LedgerAuditService (None이면 publish skip).
        self._ledger = ledger_audit

    async def execute(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        version: str | None = None,
        actor: str,
        reason: str,
        chat_logs_action: ChatLogsAction = "keep_excerpts",
    ) -> HardDeleteResult:
        result = HardDeleteResult(
            tenant_id=tenant_id,
            doc_id=doc_id,
            version=version,
            removed_chunks=0,
            removed_storage_files=0,
            removed_documents=0,
            affected_chat_logs=0,
            chat_logs_action_applied=chat_logs_action,
        )

        # 1) chunks DELETE
        try:
            chunk_ids = await self._chunks.delete_by_doc(
                tenant_id=tenant_id, doc_id=doc_id, doc_version=version
            )
            result.removed_chunks = len(chunk_ids)
        except Exception as exc:  # noqa: BLE001
            chunk_ids = []
            result.dead_letters.append(
                {"step": "delete_chunks", "error": str(exc)}
            )

        # 2) Qdrant points
        if chunk_ids:
            try:
                await self._vs.delete_points(
                    tenant_id=tenant_id, chunk_ids=chunk_ids
                )
            except Exception as exc:  # noqa: BLE001
                result.dead_letters.append(
                    {"step": "vector_store_delete", "chunk_ids": chunk_ids, "error": str(exc)}
                )

        # 3) Storage
        try:
            result.removed_storage_files = await self._storage.delete(
                tenant_id=tenant_id, doc_id=doc_id, version=version
            )
        except Exception as exc:  # noqa: BLE001
            result.dead_letters.append(
                {"step": "storage_delete", "error": str(exc)}
            )

        # 4) documents DELETE
        try:
            result.removed_documents = await self._docs.delete(
                tenant_id=tenant_id, doc_id=doc_id, version=version
            )
        except Exception as exc:  # noqa: BLE001
            result.dead_letters.append(
                {"step": "delete_document", "error": str(exc)}
            )

        # 5) chat_logs 처리
        result.affected_chat_logs = await self._handle_chat_logs(
            tenant_id=tenant_id, doc_id=doc_id, action=chat_logs_action
        )

        # 6) audit (tenant_lifecycle_logs)
        await self._audit(
            tenant_id=tenant_id, actor=actor, reason=reason, result=result
        )
        # 7) AuthFusion Ledger publish (ADR-020 §8). 실패는 swallow.
        if self._ledger is not None:
            try:
                await self._ledger.publish_hard_delete(
                    tenant_id=tenant_id,
                    actor=actor,
                    reason=reason,
                    doc_id=doc_id,
                    version=version,
                    removed_chunks=result.removed_chunks,
                    affected_chat_logs=result.affected_chat_logs,
                    chat_logs_action=result.chat_logs_action_applied,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ledger publish wrapper failed: %s", exc)
        return result

    # ------------------------------------------------------------------ #
    # chat_logs 옵션
    # ------------------------------------------------------------------ #

    async def _handle_chat_logs(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        action: ChatLogsAction,
    ) -> int:
        if action == "keep_excerpts":
            return 0
        if self._chat_logs_handler is not None:
            return await self._chat_logs_handler.apply(
                tenant_id=tenant_id, doc_id=doc_id, action=action
            )
        if self._app is None:
            return 0
        async with self._app() as session:
            await set_tenant_context(session, tenant_id)
            if action == "delete_logs":
                result = await session.execute(
                    text(
                        """
                        DELETE FROM chat_logs
                         WHERE tenant_id = :tenant_id
                           AND citations @> CAST(:doc_filter AS JSONB)
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "doc_filter": json.dumps([{"doc_id": doc_id}]),
                    },
                )
                count = result.rowcount or 0
            else:  # mask_excerpts
                # citations[].excerpt 중 doc_id 일치하는 항목을 mask 처리.
                # PostgreSQL jsonb_set는 path 기반이라 array 안 원소 일괄 update는 sub-query 필요.
                result = await session.execute(
                    text(
                        """
                        WITH affected AS (
                            SELECT id, created_at, citations
                              FROM chat_logs
                             WHERE tenant_id = :tenant_id
                               AND citations @> CAST(:doc_filter AS JSONB)
                        ),
                        masked AS (
                            SELECT a.id, a.created_at,
                                   jsonb_agg(
                                       CASE WHEN c->>'doc_id' = :doc_id
                                            THEN c - 'excerpt' || jsonb_build_object(
                                                'excerpt', '[masked: hard_deleted]'
                                            )
                                            ELSE c END
                                   ) AS new_citations
                              FROM affected a
                              CROSS JOIN LATERAL jsonb_array_elements(a.citations) c
                             GROUP BY a.id, a.created_at
                        )
                        UPDATE chat_logs cl
                           SET citations = m.new_citations
                          FROM masked m
                         WHERE cl.id = m.id AND cl.created_at = m.created_at
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "doc_id": doc_id,
                        "doc_filter": json.dumps([{"doc_id": doc_id}]),
                    },
                )
                count = result.rowcount or 0
            await session.commit()
            return count

    # ------------------------------------------------------------------ #
    # audit
    # ------------------------------------------------------------------ #

    async def _audit(
        self,
        *,
        tenant_id: str,
        actor: str,
        reason: str,
        result: HardDeleteResult,
    ) -> None:
        if self._admin is None:
            return
        async with self._admin() as session:
            await set_tenant_context(session, tenant_id)
            await session.execute(
                text(
                    """
                    INSERT INTO tenant_lifecycle_logs (
                        tenant_id, action, from_state, to_state, actor, reason, details
                    )
                    VALUES (
                        :tenant_id, 'document_hard_deleted',
                        NULL, NULL, :actor, :reason, CAST(:details AS JSONB)
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "actor": actor,
                    "reason": reason,
                    "details": json.dumps(
                        {
                            "doc_id": result.doc_id,
                            "version": result.version,
                            "removed_chunks": result.removed_chunks,
                            "removed_storage_files": result.removed_storage_files,
                            "removed_documents": result.removed_documents,
                            "affected_chat_logs": result.affected_chat_logs,
                            "chat_logs_action": result.chat_logs_action_applied,
                            "dead_letters": result.dead_letters,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            )
            await session.commit()


# --------------------------------------------------------------------------- #
# InMemory chat_logs masker — dev/tests 용
# --------------------------------------------------------------------------- #


class InMemoryChatLogsActionHandler:
    """InMemoryChatLogWriter.records를 대상으로 keep/mask/delete 처리.

    HardDeleteService가 _app session 없이 backed될 때 caller가 본 핸들러를 분리 호출.
    """

    def __init__(self, *, writer) -> None:
        self._writer = writer

    async def apply(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        action: ChatLogsAction,
    ) -> int:
        if action == "keep_excerpts":
            return 0
        affected = 0
        kept = []
        for rec in self._writer.records:
            if rec.tenant_id != tenant_id:
                kept.append(rec)
                continue
            citations = list(rec.citations or [])
            matches = [c for c in citations if c.get("doc_id") == doc_id]
            if not matches:
                kept.append(rec)
                continue
            affected += 1
            if action == "delete_logs":
                continue  # drop record
            # mask_excerpts
            new_citations = []
            for c in citations:
                if c.get("doc_id") == doc_id:
                    c = {**c, "excerpt": "[masked: hard_deleted]"}
                new_citations.append(c)
            rec.citations = new_citations
            kept.append(rec)
        self._writer.records = kept
        return affected
