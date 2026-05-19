"""ArchivalWorker — 단일 tenant 처리 + 다중 tenant 순회 + audit 기록 검증 (ADR-012 §3).

InMemory archiver + mock vector_store + mock admin_session으로 DB 의존 없이 동작.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from rag_core.clients.in_memory import InMemoryChunkRepository, InMemoryVectorStore
from rag_core.interfaces.chunk_repository import ChunkRecord
from rag_core.services.chunks_archiver import (
    ArchivalBatchResult,
    InMemoryChunksArchiver,
)

from app.services.archival_worker import ArchivalWorker


def _chunk(*, tenant_id, chunk_id, valid_until):
    return ChunkRecord(
        tenant_id=tenant_id,
        chunk_id=chunk_id,
        doc_id="d1",
        doc_version="v1",
        parser_version="p1",
        chunk_strategy="fixed-window-v1",
        chunk_index=0,
        content=f"content {chunk_id}",
        title="t",
        page_number=1,
        section_title=None,
        heading_path=[],
        content_hash=None,
        start_char=0,
        end_char=10,
        token_count=2,
        department=None,
        doc_type=None,
        security_level="internal",
        acl=[],
        approval_status="approved",
        tags=[],
        valid_from=None,
        valid_until=valid_until,
    )


async def _populate(repo, chunks):
    by_key: dict[tuple, list] = {}
    for c in chunks:
        key = (c.tenant_id, c.doc_id, c.doc_version, c.parser_version)
        by_key.setdefault(key, []).append(c)
    for (tid, did, ver, parser), batch in by_key.items():
        await repo.replace_chunks(
            tenant_id=tid, doc_id=did, doc_version=ver,
            parser_version=parser, chunks=batch,
        )


class _AuditRecorder:
    """admin_session_factory 대용 — execute 호출을 캡쳐."""

    def __init__(self, tenants: list[str]):
        self._tenants = tenants
        self.audit_payloads: list[dict] = []
        self.set_payload_calls: list[dict] = []  # not used; for symmetry

    def __call__(self):
        return _Session(self)


class _Session:
    def __init__(self, recorder: _AuditRecorder):
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "pg_try_advisory_lock" in sql:
            # ADR-021 §3 — multi-instance lock 시뮬레이션: 항상 획득 성공
            return _RowsResult([(True,)])
        if "pg_advisory_unlock" in sql:
            return _RowsResult([(True,)])
        if "FROM tenants" in sql:
            return _RowsResult(
                [(tid,) for tid in self._recorder._tenants]
            )
        if "INSERT INTO tenant_lifecycle_logs" in sql:
            self._recorder.audit_payloads.append(dict(params or {}))
            return _RowsResult([])
        if "set_config" in sql:
            return _RowsResult([])
        return _RowsResult([])

    async def commit(self):
        return None


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def world():
    """tenant 두 곳 + InMemory archiver + mock admin session."""
    repo = InMemoryChunkRepository()
    return {"repo": repo}


async def test_worker_archives_only_threshold_chunks_and_calls_set_payload(world):
    repo = world["repo"]
    await _populate(
        repo,
        [
            _chunk(tenant_id="t1", chunk_id="old", valid_until="2026-01-01"),
            _chunk(tenant_id="t1", chunk_id="fresh", valid_until="2026-06-01"),
        ],
    )
    archiver = InMemoryChunksArchiver(chunk_repo=repo)
    vs = InMemoryVectorStore()
    await vs.create_collection(tenant_id="t1", dense_dim=4)
    await vs.upsert_chunks(
        tenant_id="t1",
        points=[
            {"id": "old", "dense_vector": [0.1, 0.2, 0.3, 0.4], "sparse_vector": {}, "payload": {}},
            {"id": "fresh", "dense_vector": [0.5, 0.5, 0.5, 0.5], "sparse_vector": {}, "payload": {}},
        ],
    )
    rec = _AuditRecorder(tenants=["t1"])
    worker = ArchivalWorker(
        archiver=archiver,
        vector_store=vs,
        admin_session_factory=rec,
        clock=lambda: datetime(2026, 5, 10, tzinfo=timezone.utc),
        threshold_days_provider=lambda _t: 90,
    )

    result = await worker.run_for_tenant("t1")
    assert result.archived_chunk_ids == ["old"]
    # vector store payload 확인
    coll = vs._collections["t1"]  # type: ignore[attr-defined]
    assert coll.points["old"].payload.get("archived") is True
    assert "archived" not in coll.points["fresh"].payload

    # audit row 1건 (chunk_archival_executed)
    assert len(rec.audit_payloads) == 1
    audit = rec.audit_payloads[0]
    assert audit["tenant_id"] == "t1"


async def test_worker_run_all_tenants_iterates_active_tenants(world):
    repo = world["repo"]
    await _populate(
        repo,
        [
            _chunk(tenant_id="t1", chunk_id="t1-old", valid_until="2026-01-01"),
            _chunk(tenant_id="t2", chunk_id="t2-old", valid_until="2026-01-01"),
        ],
    )
    archiver = InMemoryChunksArchiver(chunk_repo=repo)
    vs = InMemoryVectorStore()
    for tid in ("t1", "t2"):
        await vs.create_collection(tenant_id=tid, dense_dim=4)
        await vs.upsert_chunks(
            tenant_id=tid,
            points=[
                {"id": f"{tid}-old", "dense_vector": [0.1, 0.2, 0.3, 0.4],
                 "sparse_vector": {}, "payload": {}}
            ],
        )
    rec = _AuditRecorder(tenants=["t1", "t2"])
    worker = ArchivalWorker(
        archiver=archiver,
        vector_store=vs,
        admin_session_factory=rec,
        clock=lambda: datetime(2026, 5, 10, tzinfo=timezone.utc),
    )
    summary = await worker.run_all_tenants()
    assert summary.total_archived == 2
    tenant_ids = {r.tenant_id for r in summary.tenant_results}
    assert tenant_ids == {"t1", "t2"}


async def test_worker_handles_no_candidates_gracefully(world):
    """후보가 없으면 set_payload·audit 모두 호출되지 않음."""
    repo = world["repo"]
    await _populate(
        repo,
        [_chunk(tenant_id="t1", chunk_id="fresh", valid_until="2026-06-01")],
    )
    archiver = InMemoryChunksArchiver(chunk_repo=repo)
    vs = InMemoryVectorStore()
    await vs.create_collection(tenant_id="t1", dense_dim=4)
    rec = _AuditRecorder(tenants=["t1"])
    worker = ArchivalWorker(
        archiver=archiver,
        vector_store=vs,
        admin_session_factory=rec,
        clock=lambda: datetime(2026, 5, 10, tzinfo=timezone.utc),
    )
    result = await worker.run_for_tenant("t1")
    assert result.archived_chunk_ids == []
    assert rec.audit_payloads == []  # audit도 skip


async def test_worker_uses_threshold_provider_per_tenant(world):
    """tenant별 threshold_days override 적용 — t1=90, t2=30."""
    repo = world["repo"]
    # 2026-04-15는 2026-05-10 기준 25일 전 → 90일 threshold에선 후보 X, 30일 threshold도 후보 X
    # 2026-04-01은 39일 전 → 30일 threshold에선 후보 O
    await _populate(
        repo,
        [
            _chunk(tenant_id="t1", chunk_id="t1-medium", valid_until="2026-04-01"),
            _chunk(tenant_id="t2", chunk_id="t2-medium", valid_until="2026-04-01"),
        ],
    )
    archiver = InMemoryChunksArchiver(chunk_repo=repo)
    vs = InMemoryVectorStore()
    for tid in ("t1", "t2"):
        await vs.create_collection(tenant_id=tid, dense_dim=4)
        await vs.upsert_chunks(
            tenant_id=tid,
            points=[
                {"id": f"{tid}-medium", "dense_vector": [0.1, 0.2, 0.3, 0.4],
                 "sparse_vector": {}, "payload": {}}
            ],
        )
    rec = _AuditRecorder(tenants=["t1", "t2"])
    worker = ArchivalWorker(
        archiver=archiver,
        vector_store=vs,
        admin_session_factory=rec,
        clock=lambda: datetime(2026, 5, 10, tzinfo=timezone.utc),
        threshold_days_provider=lambda tid: 90 if tid == "t1" else 30,
    )
    summary = await worker.run_all_tenants()
    archived = [
        c for r in summary.tenant_results for c in r.archived_chunk_ids
    ]
    assert archived == ["t2-medium"]
