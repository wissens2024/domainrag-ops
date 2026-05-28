"""PostgresDashboardAnalytics — admin 대시보드 집계 (ADR-017 §10).

RLS context 적용 후 documents/chunks/indexing_jobs/chat_logs 4테이블에서 12개
메트릭을 산출. today는 KST(Asia/Seoul, Y4) 자정 기준.

쿼리 7개:
  1. documents 전체 + today
  2. chunks (archived 제외)
  3. indexing_jobs (today completed/failed)
  4. chat_logs scalar: count / avg_latency / no_citation / feedback bad·total
  5. citation_type_distribution
  6. fallback_distribution
  7. routing_distribution

routing_distribution key: `selected_model + ('_lora' if selected_lora else '_no_lora')`.
shared_llm은 단일 'shared_llm' (lora 비적용).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.dashboard_analytics import (
    KST,
    DashboardSnapshot,
    _kst_today_start,
)

from app.core.rls import set_tenant_context


class PostgresDashboardAnalytics:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        clock=lambda: datetime.now(KST),
    ) -> None:
        self._sf = session_factory
        self._clock = clock

    async def get_snapshot(self, *, domain_id: str) -> DashboardSnapshot:
        today_start = _kst_today_start(self._clock())
        snap = DashboardSnapshot()

        async with self._sf() as session:
            await set_tenant_context(session, domain_id)

            # 1. documents
            doc_row = (
                await session.execute(
                    text(
                        """
                        SELECT
                          COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE created_at >= :today_start) AS today
                        FROM documents
                        """
                    ),
                    {"today_start": today_start},
                )
            ).first()
            snap.total_documents = int(doc_row[0] or 0)
            snap.uploaded_today = int(doc_row[1] or 0)

            # 2. chunks (archived 제외)
            chunk_row = (
                await session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM chunks WHERE archived_at IS NULL
                        """
                    )
                )
            ).first()
            snap.total_chunks = int(chunk_row[0] or 0)

            # 3. indexing_jobs today (status별)
            job_row = (
                await session.execute(
                    text(
                        """
                        SELECT
                          COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                          COUNT(*) FILTER (WHERE status = 'failed') AS failed
                        FROM indexing_jobs
                        WHERE created_at >= :today_start
                        """
                    ),
                    {"today_start": today_start},
                )
            ).first()
            snap.indexing_completed_today = int(job_row[0] or 0)
            snap.indexing_failed_today = int(job_row[1] or 0)

            # 4. chat_logs scalar today
            chat_row = (
                await session.execute(
                    text(
                        """
                        SELECT
                          COUNT(*) AS total,
                          AVG(latency_ms) AS avg_latency,
                          COUNT(*) FILTER (
                            WHERE jsonb_array_length(citations) = 0
                          ) AS no_cite,
                          COUNT(*) FILTER (WHERE feedback = 'bad') AS bad
                        FROM chat_logs
                        WHERE created_at >= :today_start
                        """
                    ),
                    {"today_start": today_start},
                )
            ).first()
            total = int(chat_row[0] or 0)
            snap.questions_today = total
            snap.avg_latency_ms = float(chat_row[1] or 0.0)
            snap.answers_without_citation = int(chat_row[2] or 0)
            bad = int(chat_row[3] or 0)
            snap.negative_feedback_rate = (bad / total) if total > 0 else 0.0

            # 5. citation_type_distribution — row 단위 distinct
            citation_rows = (
                await session.execute(
                    text(
                        """
                        SELECT ct, COUNT(DISTINCT (id, created_at)) AS cnt
                          FROM chat_logs cl,
                               LATERAL jsonb_array_elements_text(cl.citation_types) ct
                         WHERE cl.created_at >= :today_start
                         GROUP BY ct
                        """
                    ),
                    {"today_start": today_start},
                )
            ).all()
            snap.citation_type_distribution = {
                r[0]: int(r[1] or 0) for r in citation_rows
            }

            # 6. fallback_distribution
            fb_rows = (
                await session.execute(
                    text(
                        """
                        SELECT fallback_reason, COUNT(*) AS cnt
                          FROM chat_logs
                         WHERE created_at >= :today_start
                           AND fallback_reason IS NOT NULL
                         GROUP BY fallback_reason
                        """
                    ),
                    {"today_start": today_start},
                )
            ).all()
            snap.fallback_distribution = {
                r[0]: int(r[1] or 0) for r in fb_rows
            }

            # 7. routing_distribution
            routing_rows = (
                await session.execute(
                    text(
                        """
                        SELECT
                          CASE
                            WHEN routing_decision->>'selected_model' IS NULL THEN NULL
                            WHEN routing_decision->>'selected_model' = 'shared_llm'
                                 THEN 'shared_llm'
                            WHEN COALESCE(routing_decision->>'selected_lora', '') = ''
                                 THEN routing_decision->>'selected_model' || '_no_lora'
                            ELSE routing_decision->>'selected_model' || '_lora'
                          END AS key,
                          COUNT(*) AS cnt
                        FROM chat_logs
                        WHERE created_at >= :today_start
                        GROUP BY key
                        """
                    ),
                    {"today_start": today_start},
                )
            ).all()
            snap.routing_distribution = {
                r[0]: int(r[1] or 0) for r in routing_rows if r[0]
            }

        return snap
