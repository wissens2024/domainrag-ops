"""DocumentMetadataService — ADR-017 §6.4 PATCH documents (payload-only).

흐름:
  1. document_repo.get(tenant, doc, version) — 기존 record
  2. merged = existing 메타 + patch (deep merge for metadata dict)
  3. InputSchemaService.validate(input_type, merged_metadata) — 422 on fail
  4. document_repo.update_partial(tenant, doc, version, patch)
  5. chunks/Qdrant payload sync — chunk-syncable 필드만 (title, department, doc_type,
     security_level, tags, valid_from, valid_until)
  6. tenant_lifecycle_logs document_metadata_updated audit
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.chunk_repository import (
    ChunkRepository,
    DocumentRecord,
    DocumentRepository,
)
from rag_core.interfaces.vector_store import VectorStore

from app.core.input_schema import (
    InputSchemaService,
    InputSchemaValidationError,
)
from app.core.rls import set_tenant_context

logger = logging.getLogger(__name__)


# documents 컬럼 중 chunks/Qdrant payload에도 동기화해야 하는 키 (ADR-007/012 payload_only_sync).
# title은 chunks에 title 컬럼이 있으므로 동기화. metadata(domain fields)는 doc 단위라 chunks/Qdrant에 직접 매핑되지 않음.
_CHUNK_SYNC_KEYS = {
    "title",
    "department",
    "doc_type",
    "security_level",
    "tags",
    "valid_from",
    "valid_until",
}


@dataclass
class MetadataUpdateResult:
    document: DocumentRecord
    affected_chunks: int
    synced_keys: list[str]


class DocumentMetadataService:
    """ADR-017 §6.4 payload-only metadata 갱신.

    Args:
        document_repo / chunk_repo / vector_store: rag_core protocols
        schema_service: InputSchemaService — partial 검증 (merged 후)
        admin_session_factory: BYPASSRLS admin engine (audit log). None이면 audit skip
    """

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        chunk_repo: ChunkRepository,
        vector_store: VectorStore,
        schema_service: InputSchemaService,
        admin_session_factory: async_sessionmaker | None = None,
    ) -> None:
        self._docs = document_repo
        self._chunks = chunk_repo
        self._vs = vector_store
        self._schema = schema_service
        self._admin = admin_session_factory

    async def update(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        version: str,
        patch: dict[str, Any],
        actor: str,
        reason: str | None = None,
    ) -> MetadataUpdateResult | None:
        """patch만 받아 부분 갱신. None이면 doc 없음(404).

        Raises:
            InputSchemaValidationError: merged metadata가 input_type schema 위반
        """
        existing = await self._docs.get(
            tenant_id=tenant_id, doc_id=doc_id, version=version
        )
        if existing is None:
            return None

        # 1) merged metadata 빌드 (input_type schema 검증용). acl은 documents 컬럼에 없으므로
        #    같은 (doc, version)의 첫 chunk에서 보강 — common_required.acl 검증 통과 목적.
        chunks_for_acl = await self._chunks.list_by_doc(
            tenant_id=tenant_id, doc_id=doc_id, doc_version=version
        )
        existing_acl = list(chunks_for_acl[0].acl) if chunks_for_acl else []
        merged = _merge_for_validation(existing, patch, existing_acl=existing_acl)

        # 2) schema 검증 — input_type이 명시되어 있을 때만
        input_type = merged.get("input_type") or existing.input_type
        if input_type:
            self._schema.validate(
                tenant_id=tenant_id, input_type=input_type, metadata=merged
            )

        # 3) documents UPDATE
        updated = await self._docs.update_partial(
            tenant_id=tenant_id, doc_id=doc_id, version=version, patch=patch
        )
        if updated is None:  # race — 검증 중 다른 caller가 delete
            return None

        # 4) chunks/Qdrant sync — chunk-syncable 필드만
        sync_payload = {k: patch[k] for k in patch if k in _CHUNK_SYNC_KEYS}
        affected_chunks = 0
        if sync_payload:
            chunks = await self._chunks.list_by_doc(
                tenant_id=tenant_id, doc_id=doc_id, doc_version=version
            )
            chunk_ids = [c.chunk_id for c in chunks]
            if chunk_ids:
                await self._chunks.update_metadata(
                    tenant_id=tenant_id,
                    chunk_ids=chunk_ids,
                    metadata=sync_payload,
                )
                try:
                    await self._vs.set_payload(
                        tenant_id=tenant_id,
                        chunk_ids=chunk_ids,
                        payload=sync_payload,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "vector_store.set_payload failed during metadata update: "
                        "tenant=%s doc=%s err=%s",
                        tenant_id, doc_id, exc,
                    )
                affected_chunks = len(chunk_ids)

        # 5) audit
        await self._audit(
            tenant_id=tenant_id,
            actor=actor,
            details={
                "doc_id": doc_id,
                "version": version,
                "patched_keys": sorted(patch.keys()),
                "synced_keys": sorted(sync_payload.keys()),
                "affected_chunks": affected_chunks,
                "reason": reason,
            },
        )

        return MetadataUpdateResult(
            document=updated,
            affected_chunks=affected_chunks,
            synced_keys=sorted(sync_payload.keys()),
        )

    async def _audit(
        self,
        *,
        tenant_id: str,
        actor: str,
        details: dict[str, Any],
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
                        :tenant_id, 'document_metadata_updated',
                        NULL, NULL, :actor, :reason, CAST(:details AS JSONB)
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "actor": actor,
                    "reason": details.get("reason") or "metadata_patch",
                    "details": json.dumps(details, ensure_ascii=False, default=str),
                },
            )
            await session.commit()


def _merge_for_validation(
    existing: DocumentRecord,
    patch: dict[str, Any],
    *,
    existing_acl: list[str] | None = None,
) -> dict[str, Any]:
    """기존 record + patch를 input_schema validation에 줄 dict로 합성.

    common_required(title, security_level, acl, owner) + domain_fields(input_type별)를
    한 평면 dict에 담는다. patch의 metadata는 dict이므로 spread해서 도메인 키와 합친다.
    acl은 documents 컬럼에 없으므로 caller가 chunks에서 보강한 existing_acl을 base에 넣어
    common_required.acl 검증을 통과시킨다.
    """
    base: dict[str, Any] = {
        "title": existing.title,
        "security_level": existing.security_level,
        "owner": existing.owner,
        "acl": list(existing_acl or []),  # chunks 단위 필드 — caller가 list_by_doc로 보강
        "tags": list(existing.tags or []),
        "valid_from": existing.valid_from,
        "valid_until": existing.valid_until,
        "department": existing.department,
        "doc_type": existing.doc_type,
        "input_type": existing.input_type,
    }
    # 기존 metadata(domain fields)를 풀어 평면화
    if existing.metadata:
        base.update(dict(existing.metadata))

    # patch — 최상위 키 + patch.metadata 평면화
    patch_meta = patch.get("metadata") or {}
    for k, v in patch.items():
        if k == "metadata":
            continue
        base[k] = v
    if isinstance(patch_meta, dict):
        base.update(patch_meta)

    # date 객체를 ISO 문자열로 (schema validate가 format=date 검증 시 str을 기대)
    for k in ("valid_from", "valid_until"):
        v = base.get(k)
        if isinstance(v, date):
            base[k] = v.isoformat()
    return base
