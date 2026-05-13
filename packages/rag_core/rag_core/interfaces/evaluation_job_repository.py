"""EvaluationJobRepository — ADR-009 §7 / ADR-017 §16.

평가 실행 비동기 job lifecycle 추적. Protocol과 dataclass는 rag_core, 운영 구현은 backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class EvaluationJobRecord:
    job_id: str
    tenant_id: str
    dataset_name: str
    status: str = "pending"  # pending | running | completed | failed | promoted
    actor: str | None = None
    config_override: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    gate_result: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    promoted_at: datetime | None = None
    promoted_by: str | None = None
    promotion_target: str | None = None
    promotion_version: str | None = None


class EvaluationJobRepository(Protocol):
    async def create(self, job: EvaluationJobRecord) -> None: ...

    async def update(
        self,
        *,
        job_id: str,
        status: str | None = None,
        summary: dict[str, Any] | None = None,
        gate_result: dict[str, Any] | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        promoted_at: datetime | None = None,
        promoted_by: str | None = None,
        promotion_target: str | None = None,
        promotion_version: str | None = None,
    ) -> None: ...

    async def get(self, *, tenant_id: str, job_id: str) -> EvaluationJobRecord | None: ...

    async def list_by_tenant(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EvaluationJobRecord]: ...


# --------------------------------------------------------------------------- #
# InMemory variant — dev / tests
# --------------------------------------------------------------------------- #


class InMemoryEvaluationJobRepository:
    def __init__(self) -> None:
        self._jobs: dict[tuple[str, str], EvaluationJobRecord] = {}

    async def create(self, job: EvaluationJobRecord) -> None:
        key = (job.tenant_id, job.job_id)
        if key in self._jobs:
            raise ValueError(f"job already exists: {job.job_id}")
        self._jobs[key] = job

    async def update(
        self,
        *,
        job_id: str,
        status: str | None = None,
        summary: dict[str, Any] | None = None,
        gate_result: dict[str, Any] | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        promoted_at: datetime | None = None,
        promoted_by: str | None = None,
        promotion_target: str | None = None,
        promotion_version: str | None = None,
    ) -> None:
        record = self._find_by_job_id(job_id)
        if record is None:
            return
        if status is not None:
            record.status = status
        if summary is not None:
            record.summary = dict(summary)
        if gate_result is not None:
            record.gate_result = dict(gate_result)
        if error_message is not None:
            record.error_message = error_message
        if started_at is not None:
            record.started_at = started_at
        if finished_at is not None:
            record.finished_at = finished_at
        if promoted_at is not None:
            record.promoted_at = promoted_at
        if promoted_by is not None:
            record.promoted_by = promoted_by
        if promotion_target is not None:
            record.promotion_target = promotion_target
        if promotion_version is not None:
            record.promotion_version = promotion_version

    async def get(
        self, *, tenant_id: str, job_id: str
    ) -> EvaluationJobRecord | None:
        return self._jobs.get((tenant_id, job_id))

    async def list_by_tenant(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EvaluationJobRecord]:
        out = [j for k, j in self._jobs.items() if k[0] == tenant_id]
        out.reverse()  # 최신순 (등록 순의 역)
        return out[offset : offset + limit]

    def _find_by_job_id(self, job_id: str) -> EvaluationJobRecord | None:
        for (_tid, jid), record in self._jobs.items():
            if jid == job_id:
                return record
        return None
