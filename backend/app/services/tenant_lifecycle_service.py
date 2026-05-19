"""TenantLifecycleService — ADR-008/012 + ADR-017 §18 tenant CRUD + status 전이 + hard delete.

본 service:
  - register: tenants row + 적절한 collection 생성 (vector store) + storage prefix 보장
  - get / list: 단순 조회
  - update_status: ADR-012 §2 active/suspended/archived 전이 검증
  - hard_delete: archived 상태일 때만 허용. chunks DB / Qdrant collection / storage prefix
    전부 삭제 + tenants.status=deleted + Ledger publish_platform_admin_action.
    실패 step은 tenant_delete_failures dead-letter (InMemory variant는 in-process list).

Postgres 운영 구현은 후속 작업으로 분리. 본 작업에서는 InMemory만 결선 → admin endpoint
+ 테스트는 inmemory backend로 검증 가능.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ADR-012 §2 status 전이 표
_TRANSITIONS = {
    "active": {"suspended", "archived"},
    "suspended": {"active", "archived"},
    "archived": {"active"},   # 복구 가능
    "deleted": set(),         # 종료 상태
}


@dataclass
class TenantRecord:
    tenant_id: str
    display_name: str
    domain_type: str | None = None
    embedding_model: str = "bge-m3"
    embedding_migration_state: str = "idle"
    status: str = "active"
    modules: list[str] = field(default_factory=lambda: ["rag"])
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    delete_status: str | None = None
    deleted_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "domain_type": self.domain_type,
            "embedding_model": self.embedding_model,
            "status": self.status,
            "modules": list(self.modules),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "delete_status": self.delete_status,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }


class TenantNotFoundError(Exception):
    def __init__(self, tenant_id: str):
        super().__init__(f"tenant not found: {tenant_id}")
        self.tenant_id = tenant_id


class TenantConflictError(Exception):
    def __init__(self, tenant_id: str):
        super().__init__(f"tenant already exists: {tenant_id}")
        self.tenant_id = tenant_id


class InvalidStatusTransitionError(Exception):
    def __init__(self, frm: str, to: str):
        super().__init__(f"status transition not allowed: {frm} -> {to}")
        self.frm = frm
        self.to = to


class TenantNotArchivedError(Exception):
    """hard delete는 archived 상태만 허용."""

    def __init__(self, tenant_id: str, status: str):
        super().__init__(
            f"tenant must be in 'archived' status for hard_delete, got {status}"
        )
        self.tenant_id = tenant_id
        self.status = status


class PostgresTenantLifecycleService:
    """ADR-012 §2·§6 운영 구현. admin engine(BYPASSRLS)로 tenants/관련 테이블 직접 조작.

    cross-system 일관성 (hard_delete):
        1. tenants.delete_status='in_progress'
        2. Postgres rows DELETE — 모든 tenant-scoped 테이블 (chunks, documents, chunks_archive,
           indexing_jobs, chat_logs[옵션], evaluation_jobs, tenant_config_overrides,
           tenant_config_change_logs, pii_storage_approvals, user_tenant_membership)
        3. Qdrant: delete_collection(chunks_<tenant_id>)
        4. MinIO: storage prefix rm (tenant 전체)
        5. tenants.status='deleted', delete_status='completed'|'partial'
        실패한 step은 tenant_delete_failures dead-letter에 누적.
    """

    # ADR-008 RLS 적용 테이블 중 tenant_id 컬럼이 있는 것 (RLS bypass 후 DELETE)
    _TENANT_SCOPED_TABLES = (
        "chunks_archive", "chunks", "documents", "indexing_jobs",
        "evaluation_jobs", "messages", "conversations", "chat_logs",
        "tenant_config_change_logs", "tenant_config_overrides",
        "tenant_input_schemas", "tenant_lifecycle_logs",
        "adapter_registry", "assessment_items", "assessment_logs",
        "pii_storage_approvals", "user_tenant_membership",
    )

    def __init__(
        self,
        *,
        admin_session_factory,
        vector_store=None,
        storage=None,
        embedder_dim_provider=None,
        ledger_audit=None,
    ) -> None:
        self._sf = admin_session_factory
        self._vector_store = vector_store
        self._storage = storage
        self._dim_provider = embedder_dim_provider or (lambda: 1024)
        self._ledger = ledger_audit

    async def register(
        self,
        *,
        tenant_id: str,
        display_name: str,
        domain_type: str | None = None,
        embedding_model: str | None = None,
        modules: list[str] | None = None,
        actor: str,
    ) -> TenantRecord:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError
        import json as _json

        async with self._sf() as session:
            try:
                # embedding_model이 None이면 DB default("bge-m3" 표준)를 그대로 사용.
                # 제공 시 명시적으로 INSERT 컬럼에 추가.
                if embedding_model:
                    row = (
                        await session.execute(
                            text(
                                """
                                INSERT INTO tenants (tenant_id, display_name, domain_type,
                                                     embedding_model, modules, status)
                                VALUES (:tenant_id, :display_name, :domain_type,
                                        :embedding_model, CAST(:modules AS JSONB), 'active')
                                RETURNING embedding_model, embedding_migration_state,
                                          created_at, updated_at
                                """
                            ),
                            {
                                "tenant_id": tenant_id,
                                "display_name": display_name,
                                "domain_type": domain_type,
                                "embedding_model": embedding_model,
                                "modules": _json.dumps(list(modules or ["rag"])),
                            },
                        )
                    ).first()
                else:
                    row = (
                        await session.execute(
                            text(
                                """
                                INSERT INTO tenants (tenant_id, display_name, domain_type,
                                                     modules, status)
                                VALUES (:tenant_id, :display_name, :domain_type,
                                        CAST(:modules AS JSONB), 'active')
                                RETURNING embedding_model, embedding_migration_state,
                                          created_at, updated_at
                                """
                            ),
                            {
                                "tenant_id": tenant_id,
                                "display_name": display_name,
                                "domain_type": domain_type,
                                "modules": _json.dumps(list(modules or ["rag"])),
                            },
                        )
                    ).first()
            except IntegrityError as exc:
                await session.rollback()
                raise TenantConflictError(tenant_id) from exc
            await session.commit()

        embedding_model = row[0] if row else "bge-m3"
        embedding_migration_state = row[1] if row else "idle"
        created_at = row[2] if row else datetime.now(timezone.utc)
        updated_at = row[3] if row else created_at

        # vector store collection (idempotent)
        if self._vector_store is not None:
            try:
                await self._vector_store.create_collection(
                    tenant_id=tenant_id, dense_dim=self._dim_provider()
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("collection create swallow: %s", exc)

        record = TenantRecord(
            tenant_id=tenant_id, display_name=display_name,
            domain_type=domain_type,
            embedding_model=embedding_model,
            embedding_migration_state=embedding_migration_state,
            status="active", modules=list(modules or ["rag"]),
            created_at=created_at, updated_at=updated_at,
        )
        await self._publish(
            actor=actor, action="tenant_registered",
            tenant_id=tenant_id, details={"display_name": display_name},
        )
        return record

    async def get(self, tenant_id: str) -> TenantRecord:
        rec = await self._fetch(tenant_id)
        if rec is None:
            raise TenantNotFoundError(tenant_id)
        return rec

    async def _fetch(self, tenant_id: str) -> TenantRecord | None:
        from sqlalchemy import text

        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT display_name, domain_type, embedding_model,
                               embedding_migration_state, status, modules,
                               created_at, updated_at, delete_status, deleted_at
                          FROM tenants
                         WHERE tenant_id = :tenant_id
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).first()
        if row is None:
            return None
        return TenantRecord(
            tenant_id=tenant_id,
            display_name=row[0],
            domain_type=row[1],
            embedding_model=row[2] or "bge-m3",
            embedding_migration_state=row[3] or "idle",
            status=row[4] or "active",
            modules=list(row[5] or ["rag"]),
            created_at=row[6] or datetime.now(timezone.utc),
            updated_at=row[7] or row[6] or datetime.now(timezone.utc),
            delete_status=row[8],
            deleted_at=row[9],
        )

    async def list_(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[TenantRecord]:
        from sqlalchemy import text

        sql = """
            SELECT tenant_id, display_name, domain_type, embedding_model,
                   embedding_migration_state, status, modules,
                   created_at, updated_at, delete_status, deleted_at
              FROM tenants
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            sql += " WHERE status = :status"
            params["status"] = status
        sql += " ORDER BY tenant_id LIMIT :limit OFFSET :offset"
        async with self._sf() as session:
            rows = (await session.execute(text(sql), params)).all()
        return [
            TenantRecord(
                tenant_id=r[0], display_name=r[1], domain_type=r[2],
                embedding_model=r[3] or "bge-m3",
                embedding_migration_state=r[4] or "idle",
                status=r[5] or "active",
                modules=list(r[6] or ["rag"]),
                created_at=r[7], updated_at=r[8] or r[7],
                delete_status=r[9], deleted_at=r[10],
            )
            for r in rows
        ]

    async def update_status(
        self, *, tenant_id: str, to_status: str, actor: str, reason: str | None = None
    ) -> TenantRecord:
        from sqlalchemy import text

        rec = await self.get(tenant_id)
        if rec.status == to_status:
            return rec
        if to_status not in _TRANSITIONS.get(rec.status, set()):
            raise InvalidStatusTransitionError(rec.status, to_status)

        async with self._sf() as session:
            await session.execute(
                text(
                    """
                    UPDATE tenants
                       SET status = :to_status, updated_at = NOW()
                     WHERE tenant_id = :tenant_id
                    """
                ),
                {"to_status": to_status, "tenant_id": tenant_id},
            )
            await session.commit()
        rec.status = to_status
        rec.updated_at = datetime.now(timezone.utc)
        await self._publish(
            actor=actor, action="tenant_status_changed",
            tenant_id=tenant_id,
            details={"from": rec.status if False else "(prev)", "to": to_status, "reason": reason},
        )
        return rec

    async def hard_delete(
        self, *, tenant_id: str, actor: str, reason: str
    ) -> TenantRecord:
        from sqlalchemy import text

        rec = await self.get(tenant_id)
        if rec.status != "archived":
            raise TenantNotArchivedError(tenant_id, rec.status)

        steps_failed: list[dict[str, Any]] = []

        # 1) tenants.delete_status='in_progress'
        async with self._sf() as session:
            await session.execute(
                text(
                    "UPDATE tenants SET delete_status='in_progress' WHERE tenant_id=:tid"
                ),
                {"tid": tenant_id},
            )
            await session.commit()

        # 2) Postgres rows DELETE — 자식 → 부모 순 (tenants 마지막)
        for table in self._TENANT_SCOPED_TABLES:
            try:
                async with self._sf() as session:
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :tid"),
                        {"tid": tenant_id},
                    )
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                steps_failed.append(
                    {"step": f"postgres:{table}", "error": str(exc)}
                )

        # 3) Qdrant collection drop
        if self._vector_store is not None:
            try:
                await self._vector_store.delete_collection(tenant_id)
            except Exception as exc:  # noqa: BLE001
                steps_failed.append({"step": "qdrant", "error": str(exc)})

        # 4) Storage prefix rm
        if self._storage is not None:
            try:
                await self._storage.delete(tenant_id=tenant_id, doc_id="")
            except Exception as exc:  # noqa: BLE001
                steps_failed.append({"step": "storage", "error": str(exc)})

        # 5) tenants status=deleted (FK CASCADE 대비 — 부모 tenants는 마지막에)
        async with self._sf() as session:
            await session.execute(
                text(
                    """
                    UPDATE tenants
                       SET status='deleted',
                           delete_status=:ds,
                           deleted_at=NOW(),
                           updated_at=NOW()
                     WHERE tenant_id=:tid
                    """
                ),
                {
                    "ds": "completed" if not steps_failed else "partial",
                    "tid": tenant_id,
                },
            )
            # dead-letter
            for f in steps_failed:
                await session.execute(
                    text(
                        """
                        INSERT INTO tenant_delete_failures (
                            tenant_id, failed_step, error_message
                        ) VALUES (:tid, :step, :err)
                        """
                    ),
                    {"tid": tenant_id, "step": f["step"], "err": f["error"]},
                )
            await session.commit()

        rec.status = "deleted"
        rec.delete_status = "completed" if not steps_failed else "partial"
        rec.deleted_at = datetime.now(timezone.utc)
        rec.updated_at = rec.deleted_at

        await self._publish(
            actor=actor, action="tenant_hard_deleted",
            tenant_id=tenant_id,
            details={
                "reason": reason,
                "delete_status": rec.delete_status,
                "failed_steps": [f["step"] for f in steps_failed],
            },
        )
        return rec

    async def list_delete_failures(self, tenant_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import text

        async with self._sf() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT failed_step, error_message, retry_count,
                               last_attempt_at, resolved_at, created_at
                          FROM tenant_delete_failures
                         WHERE tenant_id = :tid
                         ORDER BY created_at DESC
                        """
                    ),
                    {"tid": tenant_id},
                )
            ).all()
        return [
            {
                "failed_step": r[0],
                "error_message": r[1],
                "retry_count": r[2],
                "last_attempt_at": r[3].isoformat() if r[3] else None,
                "resolved_at": r[4].isoformat() if r[4] else None,
                "created_at": r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ]

    async def _publish(
        self, *, actor: str, action: str, tenant_id: str, details: dict[str, Any]
    ) -> None:
        if self._ledger is None:
            return
        try:
            await self._ledger.publish_platform_admin_action(
                tenant_id=tenant_id, actor=actor, action=action, details=details
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ledger publish failed: %s (swallow)", exc)


class InMemoryTenantLifecycleService:
    """ADR-008/012 lifecycle + ADR-017 §18 admin API의 inmemory 구현체.

    운영 Postgres 구현은 같은 메서드 시그니처로 별도 service를 만들어 deps에서 분기.
    본 service는 vector_store / storage를 받아 register/hard_delete 시 호출.
    """

    def __init__(
        self,
        *,
        vector_store=None,
        storage=None,
        embedder_dim_provider=None,
        ledger_audit=None,
    ) -> None:
        self._tenants: dict[str, TenantRecord] = {}
        self._delete_failures: list[dict[str, Any]] = []
        self._vector_store = vector_store
        self._storage = storage
        # vector store는 collection 생성 시 dense_dim 필요 — embedder.dense_dim 위임 가능
        self._dim_provider = embedder_dim_provider or (lambda: 64)
        self._ledger = ledger_audit

    async def register(
        self,
        *,
        tenant_id: str,
        display_name: str,
        domain_type: str | None = None,
        embedding_model: str | None = None,
        modules: list[str] | None = None,
        actor: str,
    ) -> TenantRecord:
        if tenant_id in self._tenants:
            raise TenantConflictError(tenant_id)
        # InMemory도 embedding_model 받지만 무시 — dim_provider만 사용 (테스트용)
        _ = embedding_model
        record = TenantRecord(
            tenant_id=tenant_id,
            display_name=display_name,
            domain_type=domain_type,
            modules=list(modules or ["rag"]),
        )
        self._tenants[tenant_id] = record

        # vector store collection — 이미 있어도 idempotent하게 swallow
        if self._vector_store is not None:
            try:
                await self._vector_store.create_collection(
                    tenant_id=tenant_id, dense_dim=self._dim_provider()
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("collection create swallow: %s", exc)

        await self._publish(
            actor=actor, action="tenant_registered",
            tenant_id=tenant_id, details={"display_name": display_name},
        )
        return record

    async def get(self, tenant_id: str) -> TenantRecord:
        rec = self._tenants.get(tenant_id)
        if rec is None:
            raise TenantNotFoundError(tenant_id)
        return rec

    async def list_(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[TenantRecord]:
        out = list(self._tenants.values())
        if status:
            out = [t for t in out if t.status == status]
        out.sort(key=lambda t: t.tenant_id)
        return out[offset : offset + limit]

    async def update_status(
        self, *, tenant_id: str, to_status: str, actor: str, reason: str | None = None
    ) -> TenantRecord:
        rec = await self.get(tenant_id)
        if rec.status == to_status:
            return rec
        if to_status not in _TRANSITIONS.get(rec.status, set()):
            raise InvalidStatusTransitionError(rec.status, to_status)
        old = rec.status
        rec.status = to_status
        rec.updated_at = datetime.now(timezone.utc)
        await self._publish(
            actor=actor,
            action="tenant_status_changed",
            tenant_id=tenant_id,
            details={"from": old, "to": to_status, "reason": reason},
        )
        return rec

    async def hard_delete(
        self, *, tenant_id: str, actor: str, reason: str
    ) -> TenantRecord:
        rec = await self.get(tenant_id)
        if rec.status != "archived":
            raise TenantNotArchivedError(tenant_id, rec.status)

        rec.delete_status = "in_progress"
        steps_failed: list[dict[str, Any]] = []

        # Qdrant collection drop
        if self._vector_store is not None:
            try:
                await self._vector_store.delete_collection(tenant_id)
            except Exception as exc:  # noqa: BLE001
                steps_failed.append({"step": "qdrant", "error": str(exc)})

        # Storage prefix rm (전체 tenant)
        if self._storage is not None:
            try:
                await self._storage.delete(tenant_id=tenant_id, doc_id="")
            except Exception as exc:  # noqa: BLE001
                steps_failed.append({"step": "storage", "error": str(exc)})

        # tenants status=deleted
        rec.status = "deleted"
        rec.delete_status = "completed" if not steps_failed else "partial"
        rec.deleted_at = datetime.now(timezone.utc)
        rec.updated_at = rec.deleted_at

        if steps_failed:
            for f in steps_failed:
                self._delete_failures.append(
                    {
                        "tenant_id": tenant_id,
                        "failed_step": f["step"],
                        "error_message": f["error"],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

        await self._publish(
            actor=actor,
            action="tenant_hard_deleted",
            tenant_id=tenant_id,
            details={
                "reason": reason,
                "delete_status": rec.delete_status,
                "failed_steps": [f["step"] for f in steps_failed],
            },
        )
        return rec

    async def list_delete_failures(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            f for f in self._delete_failures if f["tenant_id"] == tenant_id
        ]

    async def _publish(
        self, *, actor: str, action: str, tenant_id: str, details: dict[str, Any]
    ) -> None:
        if self._ledger is None:
            return
        try:
            await self._ledger.publish_platform_admin_action(
                tenant_id=tenant_id, actor=actor, action=action, details=details
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ledger publish failed: %s (swallow)", exc)
