"""ChatLogsPartitionService — ADR-019 §2 partition 생성 SQL 흐름 검증.

실DB 없이 mock session_factory로 호출 시퀀스를 캡쳐해 다음을 확인:
  - pg_try_advisory_lock(key) 호출
  - 존재 확인 SELECT
  - CREATE TABLE IF NOT EXISTS … PARTITION OF chat_logs 호출
  - pg_advisory_unlock(key) 호출
  - 다음달/연말경계(12월→1월) 계산
  - lock 미획득 시 SKIP 처리
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from app.services.chat_logs_partition_service import (
    ChatLogsPartitionService,
    _next_month,
    _partition_name,
    _partition_range,
)


@dataclass
class _Captured:
    calls: list[tuple[str, dict]] = field(default_factory=list)
    committed: int = 0


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


def _make_session(rec: _Captured, *, lock_acquired: bool, partition_exists: bool):
    class _Session:
        async def __aenter__(self_):
            return self_

        async def __aexit__(self_, exc_type, exc, tb):
            return None

        async def execute(self_, stmt, params=None):
            sql = str(stmt)
            rec.calls.append((sql, dict(params or {})))
            if "pg_try_advisory_lock" in sql:
                return _Result([(lock_acquired,)])
            if "FROM pg_class c" in sql and "pg_inherits" in sql:
                return _Result([(1,)] if partition_exists else [])
            if "pg_advisory_unlock" in sql:
                return _Result([(True,)])
            return _Result([])

        async def commit(self_):
            rec.committed += 1

    def _factory():
        return _Session()

    return _factory


# --------------------------------------------------------------------------- #
# helpers (pure)
# --------------------------------------------------------------------------- #


def test_next_month_normal_case():
    from datetime import date

    assert _next_month(date(2026, 5, 12)) == (2026, 6)
    assert _next_month(date(2026, 1, 1)) == (2026, 2)


def test_next_month_year_boundary():
    from datetime import date

    assert _next_month(date(2026, 12, 31)) == (2027, 1)


def test_partition_range_and_name():
    from datetime import date

    start, end = _partition_range(2026, 6)
    assert start == date(2026, 6, 1)
    assert end == date(2026, 7, 1)
    assert _partition_name(2026, 6) == "chat_logs_y2026m06"

    # 12월 → 다음해 1월
    s, e = _partition_range(2026, 12)
    assert s == date(2026, 12, 1)
    assert e == date(2027, 1, 1)


# --------------------------------------------------------------------------- #
# service flow
# --------------------------------------------------------------------------- #


async def test_ensure_creates_partition_when_missing():
    rec = _Captured()
    svc = ChatLogsPartitionService(
        admin_session_factory=_make_session(rec, lock_acquired=True, partition_exists=False),
        clock=lambda: datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    result = await svc.ensure_next_month_partition()
    assert result.partition_name == "chat_logs_y2026m06"
    assert result.created is True
    assert result.skipped_reason is None

    sqls = " | ".join(s for s, _ in rec.calls).lower()
    assert "pg_try_advisory_lock" in sqls
    assert "create table if not exists chat_logs_y2026m06" in sqls
    assert "pg_advisory_unlock" in sqls

    # 회귀 가드: asyncpg는 DDL에 바인드 파라미터를 거부한다. CREATE TABLE 호출은
    # 날짜를 ISO 리터럴로 인라인하고 params를 넘기지 않아야 한다.
    create_calls = [(s, p) for s, p in rec.calls if "create table" in s.lower()]
    assert len(create_calls) == 1
    create_sql, create_params = create_calls[0]
    assert create_params == {}
    assert "'2026-06-01'" in create_sql
    assert "'2026-07-01'" in create_sql
    assert ":start_date" not in create_sql and ":end_date" not in create_sql


async def test_ensure_skips_when_partition_already_exists():
    rec = _Captured()
    svc = ChatLogsPartitionService(
        admin_session_factory=_make_session(rec, lock_acquired=True, partition_exists=True),
        clock=lambda: datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    result = await svc.ensure_next_month_partition()
    assert result.created is False
    assert result.skipped_reason == "already_exists"

    sqls = " | ".join(s for s, _ in rec.calls).lower()
    # CREATE 시도 안 함
    assert "create table" not in sqls
    # unlock은 호출됨
    assert "pg_advisory_unlock" in sqls


async def test_ensure_skips_when_lock_unavailable():
    rec = _Captured()
    svc = ChatLogsPartitionService(
        admin_session_factory=_make_session(rec, lock_acquired=False, partition_exists=False),
        clock=lambda: datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    result = await svc.ensure_next_month_partition()
    assert result.created is False
    assert result.skipped_reason == "advisory_lock_busy"

    sqls = " | ".join(s for s, _ in rec.calls).lower()
    # CREATE / 존재 확인 / unlock 모두 호출 안 됨
    assert "create table" not in sqls
    assert "pg_inherits" not in sqls
    assert "pg_advisory_unlock" not in sqls


async def test_ensure_explicit_year_month_path():
    """backfill 진입점 — ensure_partition(year, month)도 정상 동작."""
    rec = _Captured()
    svc = ChatLogsPartitionService(
        admin_session_factory=_make_session(rec, lock_acquired=True, partition_exists=False),
    )
    result = await svc.ensure_partition(year=2026, month=12)
    assert result.partition_name == "chat_logs_y2026m12"
    assert result.range_start.isoformat() == "2026-12-01"
    assert result.range_end.isoformat() == "2027-01-01"
    assert result.created is True
