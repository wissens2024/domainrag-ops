"""TenantInputSchemaRepository — ADR-015 + ADR-017 §15 schema CRUD Protocol.

tenant_input_schemas 테이블:
  (id, domain_id, schema_version INT, status active|deprecated, schema_yaml JSONB,
   ui_schema_yaml JSONB, created_at, deprecated_at, UNIQUE(domain_id, schema_version))

Status 머신:
  - 새 PUT 시 기존 active row를 deprecated로 전이 + 새 row status='active' 삽입
  - schema_version은 단조 증가 (last_active + 1). Race 시 IntegrityError → SchemaVersionConflictError.

Optimistic lock (Y8):
  - PUT body의 `base_version`이 현재 active schema_version과 일치해야 한다.
  - 불일치 시 SchemaVersionConflictError (409 변환). 운영자가 merge 후 재시도.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class TenantInputSchemaRecord:
    schema_id: str
    domain_id: str
    schema_version: int
    status: str  # 'active' | 'deprecated'
    schema_yaml: dict[str, Any] = field(default_factory=dict)
    ui_schema_yaml: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    deprecated_at: datetime | None = None


class SchemaVersionConflictError(Exception):
    """base_version이 현재 active와 다르거나 동시 INSERT race 시.

    409 변환 + 클라이언트가 latest를 다시 GET해 merge.
    """

    def __init__(self, *, current: int | None, base: int | None) -> None:
        super().__init__(f"schema version conflict: current={current} base={base}")
        self.current = current
        self.base = base


class TenantInputSchemaRepository(Protocol):
    async def get_active(self, *, domain_id: str) -> TenantInputSchemaRecord | None:
        """현재 active schema 1건. 없으면 None (tenant 미등록)."""
        ...

    async def list_history(
        self,
        *,
        domain_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TenantInputSchemaRecord]:
        """최신 schema_version 역순. status active+deprecated 모두."""
        ...

    async def insert_new_active(
        self,
        *,
        domain_id: str,
        base_version: int | None,
        schema_yaml: dict[str, Any],
        ui_schema_yaml: dict[str, Any],
    ) -> TenantInputSchemaRecord:
        """현재 active version == base_version 검증 + 기존 active deprecate + 신규 active 삽입.

        raises:
            SchemaVersionConflictError — base_version 불일치 또는 동시 INSERT race.
        """
        ...


# --------------------------------------------------------------------------- #
# InMemory — dev / tests
# --------------------------------------------------------------------------- #


class InMemoryTenantInputSchemaRepository:
    def __init__(self) -> None:
        # domain_id → list[TenantInputSchemaRecord] (append 순)
        self._records: dict[str, list[TenantInputSchemaRecord]] = {}
        self._id_counter = 0

    async def get_active(
        self, *, domain_id: str
    ) -> TenantInputSchemaRecord | None:
        rows = self._records.get(domain_id) or []
        for r in reversed(rows):
            if r.status == "active":
                return r
        return None

    async def list_history(
        self,
        *,
        domain_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TenantInputSchemaRecord]:
        rows = list(self._records.get(domain_id) or [])
        rows.sort(key=lambda r: r.schema_version, reverse=True)
        return rows[offset : offset + limit]

    async def insert_new_active(
        self,
        *,
        domain_id: str,
        base_version: int | None,
        schema_yaml: dict[str, Any],
        ui_schema_yaml: dict[str, Any],
    ) -> TenantInputSchemaRecord:
        current = await self.get_active(domain_id=domain_id)
        current_version = current.schema_version if current else None
        if current_version != base_version:
            raise SchemaVersionConflictError(
                current=current_version, base=base_version
            )
        # deprecate 기존 active
        if current is not None:
            current.status = "deprecated"
            current.deprecated_at = datetime.utcnow()
        new_version = (current_version or 0) + 1
        self._id_counter += 1
        record = TenantInputSchemaRecord(
            schema_id=f"schema-{self._id_counter:08d}",
            domain_id=domain_id,
            schema_version=new_version,
            status="active",
            schema_yaml=dict(schema_yaml or {}),
            ui_schema_yaml=dict(ui_schema_yaml or {}),
            created_at=datetime.utcnow(),
        )
        self._records.setdefault(domain_id, []).append(record)
        return record
