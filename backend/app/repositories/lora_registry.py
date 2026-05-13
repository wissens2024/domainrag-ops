"""PostgresLoRARegistry — adapter_registry CRUD (ADR-017 §14 + ADR-013).

RLS context 적용. adapter_id는 전역 UNIQUE이므로 RLS 외에도 tenant_id 명시.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.lora_registry import (
    AdapterRecord,
    LoRAConflictError,
    LoRADeleteForbiddenError,
    LoRAInvalidTransitionError,
    LoRANotFoundError,
)

from app.core.rls import set_tenant_context


class PostgresLoRARegistry:
    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def list_by_tenant(
        self, *, tenant_id: str, status: str | None = None
    ) -> list[AdapterRecord]:
        params: dict = {"tenant_id": tenant_id, "status": status}
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT adapter_id, version, base_model, keyhub_secret_ref,
                               status, training_metadata, registered_at,
                               activated_at, retired_at
                          FROM adapter_registry
                         WHERE tenant_id = :tenant_id
                           AND (status = :status OR :status IS NULL)
                         ORDER BY registered_at DESC
                        """
                    ),
                    params,
                )
            ).all()
        return [_row_to_record(tenant_id, r) for r in rows]

    async def get(
        self, *, tenant_id: str, adapter_id: str
    ) -> AdapterRecord | None:
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            row = (
                await session.execute(
                    text(
                        """
                        SELECT adapter_id, version, base_model, keyhub_secret_ref,
                               status, training_metadata, registered_at,
                               activated_at, retired_at
                          FROM adapter_registry
                         WHERE tenant_id = :tenant_id AND adapter_id = :adapter_id
                        """
                    ),
                    {"tenant_id": tenant_id, "adapter_id": adapter_id},
                )
            ).first()
        return _row_to_record(tenant_id, row) if row else None

    async def upload(self, record: AdapterRecord) -> AdapterRecord:
        async with self._sf() as session:
            await set_tenant_context(session, record.tenant_id)
            try:
                await session.execute(
                    text(
                        """
                        INSERT INTO adapter_registry (
                            tenant_id, adapter_id, version, base_model,
                            keyhub_secret_ref, status, training_metadata
                        )
                        VALUES (
                            :tenant_id, :adapter_id, :version, :base_model,
                            :keyhub_secret_ref, 'registered',
                            CAST(:training_metadata AS JSONB)
                        )
                        """
                    ),
                    {
                        "tenant_id": record.tenant_id,
                        "adapter_id": record.adapter_id,
                        "version": record.version,
                        "base_model": record.base_model,
                        "keyhub_secret_ref": record.keyhub_secret_ref,
                        "training_metadata": json.dumps(
                            record.training_metadata or {},
                            ensure_ascii=False, default=str,
                        ),
                    },
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise LoRAConflictError(record.adapter_id) from exc
        loaded = await self.get(
            tenant_id=record.tenant_id, adapter_id=record.adapter_id,
        )
        assert loaded is not None
        return loaded

    async def activate(
        self, *, tenant_id: str, adapter_id: str
    ) -> AdapterRecord:
        rec = await self.get(tenant_id=tenant_id, adapter_id=adapter_id)
        if rec is None:
            raise LoRANotFoundError(adapter_id)
        if rec.status == "retired":
            raise LoRAInvalidTransitionError(rec.status, "active")
        if rec.status == "active":
            return rec
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            await session.execute(
                text(
                    """
                    UPDATE adapter_registry
                       SET status = 'active', activated_at = NOW()
                     WHERE tenant_id = :tenant_id AND adapter_id = :adapter_id
                    """
                ),
                {"tenant_id": tenant_id, "adapter_id": adapter_id},
            )
            await session.commit()
        loaded = await self.get(tenant_id=tenant_id, adapter_id=adapter_id)
        assert loaded is not None
        return loaded

    async def retire(
        self, *, tenant_id: str, adapter_id: str
    ) -> AdapterRecord:
        rec = await self.get(tenant_id=tenant_id, adapter_id=adapter_id)
        if rec is None:
            raise LoRANotFoundError(adapter_id)
        if rec.status == "retired":
            return rec
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            await session.execute(
                text(
                    """
                    UPDATE adapter_registry
                       SET status = 'retired', retired_at = NOW()
                     WHERE tenant_id = :tenant_id AND adapter_id = :adapter_id
                    """
                ),
                {"tenant_id": tenant_id, "adapter_id": adapter_id},
            )
            await session.commit()
        loaded = await self.get(tenant_id=tenant_id, adapter_id=adapter_id)
        assert loaded is not None
        return loaded

    async def delete(
        self, *, tenant_id: str, adapter_id: str
    ) -> int:
        rec = await self.get(tenant_id=tenant_id, adapter_id=adapter_id)
        if rec is None:
            return 0
        if rec.status == "active":
            raise LoRADeleteForbiddenError(adapter_id)
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            result = await session.execute(
                text(
                    """
                    DELETE FROM adapter_registry
                     WHERE tenant_id = :tenant_id AND adapter_id = :adapter_id
                    """
                ),
                {"tenant_id": tenant_id, "adapter_id": adapter_id},
            )
            await session.commit()
        return result.rowcount or 0


def _row_to_record(tenant_id: str, row) -> AdapterRecord:
    return AdapterRecord(
        adapter_id=str(row[0]),
        tenant_id=tenant_id,
        version=row[1],
        base_model=row[2],
        keyhub_secret_ref=row[3],
        status=row[4],
        training_metadata=dict(row[5] or {}),
        registered_at=row[6] if isinstance(row[6], datetime) else None,
        activated_at=row[7] if isinstance(row[7], datetime) else None,
        retired_at=row[8] if isinstance(row[8], datetime) else None,
    )
