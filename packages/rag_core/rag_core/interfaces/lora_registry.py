"""LoRARegistry — ADR-017 §14 + ADR-013 LoRA adapter lifecycle Protocol.

상태 머신 (ADR-013 §3·§4 정합):
  registered → active → retired
                  ↓        ↑
                retired → registered? (no — retired는 종료 상태)

규칙:
  - active 상태에서 DELETE 불가 (LoRADeleteForbiddenError)
  - registered → active만 activated_at 기록
  - active → retired만 retired_at 기록
  - delete는 상태 무관 row 제거 — active만 차단
  - upload는 adapter_id 충돌 시 LoRAConflictError (adapter_id는 UNIQUE)

본 Protocol은 메타데이터 CRUD만 다룬다. weights 저장(KeyHub)·vLLM hot-swap은
backend의 별도 adapter — 본 작업 scope 밖.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class AdapterRecord:
    adapter_id: str
    tenant_id: str
    version: str | None = None
    base_model: str | None = None
    keyhub_secret_ref: str | None = None
    status: str = "registered"  # registered | active | retired
    training_metadata: dict[str, Any] = field(default_factory=dict)
    registered_at: datetime | None = None
    activated_at: datetime | None = None
    retired_at: datetime | None = None


class LoRAConflictError(Exception):
    """adapter_id가 이미 등록됨 (UNIQUE 위반)."""

    def __init__(self, adapter_id: str) -> None:
        super().__init__(f"adapter_id already exists: {adapter_id}")
        self.adapter_id = adapter_id


class LoRANotFoundError(Exception):
    """adapter_id로 등록된 row 없음."""

    def __init__(self, adapter_id: str) -> None:
        super().__init__(f"adapter not found: {adapter_id}")
        self.adapter_id = adapter_id


class LoRADeleteForbiddenError(Exception):
    """active 상태인 adapter는 DELETE 불가 — retire 후 삭제 필요."""

    def __init__(self, adapter_id: str) -> None:
        super().__init__(f"adapter active, cannot delete: {adapter_id}")
        self.adapter_id = adapter_id


class LoRAInvalidTransitionError(Exception):
    """허용되지 않는 status 전이 (예: retired → active)."""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"invalid transition: {current} → {target}")
        self.current = current
        self.target = target


class LoRARegistry(Protocol):
    async def list_by_tenant(
        self, *, tenant_id: str, status: str | None = None
    ) -> list[AdapterRecord]: ...

    async def get(
        self, *, tenant_id: str, adapter_id: str
    ) -> AdapterRecord | None: ...

    async def upload(self, record: AdapterRecord) -> AdapterRecord:
        """adapter_id UNIQUE 위반 시 LoRAConflictError."""
        ...

    async def activate(
        self, *, tenant_id: str, adapter_id: str
    ) -> AdapterRecord:
        """registered → active. retired→active는 금지(LoRAInvalidTransitionError)."""
        ...

    async def retire(
        self, *, tenant_id: str, adapter_id: str
    ) -> AdapterRecord:
        """active → retired. registered→retired도 허용 (uploaded but never active)."""
        ...

    async def delete(
        self, *, tenant_id: str, adapter_id: str
    ) -> int:
        """row 삭제. active 상태면 LoRADeleteForbiddenError. 미발견은 0."""
        ...


# --------------------------------------------------------------------------- #
# InMemory — dev / tests
# --------------------------------------------------------------------------- #


class InMemoryLoRARegistry:
    def __init__(self) -> None:
        self._records: dict[str, AdapterRecord] = {}  # adapter_id 전역 UNIQUE

    async def list_by_tenant(
        self, *, tenant_id: str, status: str | None = None
    ) -> list[AdapterRecord]:
        out = [r for r in self._records.values() if r.tenant_id == tenant_id]
        if status is not None:
            out = [r for r in out if r.status == status]
        out.sort(key=lambda r: (r.registered_at or datetime.min), reverse=True)
        return out

    async def get(
        self, *, tenant_id: str, adapter_id: str
    ) -> AdapterRecord | None:
        rec = self._records.get(adapter_id)
        if rec is None or rec.tenant_id != tenant_id:
            return None
        return rec

    async def upload(self, record: AdapterRecord) -> AdapterRecord:
        if record.adapter_id in self._records:
            raise LoRAConflictError(record.adapter_id)
        now = datetime.utcnow()
        rec = AdapterRecord(
            adapter_id=record.adapter_id,
            tenant_id=record.tenant_id,
            version=record.version,
            base_model=record.base_model,
            keyhub_secret_ref=record.keyhub_secret_ref,
            status="registered",
            training_metadata=dict(record.training_metadata or {}),
            registered_at=now,
        )
        self._records[rec.adapter_id] = rec
        return rec

    async def activate(
        self, *, tenant_id: str, adapter_id: str
    ) -> AdapterRecord:
        rec = await self.get(tenant_id=tenant_id, adapter_id=adapter_id)
        if rec is None:
            raise LoRANotFoundError(adapter_id)
        if rec.status == "retired":
            raise LoRAInvalidTransitionError(rec.status, "active")
        if rec.status != "active":
            rec.status = "active"
            rec.activated_at = datetime.utcnow()
        return rec

    async def retire(
        self, *, tenant_id: str, adapter_id: str
    ) -> AdapterRecord:
        rec = await self.get(tenant_id=tenant_id, adapter_id=adapter_id)
        if rec is None:
            raise LoRANotFoundError(adapter_id)
        if rec.status == "retired":
            return rec
        rec.status = "retired"
        rec.retired_at = datetime.utcnow()
        return rec

    async def delete(
        self, *, tenant_id: str, adapter_id: str
    ) -> int:
        rec = await self.get(tenant_id=tenant_id, adapter_id=adapter_id)
        if rec is None:
            return 0
        if rec.status == "active":
            raise LoRADeleteForbiddenError(adapter_id)
        self._records.pop(adapter_id, None)
        return 1
