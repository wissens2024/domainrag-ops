"""GET /documents + /{doc_id} + PATCH /approval — admin endpoint e2e (ADR-017 §6.2·6.3·6.5)."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.deps import (
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_document_approval_service,
    reset_evaluation_orchestrator,
    reset_indexing_orchestrator,
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
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_evaluation_orchestrator()
    reset_document_approval_service()
    yield
    app.dependency_overrides.clear()
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_evaluation_orchestrator()
    reset_document_approval_service()


def _upload(client: TestClient, *, doc_id: str, title: str, content: bytes = b"hello indexing world"):
    return client.post(
        "/api/security/admin/documents/upload",
        files={"file": (f"{doc_id}.txt", io.BytesIO(content), "text/plain")},
        data={"doc_id": doc_id, "title": title, "version": "v1",
              "metadata": '{"acl":["group:security"],"approval_status":"approved"}'},
        headers={"Authorization": "Bearer mock-token"},
    )


def test_list_documents_returns_uploaded_items():
    with TestClient(app) as client:
        for did, title in [("DOC-A", "문서 A"), ("DOC-B", "문서 B")]:
            assert _upload(client, doc_id=did, title=title).status_code == 202

        resp = client.get(
            "/api/security/admin/documents",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    doc_ids = {i["doc_id"] for i in items}
    assert {"DOC-A", "DOC-B"}.issubset(doc_ids)


def test_list_documents_filters_by_keyword():
    with TestClient(app) as client:
        _upload(client, doc_id="DOC-PASSWORD", title="패스워드 정책")
        _upload(client, doc_id="DOC-MISC", title="기타 가이드")
        resp = client.get(
            "/api/security/admin/documents?keyword=password",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    ids = [i["doc_id"] for i in resp.json()["items"]]
    assert "DOC-PASSWORD" in ids
    assert "DOC-MISC" not in ids


def test_get_document_returns_meta_and_chunks_summary():
    with TestClient(app) as client:
        _upload(client, doc_id="DOC-G", title="get test", content=b"some chunk text content for indexing")
        resp = client.get(
            "/api/security/admin/documents/DOC-G",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["doc_id"] == "DOC-G"
    assert body["version"] == "v1"
    assert body["chunks_summary"]["total"] >= 1
    assert "approval_status_distribution" in body["chunks_summary"]


def test_get_document_404_when_missing():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/documents/NOPE",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "document_not_found"


def test_patch_approval_updates_doc_chunks_and_payload():
    """approval_status=archived 전이 시 documents/chunks/Qdrant payload 모두 동기화."""
    from app.core.config import get_settings
    from app.deps import get_indexing_orchestrator

    with TestClient(app) as client:
        _upload(client, doc_id="DOC-AP", title="approval test", content=b"approval chunk text")

        resp = client.patch(
            "/api/security/admin/documents/DOC-AP/approval",
            json={"approval_status": "archived", "version": "v1",
                  "reason": "deprecated by 2026-Q3 policy"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approval_status"] == "archived"
    assert body["affected_chunks"] >= 1

    # Qdrant payload 검증
    orch = get_indexing_orchestrator(get_settings())
    vs = orch.service.vector_store
    points = vs._collections["security"].points  # type: ignore[attr-defined]
    statuses = {p.payload.get("approval_status") for p in points.values()}
    assert "archived" in statuses


def test_patch_approval_404_when_doc_missing():
    with TestClient(app) as client:
        resp = client.patch(
            "/api/security/admin/documents/UNKNOWN/approval",
            json={"approval_status": "approved", "version": "v1"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "document_not_found"


def test_list_documents_status_filter():
    """approval_status='archived' 필터로 archived만 반환."""
    with TestClient(app) as client:
        _upload(client, doc_id="DOC-X", title="X")
        _upload(client, doc_id="DOC-Y", title="Y")
        client.patch(
            "/api/security/admin/documents/DOC-Y/approval",
            json={"approval_status": "archived", "version": "v1"},
            headers={"Authorization": "Bearer mock-token"},
        )
        resp = client.get(
            "/api/security/admin/documents?approval_status=archived",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    ids = [i["doc_id"] for i in resp.json()["items"]]
    assert ids == ["DOC-Y"]
