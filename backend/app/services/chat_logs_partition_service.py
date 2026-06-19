"""ChatLogsPartitionService — ADR-019 §2 월별 partition 자동 생성.

매월 1일 자정 cron이 호출. 다음달(또는 caller가 지정한 month) partition을 idempotent하게
생성한다.

  CREATE TABLE chat_logs_y2026m06 PARTITION OF chat_logs
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');

multiple cron instance가 동시 실행될 위험은 pg_try_advisory_lock(int)로 차단 — 잠금
획득 실패면 skip(다른 instance가 처리 중). Redis 불필요(ADR-019 §11).

운영 정책 (ADR-019 §2):
  - chat_logs는 시간 기반 partitioning 의무 (`PARTITION BY RANGE (created_at)`)
  - 매월 1일 자정 다음달 partition 미리 생성 (대형 tenant insert 시 RLS scan 비용 제한)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)


# pg_advisory_lock key — 고정값. ADR-019 §2 chat_logs partition cron 전용.
# 다른 advisory lock과 겹치지 않는 임의 정수. 변경 금지.
_PARTITION_LOCK_KEY = 7019_0612  # ADR-019, 2026-06 cron


@dataclass
class PartitionResult:
    target_year: int
    target_month: int
    partition_name: str  # e.g. chat_logs_y2026m06
    range_start: date    # FROM
    range_end: date      # TO (exclusive)
    created: bool        # True = CREATE TABLE 성공, False = 이미 존재 or lock 미획득
    skipped_reason: str | None = None


def _next_month(today: date) -> tuple[int, int]:
    """today 기준 다음달 (year, month). 12월이면 다음해 1월."""
    if today.month == 12:
        return today.year + 1, 1
    return today.year, today.month + 1


def _partition_range(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start, end


def _partition_name(year: int, month: int) -> str:
    return f"chat_logs_y{year:04d}m{month:02d}"


class ChatLogsPartitionService:
    """ADR-019 §2 — chat_logs 월별 partition 자동 생성.

    Args:
        admin_session_factory: domainrag_platform_admin role의 sessionmaker (BYPASSRLS).
            CREATE TABLE 권한이 필요하므로 platform_admin engine 사용.
    """

    def __init__(
        self,
        *,
        admin_session_factory: async_sessionmaker,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._sf = admin_session_factory
        self._clock = clock

    async def ensure_next_month_partition(self) -> PartitionResult:
        """다음달 partition을 idempotent하게 보장.

        흐름:
          1. pg_try_advisory_lock 획득 (실패 → 다른 instance가 처리 중, skip)
          2. CREATE TABLE IF NOT EXISTS … PARTITION OF chat_logs
          3. pg_advisory_unlock + commit
        """
        today = self._clock().date()
        year, month = _next_month(today)
        return await self.ensure_partition(year=year, month=month)

    async def ensure_partition(self, *, year: int, month: int) -> PartitionResult:
        """특정 year/month partition을 idempotent하게 생성 (backfill 또는 다음달용 공용 진입점)."""
        range_start, range_end = _partition_range(year, month)
        name = _partition_name(year, month)

        async with self._sf() as session:
            lock_row = (
                await session.execute(
                    text("SELECT pg_try_advisory_lock(:key) AS got"),
                    {"key": _PARTITION_LOCK_KEY},
                )
            ).first()
            got_lock = bool(lock_row[0]) if lock_row else False
            if not got_lock:
                return PartitionResult(
                    target_year=year,
                    target_month=month,
                    partition_name=name,
                    range_start=range_start,
                    range_end=range_end,
                    created=False,
                    skipped_reason="advisory_lock_busy",
                )

            try:
                # IF NOT EXISTS — 이미 존재해도 안전(에러 없음). created 여부는 CREATE 직전
                # SELECT로 판정해도 되지만, IF NOT EXISTS의 idempotency가 race-safe.
                existed_row = (
                    await session.execute(
                        text(
                            """
                            SELECT 1 FROM pg_class c
                              JOIN pg_inherits i ON c.oid = i.inhrelid
                              JOIN pg_class p ON i.inhparent = p.oid
                             WHERE c.relname = :child AND p.relname = 'chat_logs'
                            """
                        ),
                        {"child": name},
                    )
                ).first()
                existed = existed_row is not None

                if not existed:
                    # asyncpg는 DDL(CREATE TABLE)에 바인드 파라미터를 허용하지 않는다
                    # ("parameters are supported only in SELECT/INSERT/UPDATE/DELETE/VALUES").
                    # range_start/end는 내부 계산 date(사용자 입력 아님)라 ISO 리터럴 인라인이
                    # 주입 위험 없이 안전하다. name도 _partition_name으로 계산된 식별자.
                    await session.execute(
                        text(
                            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF chat_logs "
                            f"FOR VALUES FROM ('{range_start.isoformat()}') "
                            f"TO ('{range_end.isoformat()}')"
                        )
                    )
                    await session.commit()
                else:
                    await session.commit()
            finally:
                async with self._sf() as unlock_session:
                    await unlock_session.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": _PARTITION_LOCK_KEY},
                    )

            return PartitionResult(
                target_year=year,
                target_month=month,
                partition_name=name,
                range_start=range_start,
                range_end=range_end,
                created=not existed,
                skipped_reason=None if not existed else "already_exists",
            )


# --------------------------------------------------------------------------- #
# CLI entry — python -m app.services.chat_logs_partition_worker
# --------------------------------------------------------------------------- #


async def _main() -> None:
    """운영 cron 진입. 다음달 partition + 추가 backfill(현재달 누락 시) 함께 처리."""
    from app.core.db import AdminSessionLocal

    svc = ChatLogsPartitionService(admin_session_factory=AdminSessionLocal)

    # 현재달이 없을 수도 있으니 같이 보장 (운영 처음 setup 시).
    now = datetime.now(timezone.utc).date()
    this = await svc.ensure_partition(year=now.year, month=now.month)
    nxt = await svc.ensure_next_month_partition()

    logger.info(
        "chat_logs partition ensured: this=%s(created=%s, reason=%s), next=%s(created=%s, reason=%s)",
        this.partition_name, this.created, this.skipped_reason,
        nxt.partition_name, nxt.created, nxt.skipped_reason,
    )


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
