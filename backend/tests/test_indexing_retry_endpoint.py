"""POST /admin/indexing/jobs/{job_id}/retry e2e (ADR-017 §7)."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.deps import (
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_chat_log_reader,
    reset_dashboard_analytics,
    reset_indexing_orchestrator,
    reset_ledger_audit_service,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_rag_service,
)
from app.main import app


@pytest.fixture(autouse=True)
def _reset():
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_chat_log_reader()
    reset_dashboard_analytics()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_ledger_audit_service()
    yield
    app.dependency_overrides.clear()
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_chat_log_reader()
    reset_dashboard_analytics()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_ledger_audit_service()


def _upload(client: TestClient, *, doc_id: str):
    return client.post(
        "/api/security/admin/documents/upload",
        files={"file": (f"{doc_id}.txt", io.BytesIO(b"sample"), "text/plain")},
        data={"doc_id": doc_id, "title": doc_id, "version": "v1",
              "metadata": '{"acl":["group:security"]}'},
        headers={"Authorization": "Bearer mock-token"},
    )


def test_retry_unknown_job_returns_404():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/indexing/jobs/no-such-job/retry",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404


def test_retry_completed_job_returns_400():
    """upload 직후 background job이 completed로 끝남 → retry 400."""
    with TestClient(app) as client:
        upload_resp = _upload(client, doc_id="DOC-RETRY-1")
        assert upload_resp.status_code == 202
        job_id = upload_resp.json()["job_id"]
        # background task가 끝나도록 짧게 list 호출
        client.get(
            "/api/security/admin/indexing/jobs",
            headers={"Authorization": "Bearer mock-token"},
        )
        resp = client.post(
            f"/api/security/admin/indexing/jobs/{job_id}/retry",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_status_for_retry"


def test_retry_failed_job_resets_to_pending():
    """job 상태를 직접 failed로 바꾼 뒤 retry → 202."""
    from app.core.config import get_settings
    from app.deps import get_indexing_orchestrator
    import asyncio

    with TestClient(app) as client:
        upload_resp = _upload(client, doc_id="DOC-RETRY-FAIL")
        job_id = upload_resp.json()["job_id"]

        # 직접 job state를 failed로 강제
        orch = get_indexing_orchestrator(get_settings())
        asyncio.new_event_loop().run_until_complete(
            orch.service.job_repo.update(
                job_id=job_id, status="failed", error_message="forced for test",
            )
        )

        resp = client.post(
            f"/api/security/admin/indexing/jobs/{job_id}/retry",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] == "pending"
