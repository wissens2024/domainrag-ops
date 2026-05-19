"""LoRA Registry API e2e (ADR-017 §14 + ADR-013).

5 endpoint:
  - GET    /admin/lora                              list (status 필터)
  - POST   /admin/lora/upload                       multipart weights + metadata
  - POST   /admin/lora/{aid}/activate
  - POST   /admin/lora/{aid}/retire
  - DELETE /admin/lora/{aid}
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
from app.deps import (
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_chat_log_reader,
    reset_citation_distribution,
    reset_citation_reverify_service,
    reset_conversation_repository,
    reset_dashboard_analytics,
    reset_feedback_writer,
    reset_indexing_orchestrator,
    reset_ledger_audit_service,
    reset_lora_orchestrator,
    reset_lora_registry,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_prompt_studio_service,
    reset_rag_service,
    reset_tenant_config_override_service,
)
from app.main import app


def _non_admin_user() -> UserContext:
    return UserContext(
        user_id="dev-user-001", tenant_id="security",
        roles=["USER"], clearance="confidential",
    )


@pytest.fixture(autouse=True)
def _reset():
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_chat_log_reader()
    reset_dashboard_analytics()
    reset_feedback_writer()
    reset_conversation_repository()
    reset_citation_distribution()
    reset_citation_reverify_service()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_ledger_audit_service()
    reset_tenant_config_override_service()
    reset_prompt_studio_service()
    reset_lora_registry()
    reset_lora_orchestrator()
    yield
    app.dependency_overrides.clear()
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_chat_log_reader()
    reset_dashboard_analytics()
    reset_feedback_writer()
    reset_conversation_repository()
    reset_citation_distribution()
    reset_citation_reverify_service()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_ledger_audit_service()
    reset_lora_orchestrator()
    reset_tenant_config_override_service()
    reset_prompt_studio_service()
    reset_lora_registry()


def _upload(client: TestClient, *, adapter_id: str, version: str = "v1") -> dict:
    metadata = json.dumps({
        "adapter_id": adapter_id,
        "version": version,
        "base_model": "qwen-7b",
        "training_metadata": {"dataset": "test"},
    })
    return client.post(
        "/api/security/admin/lora/upload",
        files={"weights": (f"{adapter_id}.bin", io.BytesIO(b"weights blob"), "application/octet-stream")},
        data={"metadata": metadata},
        headers={"Authorization": "Bearer mock-token"},
    )


def test_upload_creates_adapter_and_records_metadata():
    with TestClient(app) as client:
        resp = _upload(client, adapter_id="sec-lora-v1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["adapter_id"] == "sec-lora-v1"
    assert body["status"] == "registered"
    assert body["tenant_id"] == "security"
    # weights size + filename 자동 저장
    assert body["training_metadata"]["weights_size_bytes"] == len(b"weights blob")
    assert body["training_metadata"]["filename"] == "sec-lora-v1.bin"


def test_upload_conflict_409():
    with TestClient(app) as client:
        first = _upload(client, adapter_id="sec-lora-v1")
        assert first.status_code == 201
        dup = _upload(client, adapter_id="sec-lora-v1")
    assert dup.status_code == 409
    assert dup.json()["detail"]["error"] == "adapter_id_exists"


def test_upload_missing_adapter_id_422():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/lora/upload",
            files={"weights": ("x.bin", io.BytesIO(b"x"), "application/octet-stream")},
            data={"metadata": json.dumps({"version": "v1"})},  # adapter_id 누락
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422


def test_list_filters_by_status():
    with TestClient(app) as client:
        _upload(client, adapter_id="A")
        _upload(client, adapter_id="B")
        # A만 activate
        act = client.post(
            "/api/security/admin/lora/A/activate",
            headers={"Authorization": "Bearer mock-token"},
        )
        assert act.status_code == 200, act.text
        resp_active = client.get(
            "/api/security/admin/lora?status=active",
            headers={"Authorization": "Bearer mock-token"},
        )
        resp_all = client.get(
            "/api/security/admin/lora",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp_active.json()["total"] == 1
    assert resp_active.json()["items"][0]["adapter_id"] == "A"
    assert resp_all.json()["total"] == 2


def test_activate_then_retire_then_delete():
    with TestClient(app) as client:
        _upload(client, adapter_id="A")
        client.post(
            "/api/security/admin/lora/A/activate",
            headers={"Authorization": "Bearer mock-token"},
        )
        # active 상태에서 DELETE → 409
        bad_del = client.delete(
            "/api/security/admin/lora/A",
            headers={"Authorization": "Bearer mock-token"},
        )
        assert bad_del.status_code == 409
        assert bad_del.json()["detail"]["error"] == "adapter_active_cannot_delete"

        # retire
        ret = client.post(
            "/api/security/admin/lora/A/retire",
            headers={"Authorization": "Bearer mock-token"},
        )
        assert ret.status_code == 200
        assert ret.json()["status"] == "retired"

        # retired → activate 거절 (400 invalid_transition)
        bad_act = client.post(
            "/api/security/admin/lora/A/activate",
            headers={"Authorization": "Bearer mock-token"},
        )
        assert bad_act.status_code == 400
        assert bad_act.json()["detail"]["error"] == "invalid_transition"

        # delete OK
        del_resp = client.delete(
            "/api/security/admin/lora/A",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert del_resp.status_code == 204


def test_activate_unknown_404():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/lora/ghost/activate",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "adapter_not_found"


def test_retire_unknown_404():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/lora/ghost/retire",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404


def test_delete_unknown_404():
    with TestClient(app) as client:
        resp = client.delete(
            "/api/security/admin/lora/ghost",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404


def test_list_requires_admin():
    app.dependency_overrides[get_user_context] = _non_admin_user
    with TestClient(app) as client:
        resp = client.get("/api/security/admin/lora")
    assert resp.status_code == 403
