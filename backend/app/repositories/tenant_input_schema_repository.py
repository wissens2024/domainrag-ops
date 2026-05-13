"""PostgresTenantInputSchemaRepository — tenant_input_schemas CRUD (ADR-015 + ADR-017 §15).

UNIQUE(tenant_id, schema_version) → race 시 IntegrityError → SchemaVersionConflictError.
status 전이는 두 SQL 한 트랜잭션:
  UPDATE old active → deprecated
  INSERT new active

base_version 검증: current active를 SELECT FOR UPDATE로 잡고 비교.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.tenant_input_schema_repository import (
    SchemaVersionConflictError,
    TenantInputSchemaRecord,
)

from app.core.rls import set_tenant_context


class PostgresTenantInputSchemaRepository:
    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def get_active(
        self, *, tenant_id: str
    ) -> TenantInputSchemaRecord | None:
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id, schema_version, status,
                               schema_yaml, ui_schema_yaml,
                               created_at, deprecated_at
                          FROM tenant_input_schemas
                         WHERE tenant_id = :tenant_id AND status = 'active'
                         ORDER BY schema_version DESC
                         LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).first()
        return _row_to_record(tenant_id, row) if row else None

    async def list_history(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TenantInputSchemaRecord]:
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT id, schema_version, status,
                               schema_yaml, ui_schema_yaml,
                               created_at, deprecated_at
                          FROM tenant_input_schemas
                         WHERE tenant_id = :tenant_id
                         ORDER BY schema_version DESC
                         LIMIT :limit OFFSET :offset
                        """
                    ),
                    {"tenant_id": tenant_id, "limit": limit, "offset": offset},
                )
            ).all()
        return [_row_to_record(tenant_id, r) for r in rows]

    async def insert_new_active(
        self,
        *,
        tenant_id: str,
        base_version: int | None,
        schema_yaml: dict[str, Any],
        ui_schema_yaml: dict[str, Any],
    ) -> TenantInputSchemaRecord:
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)

            # 1. current active를 FOR UPDATE — 동시 PUT race 차단
            current_row = (
                await session.execute(
                    text(
                        """
                        SELECT id, schema_version
                          FROM tenant_input_schemas
                         WHERE tenant_id = :tenant_id AND status = 'active'
                         ORDER BY schema_version DESC
                         LIMIT 1
                         FOR UPDATE
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).first()
            current_version = int(current_row[1]) if current_row else None
            if current_version != base_version:
                await session.rollback()
                raise SchemaVersionConflictError(
                    current=current_version, base=base_version
                )

            # 2. 기존 active → deprecated
            if current_row is not None:
                await session.execute(
                    text(
                        """
                        UPDATE tenant_input_schemas
                           SET status = 'deprecated', deprecated_at = NOW()
                         WHERE id = :id
                        """
                    ),
                    {"id": current_row[0]},
                )

            # 3. 신규 active 삽입
            new_version = (current_version or 0) + 1
            try:
                inserted_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO tenant_input_schemas (
                                tenant_id, schema_version, status,
                                schema_yaml, ui_schema_yaml
                            )
                            VALUES (
                                :tenant_id, :schema_version, 'active',
                                CAST(:schema_yaml AS JSONB),
                                CAST(:ui_schema_yaml AS JSONB)
                            )
                            RETURNING id, created_at
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "schema_version": new_version,
                            "schema_yaml": json.dumps(
                                schema_yaml, ensure_ascii=False, default=str
                            ),
                            "ui_schema_yaml": json.dumps(
                                ui_schema_yaml or {}, ensure_ascii=False, default=str
                            ),
                        },
                    )
                ).first()
            except IntegrityError as exc:
                await session.rollback()
                raise SchemaVersionConflictError(
                    current=new_version - 1, base=base_version
                ) from exc

            await session.commit()

        return TenantInputSchemaRecord(
            schema_id=str(inserted_id[0]),
            tenant_id=tenant_id,
            schema_version=new_version,
            status="active",
            schema_yaml=dict(schema_yaml or {}),
            ui_schema_yaml=dict(ui_schema_yaml or {}),
            created_at=inserted_id[1] if isinstance(inserted_id[1], datetime) else None,
        )


def _row_to_record(tenant_id: str, row) -> TenantInputSchemaRecord:
    return TenantInputSchemaRecord(
        schema_id=str(row[0]),
        tenant_id=tenant_id,
        schema_version=int(row[1]),
        status=row[2],
        schema_yaml=dict(row[3] or {}),
        ui_schema_yaml=dict(row[4] or {}),
        created_at=row[5] if isinstance(row[5], datetime) else None,
        deprecated_at=row[6] if isinstance(row[6], datetime) else None,
    )
