"""PostgresAssessmentLogger — ADR-014 §10 assessment_logs INSERT."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.services.assessment_logger import AssessmentLogPayload

from app.core.rls import set_tenant_context


class PostgresAssessmentLogger:
    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def write(self, payload: AssessmentLogPayload) -> None:
        async with self._sf() as session:
            await set_tenant_context(session, payload.domain_id)
            await session.execute(
                text(
                    """
                    INSERT INTO assessment_logs (
                        domain_id, request_id, action, criteria,
                        result_summary, validator_summary, similarity_results,
                        latency_ms, actor
                    )
                    VALUES (
                        :domain_id, :request_id, :action,
                        CAST(:criteria AS JSONB),
                        CAST(:result_summary AS JSONB),
                        CAST(:validator_summary AS JSONB),
                        CAST(:similarity_results AS JSONB),
                        :latency_ms, :actor
                    )
                    """
                ),
                {
                    "domain_id": payload.domain_id,
                    "request_id": payload.request_id,
                    "action": payload.action,
                    "criteria": json.dumps(payload.criteria, ensure_ascii=False, default=str),
                    "result_summary": json.dumps(payload.result_summary, ensure_ascii=False, default=str),
                    "validator_summary": json.dumps(payload.validator_summary, ensure_ascii=False, default=str),
                    "similarity_results": json.dumps(payload.similarity_results, ensure_ascii=False, default=str),
                    "latency_ms": payload.latency_ms,
                    "actor": payload.actor,
                },
            )
            await session.commit()
