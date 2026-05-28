"""PostgresTenantLifecycleService — DB 없이 mock session으로 SQL 흐름 검증."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from app.services.tenant_lifecycle_service import (
    InvalidStatusTransitionError,
    PostgresTenantLifecycleService,
    TenantConflictError,
    TenantNotArchivedError,
    TenantNotFoundError,
)


@dataclass
class _Captured:
    statements: list[tuple[str, dict]] = field(default_factory=list)
    committed: int = 0
    rolled_back: int = 0


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


def _factory(rec: _Captured, responses: dict):
    """responses: SQL substring → list of rows. 매치되는 첫 키의 응답 반환."""

    class _Session:
        async def __aenter__(self_):
            return self_

        async def __aexit__(self_, exc_type, exc, tb):
            return None

        async def execute(self_, stmt, params=None):
            sql = str(stmt)
            rec.statements.append((sql, dict(params or {})))
            for needle, rows in responses.items():
                if needle in sql:
                    return _Result(rows)
            return _Result([])

        async def commit(self_):
            rec.committed += 1

        async def rollback(self_):
            rec.rolled_back += 1

    def _make():
        return _Session()
    return _make


class _RecordingLedger:
    def __init__(self):
        self.calls: list[dict] = []

    async def publish_platform_admin_action(self, **kwargs):
        self.calls.append(kwargs)


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #


async def test_register_executes_insert_and_returns_record():
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    rec = _Captured()
    sf = _factory(rec, {
        "INSERT INTO tenants": [("bge-m3", "idle", now, now)],
    })
    ledger = _RecordingLedger()
    svc = PostgresTenantLifecycleService(
        admin_session_factory=sf,
        ledger_audit=ledger,
    )
    out = await svc.register(
        domain_id="legal", display_name="법무",
        domain_type="legal", modules=["rag"], actor="platform-admin-1",
    )
    assert out.domain_id == "legal"
    assert out.status == "active"
    sqls = " | ".join(s for s, _ in rec.statements)
    assert "INSERT INTO tenants" in sqls
    # ledger publish 1회 (tenant_registered)
    assert len(ledger.calls) == 1
    assert ledger.calls[0]["action"] == "tenant_registered"


async def test_register_duplicate_raises_conflict():
    from sqlalchemy.exc import IntegrityError

    class _FailingSession:
        async def __aenter__(self_):
            return self_

        async def __aexit__(self_, *a, **k):
            return None

        async def execute(self_, *a, **k):
            raise IntegrityError("dup", params=None, orig=Exception("unique"))

        async def commit(self_):
            pass

        async def rollback(self_):
            pass

    svc = PostgresTenantLifecycleService(
        admin_session_factory=lambda: _FailingSession(),
    )
    with pytest.raises(TenantConflictError):
        await svc.register(
            domain_id="legal", display_name="법무", actor="x",
        )


# --------------------------------------------------------------------------- #
# get / list / update_status
# --------------------------------------------------------------------------- #


async def test_get_404_when_no_row():
    rec = _Captured()
    sf = _factory(rec, {"SELECT display_name": []})
    svc = PostgresTenantLifecycleService(admin_session_factory=sf)
    with pytest.raises(TenantNotFoundError):
        await svc.get("missing")


async def test_get_returns_record_when_present():
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    rec = _Captured()
    sf = _factory(rec, {
        "SELECT display_name": [
            ("Legal", "legal", "bge-m3", "idle", "active",
             ["rag"], now, now, None, None, "assigned")
        ],
    })
    svc = PostgresTenantLifecycleService(admin_session_factory=sf)
    out = await svc.get("legal")
    assert out.domain_id == "legal"
    assert out.display_name == "Legal"
    assert out.status == "active"


async def test_update_status_validates_transition():
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    rec = _Captured()
    sf = _factory(rec, {
        "SELECT display_name": [
            ("Legal", None, "bge-m3", "idle", "active",
             ["rag"], now, now, None, None, "assigned")
        ],
    })
    ledger = _RecordingLedger()
    svc = PostgresTenantLifecycleService(
        admin_session_factory=sf, ledger_audit=ledger
    )
    # active → suspended는 허용
    out = await svc.update_status(
        domain_id="legal", to_status="suspended", actor="root",
    )
    assert out.status == "suspended"
    sqls = " | ".join(s for s, _ in rec.statements)
    assert "UPDATE tenants" in sqls
    assert ledger.calls[0]["action"] == "tenant_status_changed"


async def test_update_status_invalid_raises():
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    rec = _Captured()
    sf = _factory(rec, {
        "SELECT display_name": [
            ("Legal", None, "bge-m3", "idle", "active",
             ["rag"], now, now, None, None, "assigned")
        ],
    })
    svc = PostgresTenantLifecycleService(admin_session_factory=sf)
    with pytest.raises(InvalidStatusTransitionError):
        await svc.update_status(
            domain_id="legal", to_status="deleted", actor="root",
        )


# --------------------------------------------------------------------------- #
# hard_delete
# --------------------------------------------------------------------------- #


async def test_hard_delete_requires_archived_status():
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    rec = _Captured()
    sf = _factory(rec, {
        "SELECT display_name": [
            ("Legal", None, "bge-m3", "idle", "active",
             ["rag"], now, now, None, None, "assigned")
        ],
    })
    svc = PostgresTenantLifecycleService(admin_session_factory=sf)
    with pytest.raises(TenantNotArchivedError) as exc:
        await svc.hard_delete(domain_id="legal", actor="root", reason="x")
    assert exc.value.status == "active"


class _RecordingVectorStore:
    def __init__(self):
        self.deleted: list[str] = []

    async def delete_collection(self, domain_id: str) -> None:
        self.deleted.append(domain_id)


class _RecordingStorage:
    def __init__(self):
        self.deleted: list[tuple[str, str]] = []

    async def delete(self, *, domain_id: str, doc_id: str, version=None):
        self.deleted.append((domain_id, doc_id))
        return 0


async def test_hard_delete_runs_full_cross_system_when_archived():
    """archived 상태 → 모든 scoped 테이블 DELETE + Qdrant drop + storage rm + tenants
    status=deleted + Ledger publish."""
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    rec = _Captured()
    sf = _factory(rec, {
        # 처음 1회 get → archived 상태
        "SELECT display_name": [
            ("Legal", None, "bge-m3", "idle", "archived",
             ["rag"], now, now, None, None, "assigned")
        ],
    })
    vs = _RecordingVectorStore()
    storage = _RecordingStorage()
    ledger = _RecordingLedger()
    svc = PostgresTenantLifecycleService(
        admin_session_factory=sf,
        vector_store=vs, storage=storage, ledger_audit=ledger,
    )
    out = await svc.hard_delete(
        domain_id="legal", actor="platform-admin", reason="compliance"
    )
    assert out.status == "deleted"
    assert out.delete_status == "completed"
    assert vs.deleted == ["legal"]
    assert storage.deleted == [("legal", "")]
    sqls = " | ".join(s for s, _ in rec.statements)
    # tenants.delete_status='in_progress' UPDATE 1회 + 각 테이블 DELETE + 최종 UPDATE
    assert "UPDATE tenants" in sqls
    assert "DELETE FROM chunks " in sqls or "DELETE FROM chunks\n" in sqls
    assert "DELETE FROM documents" in sqls
    # ledger
    assert any(c["action"] == "tenant_hard_deleted" for c in ledger.calls)
