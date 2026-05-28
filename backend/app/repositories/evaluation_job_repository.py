"""PostgresEvaluationJobRepository — evaluation_jobs CRUD (ADR-009 §7).

운영 구현. RLS 적용 — 각 tenant는 자기 jobs만 조회. promote 호출 시 platform_admin은
별도 endpoint를 통해 admin engine으로 접근하지 않고 RLS 환경에서 tenant scope로
운영되므로 set_tenant_context를 매번 적용한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.evaluation_job_repository import EvaluationJobRecord

from app.core.rls import set_tenant_context


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class PostgresEvaluationJobRepository:
    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def create(self, job: EvaluationJobRecord) -> None:
        async with self._sf() as session:
            await set_tenant_context(session, job.domain_id)
            await session.execute(
                text(
                    """
                    INSERT INTO evaluation_jobs (
                        job_id, domain_id, dataset_name, status, actor,
                        config_override
                    )
                    VALUES (
                        :job_id, :domain_id, :dataset_name, :status, :actor,
                        CAST(:config_override AS JSONB)
                    )
                    """
                ),
                {
                    "job_id": job.job_id,
                    "domain_id": job.domain_id,
                    "dataset_name": job.dataset_name,
                    "status": job.status,
                    "actor": job.actor,
                    "config_override": _json(job.config_override),
                },
            )
            await session.commit()

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
        set_clauses: list[str] = []
        params: dict[str, Any] = {"job_id": job_id}
        if status is not None:
            set_clauses.append("status = :status")
            params["status"] = status
        if summary is not None:
            set_clauses.append("summary = CAST(:summary AS JSONB)")
            params["summary"] = _json(summary)
        if gate_result is not None:
            set_clauses.append("gate_result = CAST(:gate_result AS JSONB)")
            params["gate_result"] = _json(gate_result)
        if error_message is not None:
            set_clauses.append("error_message = :error_message")
            params["error_message"] = error_message
        if started_at is not None:
            set_clauses.append("started_at = :started_at")
            params["started_at"] = started_at
        if finished_at is not None:
            set_clauses.append("finished_at = :finished_at")
            params["finished_at"] = finished_at
        if promoted_at is not None:
            set_clauses.append("promoted_at = :promoted_at")
            params["promoted_at"] = promoted_at
        if promoted_by is not None:
            set_clauses.append("promoted_by = :promoted_by")
            params["promoted_by"] = promoted_by
        if promotion_target is not None:
            set_clauses.append("promotion_target = :promotion_target")
            params["promotion_target"] = promotion_target
        if promotion_version is not None:
            set_clauses.append("promotion_version = :promotion_version")
            params["promotion_version"] = promotion_version

        if not set_clauses:
            return

        async with self._sf() as session:
            # domain_id를 먼저 조회 — RLS context 적용
            row = (
                await session.execute(
                    text(
                        "SELECT domain_id FROM evaluation_jobs WHERE job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
            ).first()
            if row is None:
                return
            domain_id = str(row[0])
            await set_tenant_context(session, domain_id)
            await session.execute(
                text(
                    f"UPDATE evaluation_jobs SET {', '.join(set_clauses)} "
                    f"WHERE job_id = :job_id"
                ),
                params,
            )
            await session.commit()

    async def get(
        self, *, domain_id: str, job_id: str
    ) -> EvaluationJobRecord | None:
        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            row = (
                await session.execute(
                    text(
                        """
                        SELECT job_id, dataset_name, status, actor, config_override,
                               summary, gate_result, error_message, started_at,
                               finished_at, promoted_at, promoted_by, promotion_target,
                               promotion_version
                          FROM evaluation_jobs
                         WHERE domain_id = :domain_id AND job_id = :job_id
                        """
                    ),
                    {"domain_id": domain_id, "job_id": job_id},
                )
            ).first()
            if row is None:
                return None
            return _row_to_record(domain_id, row)

    async def list_by_tenant(
        self,
        *,
        domain_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EvaluationJobRecord]:
        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT job_id, dataset_name, status, actor, config_override,
                               summary, gate_result, error_message, started_at,
                               finished_at, promoted_at, promoted_by, promotion_target,
                               promotion_version
                          FROM evaluation_jobs
                         WHERE domain_id = :domain_id
                         ORDER BY created_at DESC
                         LIMIT :limit OFFSET :offset
                        """
                    ),
                    {"domain_id": domain_id, "limit": limit, "offset": offset},
                )
            ).all()
            return [_row_to_record(domain_id, r) for r in rows]


def _row_to_record(domain_id: str, r) -> EvaluationJobRecord:
    return EvaluationJobRecord(
        job_id=str(r[0]),
        domain_id=domain_id,
        dataset_name=r[1],
        status=r[2],
        actor=r[3],
        config_override=dict(r[4] or {}),
        summary=dict(r[5] or {}),
        gate_result=dict(r[6] or {}),
        error_message=r[7],
        started_at=r[8],
        finished_at=r[9],
        promoted_at=r[10],
        promoted_by=r[11],
        promotion_target=r[12],
        promotion_version=r[13],
    )
