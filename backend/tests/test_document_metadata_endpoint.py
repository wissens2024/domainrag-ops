"""PATCH /api/{tenant_id}/admin/documents/{doc_id} — payload-only metadata 갱신 (ADR-017 §6.4)."""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.deps import (
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_document_approval_service,
    reset_document_metadata_service,
    reset_evaluation_orchestrator,
    reset_hard_delete_service,
    reset_indexing_orchestrator,
    reset_input_schema_service,
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
    reset_hard_delete_service()
    reset_input_schema_service()
    reset_document_metadata_service()
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
    reset_hard_delete_service()
    reset_input_schema_service()
    reset_document_metadata_service()


def _upload(
    client: TestClient,
    *,
    doc_id: str,
    title: str = "테스트",
    input_type: str | None = None,
    metadata: dict | None = None,
):
    data = {
        "doc_id": doc_id,
        "title": title,
        "version": "v1",
        "metadata": json.dumps(
            metadata or {"acl": ["group:security"], "approval_status": "approved"}
        ),
    }
    if input_type:
        data["input_type"] = input_type
    return client.post(
        "/api/security/admin/documents/upload",
        files={"file": ("p.txt", io.BytesIO(b"text"), "text/plain")},
        data=data,
        headers={"Authorization": "Bearer mock-token"},
    )


def test_patch_metadata_simple_fields_returns_updated_doc():
    """title/department/tags 단순 필드 변경 + chunks/Qdrant 동기화."""
    from app.core.config import get_settings
    from app.deps import get_indexing_orchestrator

    with TestClient(app) as client:
        assert _upload(client, doc_id="DOC-M1").status_code == 202

        resp = client.patch(
            "/api/security/admin/documents/DOC-M1",
            json={
                "version": "v1",
                "patch": {
                    "title": "수정된 제목",
                    "department": "it",
                    "tags": ["revised", "ops"],
                },
                "reason": "운영 후 정정",
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"]["title"] == "수정된 제목"
    assert body["document"]["department"] == "it"
    assert body["document"]["tags"] == ["revised", "ops"]
    assert body["affected_chunks"] >= 1
    # 동기화 키는 모두 chunk-syncable
    assert set(body["synced_keys"]) == {"title", "department", "tags"}

    # Qdrant payload 검증
    orch = get_indexing_orchestrator(get_settings())
    vs = orch.service.vector_store
    points = vs._collections["security"].points  # type: ignore[attr-defined]
    for p in points.values():
        if p.payload.get("doc_id") == "DOC-M1":
            assert p.payload.get("title") == "수정된 제목"
            assert p.payload.get("department") == "it"
            assert p.payload.get("tags") == ["revised", "ops"]


def test_patch_metadata_skips_chunk_sync_for_non_syncable_keys():
    """owner/language는 chunks 컬럼에 없으므로 synced_keys에 포함되지 않음."""
    with TestClient(app) as client:
        _upload(client, doc_id="DOC-M2")
        resp = client.patch(
            "/api/security/admin/documents/DOC-M2",
            json={"version": "v1", "patch": {"owner": "bob", "language": "en"}},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"]["owner"] == "bob"
    assert body["document"]["language"] == "en"
    assert body["synced_keys"] == []  # 둘 다 chunk-syncable 아님
    assert body["affected_chunks"] == 0


def test_patch_metadata_404_when_doc_missing():
    with TestClient(app) as client:
        resp = client.patch(
            "/api/security/admin/documents/UNKNOWN",
            json={"version": "v1", "patch": {"title": "x"}},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "document_not_found"


def test_patch_metadata_400_on_empty_patch():
    with TestClient(app) as client:
        _upload(client, doc_id="DOC-M3")
        resp = client.patch(
            "/api/security/admin/documents/DOC-M3",
            json={"version": "v1", "patch": {}},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "empty_patch"


def test_patch_metadata_422_when_violates_input_schema():
    """input_type=policy_document로 업로드 → patch로 잘못된 enum 적용 시 422."""
    valid_meta = {
        "acl": ["group:security"],
        "approval_status": "approved",
        "policy_id": "SEC-POL-200",
        "policy_type": "access_control",
        "department": "security",
        "authority_rank": 50,
        "effective_date": "2026-05-01",
        "owner": "alice",
        "security_level": "internal",
    }
    with TestClient(app) as client:
        up = _upload(
            client, doc_id="DOC-M4", title="정책",
            input_type="policy_document", metadata=valid_meta,
        )
        assert up.status_code == 202, up.text

        # patch로 policy_type을 invalid enum 값으로
        resp = client.patch(
            "/api/security/admin/documents/DOC-M4",
            json={
                "version": "v1",
                "patch": {"metadata": {"policy_type": "not_a_valid_type"}},
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "input_schema_validation_failed"
    assert any(f["code"] == "enum" for f in detail["fields"])


def test_patch_metadata_passes_when_patch_keeps_required_fields_valid():
    """input_type 문서에 대해 valid한 patch는 통과 — merged validation 정합."""
    valid_meta = {
        "acl": ["group:security"],
        "approval_status": "approved",
        "policy_id": "SEC-POL-300",
        "policy_type": "access_control",
        "department": "security",
        "authority_rank": 50,
        "effective_date": "2026-05-01",
        "owner": "alice",
        "security_level": "internal",
    }
    with TestClient(app) as client:
        assert _upload(
            client, doc_id="DOC-M5", title="정책",
            input_type="policy_document", metadata=valid_meta,
        ).status_code == 202

        resp = client.patch(
            "/api/security/admin/documents/DOC-M5",
            json={
                "version": "v1",
                "patch": {
                    "tags": ["q2-review"],
                    "metadata": {"authority_rank": 70},
                },
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"]["tags"] == ["q2-review"]
    assert body["document"]["metadata"]["authority_rank"] == 70
