"""PostgresCitationDistributionAnalytics — date_trunc 시계열 집계 (ADR-017 §9).

쿼리:
  SELECT date_trunc(:gran, created_at) AS bucket, ct, COUNT(DISTINCT id) AS cnt
    FROM chat_logs cl, LATERAL jsonb_array_elements_text(cl.citation_types) ct
   WHERE domain_id = :tid
     AND (created_at >= :from_date OR :from_date IS NULL)
     AND (created_at <  :to_date OR :to_date IS NULL)
   GROUP BY bucket, ct
   ORDER BY bucket ASC, ct;

JSONB array가 빈 chat_logs는 LATERAL 결과에서 빠지므로 별도 total_messages를
COUNT(*) 1쿼리로 산출. RLS context 적용.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.citation_distribution import (
    CitationDistributionBucket,
    CitationDistributionResult,
)

from app.core.rls import set_tenant_context


class PostgresCitationDistributionAnalytics:
    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def distribution(
        self,
        *,
        domain_id: str,
        from_date: datetime | None,
        to_date: datetime | None,
        group_by: Literal["day", "hour"],
    ) -> CitationDistributionResult:
        params: dict = {
            "domain_id": domain_id,
            "from_date": from_date,
            "to_date": to_date,
            "gran": group_by,
        }

        async with self._sf() as session:
            await set_tenant_context(session, domain_id)

            total_row = (
                await session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM chat_logs
                         WHERE (created_at >= :from_date OR CAST(:from_date AS TIMESTAMPTZ) IS NULL)
                           AND (created_at <  :to_date   OR CAST(:to_date   AS TIMESTAMPTZ) IS NULL)
                        """
                    ),
                    params,
                )
            ).first()
            total_messages = int(total_row[0]) if total_row else 0

            rows = (
                await session.execute(
                    text(
                        """
                        SELECT date_trunc(:gran, cl.created_at) AS bucket, ct,
                               COUNT(DISTINCT (cl.id, cl.created_at)) AS cnt
                          FROM chat_logs cl,
                               LATERAL jsonb_array_elements_text(cl.citation_types) ct
                         WHERE (cl.created_at >= :from_date OR CAST(:from_date AS TIMESTAMPTZ) IS NULL)
                           AND (cl.created_at <  :to_date   OR CAST(:to_date   AS TIMESTAMPTZ) IS NULL)
                         GROUP BY bucket, ct
                         ORDER BY bucket ASC, ct ASC
                        """
                    ),
                    params,
                )
            ).all()

        # bucket → counts dict 으로 집계
        bucket_map: dict[datetime, dict[str, int]] = {}
        for r in rows:
            bucket_ts = r[0]
            ct = r[1]
            cnt = int(r[2] or 0)
            bucket_map.setdefault(bucket_ts, {})[ct] = cnt

        buckets = [
            CitationDistributionBucket(
                bucket=ts.isoformat() if ts else "unknown",
                counts=counts,
            )
            for ts, counts in sorted(bucket_map.items(), key=lambda kv: kv[0])
        ]

        return CitationDistributionResult(
            granularity=group_by,
            buckets=buckets,
            total_messages=total_messages,
            from_date=from_date,
            to_date=to_date,
        )
