"""POST /api/{domain_id}/admin/documents/upload + reindex + indexing/jobs 통합 테스트.

env: AUTH_MODE=mock + RAG_BACKEND=inmemory (conftest.py).
InMemory backend로 storage→parse→chunk→embed→upsert→DB write 전체 흐름을 검증한다.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _reset_singletons():
    """매 테스트마다 RAGService / IndexingOrchestrator 싱글턴을 리셋해 InMemory 상태를
    분리 (다른 테스트의 chunks/jobs가 누수되지 않도록)."""
    from app.deps import reset_indexing_orchestrator, reset_rag_service

    reset_rag_service()
    reset_indexing_orchestrator()
    yield
    reset_rag_service()
    reset_indexing_orchestrator()


def test_upload_returns_job_id_and_runs_indexing_in_background():
    """multipart upload → 202 + job_id. background 실행 후 job status='completed'."""
    file_content = b"DomainRAG indexing pipeline test. " * 5
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/documents/upload",
            files={"file": ("test.txt", io.BytesIO(file_content), "text/plain")},
            data={
                # input_type 미지정 — ADR-015 schema 검증 skip (해당 테스트는 indexing 흐름 한정)
                "title": "테스트 문서",
                "version": "v1",
                "metadata": '{"acl": ["group:security"], "approval_status": "approved"}',
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["job_id"].startswith("IDX-")
    assert body["doc_id"].startswith("DOC-")
    assert body["version"] == "v1"
    job_id = body["job_id"]

    # background 실행 후 status가 completed 또는 partial이어야 한다 (TestClient는 background
    # tasks 완료까지 await — Starlette BaseHTTPResponse가 response.background를 await).
    with TestClient(app) as client:
        detail = client.get(
            f"/api/security/admin/indexing/jobs/{job_id}",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert detail.status_code == 200, detail.text
    job = detail.json()
    assert job["status"] in {"completed", "partial"}, job
    assert job["total_chunks"] >= 1
    assert job["indexed_chunks"] >= 1
    assert job["failure_rate"] == 0.0 or job["failure_rate"] is None


def test_upload_then_list_jobs_includes_new_job():
    """list endpoint가 방금 만든 job을 반환."""
    with TestClient(app) as client:
        up = client.post(
            "/api/security/admin/documents/upload",
            files={"file": ("a.txt", io.BytesIO(b"hello indexing world"), "text/plain")},
            data={"title": "A", "version": "v1"},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert up.status_code == 202, up.text
        job_id = up.json()["job_id"]

        listing = client.get(
            "/api/security/admin/indexing/jobs",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert any(j["job_id"] == job_id for j in items)


def test_upload_returns_409_on_active_job_conflict():
    """같은 (doc_id, version)에 대한 active 인덱싱 job이 존재하면 두 번째 upload는 409.

    InMemory 환경에서는 첫 background가 응답과 동시에 완료되므로 active 상태를 재현하려면
    repo에 직접 active status job을 미리 등록한다 — ADR-012 §10 unique 조건의 정합성을
    검증하는 의도.
    """
    import asyncio

    from app.deps import get_indexing_orchestrator, get_settings
    from rag_core.interfaces.chunk_repository import IndexingJobRecord

    settings = get_settings()
    orch = get_indexing_orchestrator(settings)
    repo = orch.service.job_repo

    async def _seed_active_job() -> None:
        await repo.create(
            IndexingJobRecord(
                job_id="IDX-EXISTING-001",
                domain_id="security",
                doc_id="DOC-FIX-001",
                doc_version="v1",
                status="parsing",
                filename="/tmp/x",
            )
        )

    asyncio.new_event_loop().run_until_complete(_seed_active_job())

    with TestClient(app) as client:
        second = client.post(
            "/api/security/admin/documents/upload",
            files={"file": ("a.txt", io.BytesIO(b"x2"), "text/plain")},
            data={"doc_id": "DOC-FIX-001", "title": "A", "version": "v1"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert second.status_code == 409, second.text
    err = second.json()["detail"]
    assert err["error"] == "indexing_job_active"
    assert err["existing_job_id"] == "IDX-EXISTING-001"


def test_reindex_404_on_unknown_document():
    """존재하지 않는 doc_id로 reindex 시 404."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/documents/UNKNOWN/reindex",
            json={"mode": "full"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "document_not_found"


def test_reindex_after_upload_returns_202():
    """upload로 등록된 문서에 대한 embedding_only reindex는 새 job_id로 202 반환."""
    with TestClient(app) as client:
        up = client.post(
            "/api/security/admin/documents/upload",
            files={"file": ("a.txt", io.BytesIO(b"reindex test content"), "text/plain")},
            data={"doc_id": "DOC-RX-001", "title": "Reindex", "version": "v1"},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert up.status_code == 202, up.text
        upload_job = up.json()["job_id"]

        rx = client.post(
            "/api/security/admin/documents/DOC-RX-001/reindex",
            json={"mode": "embedding_only"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert rx.status_code == 202, rx.text
    body = rx.json()
    assert body["job_id"].startswith("IDX-")
    assert body["job_id"] != upload_job
    assert body["mode"] == "embedding_only"


def test_upload_rejects_invalid_metadata_json():
    """metadata가 JSON이 아니면 422."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/documents/upload",
            files={"file": ("a.txt", io.BytesIO(b"x"), "text/plain")},
            data={"title": "A", "version": "v1", "metadata": "not-json"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "metadata_not_json"


def test_get_job_returns_404_when_unknown():
    """존재하지 않는 job_id 조회 → 404."""
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/indexing/jobs/IDX-NOPE",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "job_not_found"
