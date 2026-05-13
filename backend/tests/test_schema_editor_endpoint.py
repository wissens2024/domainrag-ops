"""Schema Editor API e2e (ADR-017 §15 + ADR-015).

3 endpoint:
  - GET  /admin/schema                                  현재 active schema
  - PUT  /admin/schema                                  yaml 전체 교체 (optimistic lock + 호환성)
  - GET  /admin/schema/history                          schema_version 역순
"""

from __future__ import annotations

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
    reset_lora_registry,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_prompt_studio_service,
    reset_rag_service,
    reset_schema_editor_service,
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
    reset_schema_editor_service()
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
    reset_tenant_config_override_service()
    reset_prompt_studio_service()
    reset_lora_registry()
    reset_schema_editor_service()


_BASE_YAML = {
    "input_types": [
        {"name": "policy_document", "title": "정책 문서"}
    ]
}


def _put(client, *, schema_yaml, base_version=None, ui_schema_yaml=None):
    body = {"schema_yaml": schema_yaml, "base_version": base_version}
    if ui_schema_yaml is not None:
        body["ui_schema_yaml"] = ui_schema_yaml
    return client.put(
        "/api/security/admin/schema",
        json=body,
        headers={"Authorization": "Bearer mock-token"},
    )


def test_get_unmigrated_returns_404():
    """tenant 등록 후 schema 미초기화 → 404 schema_not_initialized."""
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/schema",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "schema_not_initialized"


def test_put_initial_schema_creates_version_1():
    with TestClient(app) as client:
        resp = _put(client, schema_yaml=_BASE_YAML, base_version=None)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["status"] == "active"
    assert body["deprecated_version"] is None


def test_get_returns_active_after_put():
    with TestClient(app) as client:
        _put(client, schema_yaml=_BASE_YAML, base_version=None)
        resp = client.get(
            "/api/security/admin/schema",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["schema_version"] == 1


def test_put_increment_with_correct_base_version():
    new_yaml = {
        "input_types": [
            {"name": "policy_document"},
            {"name": "incident_report"},
        ]
    }
    with TestClient(app) as client:
        _put(client, schema_yaml=_BASE_YAML, base_version=None)
        resp = _put(client, schema_yaml=new_yaml, base_version=1)
    assert resp.status_code == 200
    assert resp.json()["schema_version"] == 2
    assert resp.json()["deprecated_version"] == 1


def test_put_with_wrong_base_version_returns_409():
    with TestClient(app) as client:
        _put(client, schema_yaml=_BASE_YAML, base_version=None)
        resp = _put(client, schema_yaml=_BASE_YAML, base_version=99)
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["error"] == "schema_version_conflict"
    assert body["detail"]["current"] == 1
    assert body["detail"]["base"] == 99


def test_put_removing_input_type_returns_422():
    """backward compat 위반 — 기존 policy_document 제거 시도."""
    with TestClient(app) as client:
        _put(client, schema_yaml=_BASE_YAML, base_version=None)
        resp = _put(client, schema_yaml={
            "input_types": [{"name": "incident_report"}]
        }, base_version=1)
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["error"] == "schema_invalid"
    assert any("input_type_removed: policy_document" in e for e in body["detail"]["errors"])


def test_put_missing_input_types_returns_422():
    with TestClient(app) as client:
        resp = _put(client, schema_yaml={"unrelated_field": 1}, base_version=None)
    assert resp.status_code == 422
    assert "input_types_missing" in resp.json()["detail"]["errors"]


def test_put_input_type_without_name_returns_422():
    with TestClient(app) as client:
        resp = _put(client, schema_yaml={
            "input_types": [{"title": "no name"}]
        }, base_version=None)
    assert resp.status_code == 422
    assert any("missing_name" in e for e in resp.json()["detail"]["errors"])


def test_history_returns_active_and_deprecated_versions():
    with TestClient(app) as client:
        _put(client, schema_yaml=_BASE_YAML, base_version=None)
        _put(client, schema_yaml=_BASE_YAML, base_version=1)
        _put(client, schema_yaml=_BASE_YAML, base_version=2)
        resp = client.get(
            "/api/security/admin/schema/history",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    versions = [r["schema_version"] for r in items]
    assert versions == [3, 2, 1]
    statuses = {r["schema_version"]: r["status"] for r in items}
    assert statuses[3] == "active"
    assert statuses[2] == "deprecated"
    assert statuses[1] == "deprecated"


def test_get_requires_admin():
    app.dependency_overrides[get_user_context] = _non_admin_user
    with TestClient(app) as client:
        resp = client.get("/api/security/admin/schema")
    assert resp.status_code == 403
