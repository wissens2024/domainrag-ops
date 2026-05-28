"""OldCollectionDropService — ADR-012 §4 + ADR-021 §3 cron.

임베딩 모델 교체 후 30일 hold가 경과한 old collection을 추출해 *알림*한다.
**자동 drop은 하지 않는다** — ADR-012 §4 `require_explicit_delete: true` 정합.
platform_admin이 명시 호출로 drop 실행.

운영 흐름:
  1. tenant_collection_history에서 `swapped_at < NOW() - hold_days` AND `dropped_at IS NULL`
     인 old collection 후보 추출
  2. platform_admin notification (Ledger publish: `event_type='old_collection_drop_candidates'`)
  3. tenant_lifecycle_logs INSERT (audit)
  4. *실제 drop은 하지 않음* — 별도 admin API 호출 의무

advisory lock id: 0x7019_0401 (ADR-021 §3 cron 표).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)


_OLD_COLLECTION_LOCK_KEY = 7019_0401  # ADR-021 §3 표


@dataclass
class CandidateRow:
    domain_id: str
    old_collection: str
    swapped_at: datetime
    days_since_swap: int


@dataclass
class ScanSummary:
    scanned_at: datetime
    candidates: list[CandidateRow] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)


class OldCollectionDropService:
    """30일 hold 경과 old collection 후보 추출 + 알림.

    Args:
        admin_session_factory: BYPASSRLS admin engine
        ledger_audit: 선택 — 있으면 platform_admin_action 으로 publish
        hold_days: default 30 (ADR-012 §4 platform 기본값)
    """

    def __init__(
        self,
        *,
        admin_session_factory: async_sessionmaker,
        ledger_audit=None,
        hold_days: int = 30,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._admin = admin_session_factory
        self._ledger = ledger_audit
        self._hold_days = hold_days
        self._clock = clock

    async def scan(self) -> ScanSummary:
        """advisory lock 시도 + 후보 추출 + 알림.

        Returns: ScanSummary (lock 미획득 시 skipped_reason='advisory_lock_busy').
        """
        async with self._admin() as session:
            lock_row = (
                await session.execute(
                    text("SELECT pg_try_advisory_lock(:key) AS got"),
                    {"key": _OLD_COLLECTION_LOCK_KEY},
                )
            ).first()
            got_lock = bool(lock_row[0]) if lock_row else False

        if not got_lock:
            return ScanSummary(
                scanned_at=self._clock(),
                skipped_reason="advisory_lock_busy",
            )

        try:
            return await self._scan_locked()
        finally:
            async with self._admin() as unlock_session:
                await unlock_session.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _OLD_COLLECTION_LOCK_KEY},
                )

    async def _scan_locked(self) -> ScanSummary:
        scanned_at = self._clock()
        # tenant_collection_history 테이블은 ADR-012 §4에 명시되어 있으나 현재
        # schema에 미존재. 본 service는 테이블이 생기면 자동 동작하도록 graceful.
        candidates: list[CandidateRow] = []
        async with self._admin() as session:
            try:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT domain_id, old_collection, swapped_at,
                                   EXTRACT(DAY FROM NOW() - swapped_at)::int AS days
                              FROM tenant_collection_history
                             WHERE swapped_at < NOW() - (:hold_days || ' days')::interval
                               AND dropped_at IS NULL
                            """
                        ),
                        {"hold_days": self._hold_days},
                    )
                ).all()
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "tenant_collection_history not present (yet) — skip: %s", exc
                )
                return ScanSummary(
                    scanned_at=scanned_at,
                    skipped_reason="table_not_present",
                )
        for r in rows:
            candidates.append(
                CandidateRow(
                    domain_id=str(r[0]),
                    old_collection=str(r[1]),
                    swapped_at=r[2],
                    days_since_swap=int(r[3]),
                )
            )

        summary = ScanSummary(scanned_at=scanned_at, candidates=candidates)

        if candidates and self._ledger is not None:
            try:
                await self._ledger.publish_platform_admin_action(
                    domain_id="platform",
                    actor="old_collection_drop_service",
                    action="old_collection_drop_candidates",
                    reason=f"{len(candidates)}개 collection이 {self._hold_days}일 hold 경과",
                    details={
                        "candidates": [
                            {
                                "domain_id": c.domain_id,
                                "collection": c.old_collection,
                                "days_since_swap": c.days_since_swap,
                            }
                            for c in candidates
                        ],
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ledger publish (old_collection candidates) failed: %s", exc)

        # 또한 tenant_lifecycle_logs 에 audit 기록 (per-tenant)
        if candidates:
            async with self._admin() as session:
                for c in candidates:
                    await session.execute(
                        text(
                            """
                            INSERT INTO tenant_lifecycle_logs (
                                domain_id, action, from_state, to_state,
                                actor, reason, details
                            )
                            VALUES (
                                :domain_id, 'old_collection_drop_candidate',
                                NULL, NULL, 'cron',
                                'hold_days_exceeded',
                                CAST(:details AS JSONB)
                            )
                            """
                        ),
                        {
                            "domain_id": c.domain_id,
                            "details": json.dumps(
                                {
                                    "collection": c.old_collection,
                                    "days_since_swap": c.days_since_swap,
                                    "hold_days": self._hold_days,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    )
                await session.commit()

        logger.info(
            "old_collection_drop scan: candidates=%d", len(candidates)
        )
        return summary
