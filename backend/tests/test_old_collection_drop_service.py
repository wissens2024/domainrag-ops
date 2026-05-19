"""OldCollectionDropService — ADR-012 §4 + ADR-021 §3 cron scan 검증."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.services.old_collection_drop_service import (
    OldCollectionDropService,
    ScanSummary,
)


class _LedgerSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish_platform_admin_action(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return True


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, recorder: "_Recorder") -> None:
        self._r = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "pg_try_advisory_lock" in sql:
            return _RowsResult([(self._r.lock_granted,)])
        if "pg_advisory_unlock" in sql:
            return _RowsResult([(True,)])
        if "FROM tenant_collection_history" in sql:
            if self._r.history_table_missing:
                raise RuntimeError("relation tenant_collection_history does not exist")
            return _RowsResult(self._r.candidates)
        if "INSERT INTO tenant_lifecycle_logs" in sql:
            self._r.audit_calls.append(dict(params or {}))
            return _RowsResult([])
        return _RowsResult([])

    async def commit(self):
        return None


class _Recorder:
    def __init__(self) -> None:
        self.lock_granted = True
        self.history_table_missing = False
        self.candidates: list[tuple] = []
        self.audit_calls: list[dict] = []

    def factory(self):
        return _Session(self)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_scan_skipped_when_advisory_lock_busy():
    rec = _Recorder()
    rec.lock_granted = False
    ledger = _LedgerSpy()
    svc = OldCollectionDropService(
        admin_session_factory=rec.factory, ledger_audit=ledger
    )
    summary = _run(svc.scan())
    assert isinstance(summary, ScanSummary)
    assert summary.skipped_reason == "advisory_lock_busy"
    assert summary.candidate_count == 0
    assert ledger.calls == []


def test_scan_graceful_when_history_table_missing():
    rec = _Recorder()
    rec.history_table_missing = True
    ledger = _LedgerSpy()
    svc = OldCollectionDropService(
        admin_session_factory=rec.factory, ledger_audit=ledger
    )
    summary = _run(svc.scan())
    assert summary.skipped_reason == "table_not_present"
    assert ledger.calls == []


def test_scan_no_candidates_no_ledger_publish():
    rec = _Recorder()
    rec.candidates = []  # 빈 결과
    ledger = _LedgerSpy()
    svc = OldCollectionDropService(
        admin_session_factory=rec.factory, ledger_audit=ledger
    )
    summary = _run(svc.scan())
    assert summary.candidate_count == 0
    assert summary.skipped_reason is None
    assert ledger.calls == []
    assert rec.audit_calls == []


def test_scan_candidates_publish_to_ledger_and_audit():
    rec = _Recorder()
    rec.candidates = [
        ("security", "chunks_security__v1", datetime(2026, 4, 1, tzinfo=timezone.utc), 44),
        ("legal", "chunks_legal__v1", datetime(2026, 3, 15, tzinfo=timezone.utc), 60),
    ]
    ledger = _LedgerSpy()
    svc = OldCollectionDropService(
        admin_session_factory=rec.factory, ledger_audit=ledger
    )
    summary = _run(svc.scan())
    assert summary.candidate_count == 2
    assert summary.candidates[0].tenant_id == "security"
    assert summary.candidates[0].days_since_swap == 44
    assert len(ledger.calls) == 1
    call = ledger.calls[0]
    assert call["action"] == "old_collection_drop_candidates"
    assert call["tenant_id"] == "platform"
    assert len(call["details"]["candidates"]) == 2
    # tenant_lifecycle_logs audit 2건 (per-candidate)
    assert len(rec.audit_calls) == 2


def test_scan_ledger_publish_failure_swallowed():
    """ledger 호출이 raise해도 service는 정상 동작 (swallow)."""

    class _FailingLedger:
        async def publish_platform_admin_action(self, **kwargs):
            raise RuntimeError("ledger down")

    rec = _Recorder()
    rec.candidates = [
        ("security", "chunks_security__v1", datetime(2026, 4, 1, tzinfo=timezone.utc), 44),
    ]
    svc = OldCollectionDropService(
        admin_session_factory=rec.factory, ledger_audit=_FailingLedger()
    )
    summary = _run(svc.scan())
    assert summary.candidate_count == 1  # service는 정상 완료
