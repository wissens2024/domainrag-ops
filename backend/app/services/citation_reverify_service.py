"""CitationReverifyService — ADR-017 §9 reverify endpoint orchestrator.

흐름:
  1. ChatLogReader로 date range내 records 페이지 단위 조회 (limit 100)
  2. CitationReverifier로 citation 재계산
  3. ChatLogCitationUpdater로 chat_logs.citations + verifier_metrics UPDATE (RLS)
  4. tenant_lifecycle_logs `citation_inspector_reverified` audit

본 서비스는 ChatLogReader (rag_core) + CitationReverifier (rag_core) + ChatLog
write 책임을 묶는 backend 전용 oracle. embedder는 RAGService deps와 공유.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.chat_log_reader import (
    ChatLogListFilters,
    ChatLogReader,
)
from rag_core.interfaces.embedder import Embedder
from rag_core.services.citation_reverifier import (
    CitationReverifier,
    ReverifyThresholds,
    now_utc_iso,
)

from app.core.rls import set_tenant_context

logger = structlog.get_logger(__name__)


@dataclass
class ReverifyJobSummary:
    tenant_id: str
    from_date: datetime | None
    to_date: datetime | None
    scanned: int = 0
    updated: int = 0
    skipped: int = 0
    upgraded: int = 0
    downgraded: int = 0
    avg_similarity_before: float | None = None
    avg_similarity_after: float | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)


class ChatLogCitationUpdater:
    """chat_logs.citations + verifier_metrics UPDATE — RLS 적용."""

    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def update(
        self,
        *,
        tenant_id: str,
        request_id: str,
        citations: list[dict[str, Any]],
        verifier_metrics: dict[str, Any],
    ) -> int:
        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            result = await session.execute(
                text(
                    """
                    UPDATE chat_logs
                       SET citations = CAST(:citations AS JSONB),
                           verifier_metrics = CAST(:verifier_metrics AS JSONB)
                     WHERE tenant_id = :tenant_id
                       AND request_id = :request_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "request_id": request_id,
                    "citations": json.dumps(citations, ensure_ascii=False, default=str),
                    "verifier_metrics": json.dumps(
                        verifier_metrics, ensure_ascii=False, default=str
                    ),
                },
            )
            await session.commit()
            return result.rowcount or 0


class InMemoryChatLogCitationUpdater:
    """InMemoryChatLogWriter.records 위에서 동작 — 같은 request_id row 직접 갱신."""

    def __init__(self, *, writer) -> None:
        self._writer = writer

    async def update(
        self,
        *,
        tenant_id: str,
        request_id: str,
        citations: list[dict[str, Any]],
        verifier_metrics: dict[str, Any],
    ) -> int:
        affected = 0
        for rec in self._writer.records:
            if rec.tenant_id == tenant_id and rec.request_id == request_id:
                rec.citations = list(citations)
                rec.verifier_metrics = dict(verifier_metrics)
                affected += 1
        return affected


class CitationReverifyService:
    """rag_core CitationReverifier + chat_logs UPDATE + audit 묶음.

    Args:
        chat_log_reader: tenant scope에서 date range chat_logs 가져옴
        updater: chat_logs citations/verifier_metrics UPDATE
        embedder: Tier 2 재임베딩
        admin_session_factory: tenant_lifecycle_logs audit (None이면 inmemory)
    """

    def __init__(
        self,
        *,
        chat_log_reader: ChatLogReader,
        updater: ChatLogCitationUpdater | InMemoryChatLogCitationUpdater,
        embedder: Embedder,
        admin_session_factory: async_sessionmaker | None = None,
    ) -> None:
        self._reader = chat_log_reader
        self._updater = updater
        self._embedder = embedder
        self._admin_sf = admin_session_factory

    async def reverify(
        self,
        *,
        tenant_id: str,
        actor: str | None,
        from_date: datetime | None,
        to_date: datetime | None,
        thresholds: ReverifyThresholds,
        max_records: int = 100,
    ) -> ReverifyJobSummary:
        summary = ReverifyJobSummary(
            tenant_id=tenant_id, from_date=from_date, to_date=to_date
        )
        reverifier = CitationReverifier(
            embedder=self._embedder, thresholds=thresholds
        )

        filters = ChatLogListFilters(from_date=from_date, to_date=to_date)
        page = 1
        before_sims: list[float] = []
        after_sims: list[float] = []
        while summary.scanned < max_records:
            page_size = min(20, max_records - summary.scanned)
            result = await self._reader.list_by_tenant(
                tenant_id=tenant_id, filters=filters,
                page=page, page_size=page_size,
            )
            if not result.items:
                break
            for record in result.items:
                summary.scanned += 1
                citations = list(record.citations or [])
                if not citations:
                    summary.skipped += 1
                    continue
                try:
                    out = await reverifier.reverify_one(
                        request_id=record.request_id, citations=citations,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "citation_reverify_failed",
                        request_id=record.request_id,
                        error=str(exc),
                    )
                    summary.failures.append(
                        {"request_id": record.request_id, "error": str(exc)}
                    )
                    continue

                new_verifier = dict(record.verifier_metrics or {})
                new_verifier["tier2_avg_similarity"] = out.avg_similarity_after
                new_verifier["reverified_at"] = now_utc_iso()
                new_verifier["reverified_by"] = actor

                applied = await self._updater.update(
                    tenant_id=tenant_id,
                    request_id=record.request_id,
                    citations=out.citations_after,
                    verifier_metrics=new_verifier,
                )
                if applied > 0:
                    summary.updated += 1
                    summary.upgraded += out.upgraded
                    summary.downgraded += out.downgraded
                    if out.avg_similarity_before is not None:
                        before_sims.append(out.avg_similarity_before)
                    after_sims.append(out.avg_similarity_after)
                else:
                    summary.skipped += 1
            if len(result.items) < page_size:
                break
            page += 1

        if before_sims:
            summary.avg_similarity_before = sum(before_sims) / len(before_sims)
        if after_sims:
            summary.avg_similarity_after = sum(after_sims) / len(after_sims)

        await self._audit(summary=summary, actor=actor)
        return summary

    async def _audit(
        self, *, summary: ReverifyJobSummary, actor: str | None
    ) -> None:
        if self._admin_sf is None:
            return
        try:
            async with self._admin_sf() as audit_session:
                await audit_session.execute(
                    text(
                        """
                        INSERT INTO tenant_lifecycle_logs (
                            tenant_id, action, from_state, to_state, actor,
                            reason, details
                        )
                        VALUES (
                            :tenant_id, 'citation_inspector_reverified',
                            NULL, NULL, :actor, NULL, CAST(:details AS JSONB)
                        )
                        """
                    ),
                    {
                        "tenant_id": summary.tenant_id,
                        "actor": actor,
                        "details": json.dumps(
                            {
                                "from_date": summary.from_date.isoformat()
                                if summary.from_date
                                else None,
                                "to_date": summary.to_date.isoformat()
                                if summary.to_date
                                else None,
                                "scanned": summary.scanned,
                                "updated": summary.updated,
                                "skipped": summary.skipped,
                                "upgraded": summary.upgraded,
                                "downgraded": summary.downgraded,
                                "avg_similarity_before": summary.avg_similarity_before,
                                "avg_similarity_after": summary.avg_similarity_after,
                                "failures": summary.failures,
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                    },
                )
                await audit_session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("citation_reverify_audit_failed: %s", str(exc))
