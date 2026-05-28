"""ArchivalWorker — ADR-012 §3 daily archival job.

운영 흐름:
  1. 모든 active tenant 순회 (또는 단일 tenant 지정)
  2. tenant의 lifecycle.yaml.archival.chunks_archive_days를 읽어 threshold 결정 (default 90)
  3. archiver.find_candidates → archiver.archive → vector_store.set_payload({"archived": true})
  4. tenant_lifecycle_logs에 chunk_archival_executed audit 기록

cron / Celery beat / Kubernetes CronJob에서 매일 자정 호출. CLI는 `python -m
app.services.archival_worker` 형태로 단발 실행 가능.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.vector_store import VectorStore
from rag_core.services.chunks_archiver import (
    ArchivalBatchResult,
    ChunksArchiver,
)

from app.core.rls import set_tenant_context

logger = logging.getLogger(__name__)


# ADR-021 §3 — chunks archival cron 전용 advisory lock id.
# 다른 lock과 겹치지 않는 임의 정수. 변경 금지.
_ARCHIVAL_LOCK_KEY = 7019_0301  # 70190301 (ADR-021 §3 표)


@dataclass
class WorkerSummary:
    tenant_results: list[ArchivalBatchResult] = field(default_factory=list)

    @property
    def total_archived(self) -> int:
        return sum(len(r.archived_chunk_ids) for r in self.tenant_results)


class ArchivalWorker:
    """tenant 단위 archival 실행 + vector store payload 갱신 + audit.

    Args:
        archiver: ChunksArchiver — Postgres 또는 InMemory 구현
        vector_store: VectorStore — set_payload 호출용
        admin_session_factory: BYPASSRLS admin engine (tenants 목록 + audit log)
        clock: datetime factory (테스트에서 freeze 가능)
        threshold_days_provider: domain_id → int (lifecycle.yaml 또는 default 90)
    """

    def __init__(
        self,
        *,
        archiver: ChunksArchiver,
        vector_store: VectorStore,
        admin_session_factory: async_sessionmaker,
        clock=lambda: datetime.now(timezone.utc),
        threshold_days_provider=None,
    ) -> None:
        self._archiver = archiver
        self._vs = vector_store
        self._admin = admin_session_factory
        self._clock = clock
        self._threshold_days_provider = threshold_days_provider or (lambda _tid: 90)

    async def run_for_tenant(self, domain_id: str) -> ArchivalBatchResult:
        days = self._threshold_days_provider(domain_id)
        now = self._clock()
        # date 비교 — UTC 자정 기준
        threshold: date = (now - timedelta(days=days)).date()

        candidates = await self._archiver.find_candidates(
            domain_id=domain_id, valid_until_before=threshold
        )
        if not candidates:
            return ArchivalBatchResult(domain_id=domain_id)

        result = await self._archiver.archive(
            domain_id=domain_id, chunks=candidates
        )

        # Qdrant payload 갱신 — archived=true (검색 필터에서 자동 제외)
        if result.archived_chunk_ids:
            try:
                await self._vs.set_payload(
                    domain_id=domain_id,
                    chunk_ids=result.archived_chunk_ids,
                    payload={"archived": True},
                )
            except Exception as exc:  # noqa: BLE001 — payload 실패가 DB 작업을 되돌릴 수 없음
                logger.error(
                    "vector_store.set_payload failed: tenant=%s err=%s",
                    domain_id,
                    exc,
                )

        await self._audit(
            domain_id=domain_id,
            archived=result.archived_chunk_ids,
            skipped=result.skipped_chunk_ids,
            threshold_days=days,
        )
        return result

    async def run_all_tenants(self) -> WorkerSummary:
        """모든 active tenant 순회. ADR-021 §3 — pg_try_advisory_lock으로
        multi-instance 중복 실행 차단. lock 미획득 시 silent skip.
        """
        # advisory lock 시도 — 다른 instance가 처리 중이면 빈 summary 반환
        async with self._admin() as session:
            lock_row = (
                await session.execute(
                    text("SELECT pg_try_advisory_lock(:key) AS got"),
                    {"key": _ARCHIVAL_LOCK_KEY},
                )
            ).first()
            got_lock = bool(lock_row[0]) if lock_row else False

        if not got_lock:
            logger.info("archival_worker: advisory lock busy — skip")
            return WorkerSummary()

        try:
            domain_ids = await self._list_active_domain_ids()
            summary = WorkerSummary()
            for tid in domain_ids:
                try:
                    summary.tenant_results.append(await self.run_for_tenant(tid))
                except Exception:  # noqa: BLE001 — tenant 1개 실패가 나머지를 막지 않음
                    logger.exception("archival worker failed for tenant=%s", tid)
            return summary
        finally:
            async with self._admin() as unlock_session:
                await unlock_session.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _ARCHIVAL_LOCK_KEY},
                )

    async def _list_active_domain_ids(self) -> list[str]:
        async with self._admin() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT domain_id FROM tenants WHERE status = 'active' ORDER BY domain_id"
                    )
                )
            ).all()
            return [str(r[0]) for r in rows]

    async def _audit(
        self,
        *,
        domain_id: str,
        archived: list[str],
        skipped: list[str],
        threshold_days: int,
    ) -> None:
        async with self._admin() as session:
            await set_tenant_context(session, domain_id)
            await session.execute(
                text(
                    """
                    INSERT INTO tenant_lifecycle_logs (
                        domain_id, action, from_state, to_state, actor, reason, details
                    )
                    VALUES (
                        :domain_id, 'chunk_archival_executed', NULL, NULL,
                        'archival_worker', 'valid_until_threshold_reached',
                        CAST(:details AS JSONB)
                    )
                    """
                ),
                {
                    "domain_id": domain_id,
                    "details": json.dumps(
                        {
                            "archived_count": len(archived),
                            "skipped_count": len(skipped),
                            "threshold_days": threshold_days,
                        },
                        ensure_ascii=False,
                    ),
                },
            )
            await session.commit()


# --------------------------------------------------------------------------- #
# CLI entry — python -m app.services.archival_worker
# --------------------------------------------------------------------------- #


async def _main() -> None:
    """운영 cron 진입점. production backend의 archiver/vector_store/admin engine을 구성.

    inmemory 모드에서는 호출 불가 (실 DB 없음).
    """
    from qdrant_client import AsyncQdrantClient
    from rag_core.clients.qdrant_store import QdrantVectorStore

    from app.core.config import get_settings
    from app.core.db import AdminSessionLocal, AppSessionLocal
    from app.services.chunks_archiver import PostgresChunksArchiver

    settings = get_settings()
    qdrant = AsyncQdrantClient(
        url=f"http://{settings.qdrant_host}:{settings.qdrant_port}",
        api_key=settings.qdrant_api_key,
        prefer_grpc=False,
    )
    archiver = PostgresChunksArchiver(app_session_factory=AppSessionLocal)
    vs = QdrantVectorStore(client=qdrant)
    worker = ArchivalWorker(
        archiver=archiver,
        vector_store=vs,
        admin_session_factory=AdminSessionLocal,
    )
    summary = await worker.run_all_tenants()
    logger.info(
        "archival_worker done: tenants=%d archived_total=%d",
        len(summary.tenant_results),
        summary.total_archived,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
