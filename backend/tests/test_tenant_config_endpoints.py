"""ADR-009 §3·§8 + ADR-017 §11 — tenant configs endpoint e2e.

GET /configs / GET /configs/{cat} / PATCH /configs/{cat} / POST reload / GET history.
restricted_to platform_admin + breaking key Ledger publish 결선까지 검증.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
from app.deps import (
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_document_approval_service,
    reset_document_metadata_service,
    reset_evaluation_orchestrator,
    reset_hard_delete_service,
    reset_indexing_orchestrator,
    reset_input_schema_service,
    reset_ledger_audit_service,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_rag_service,
    reset_tenant_config_override_service,
)
from app.main import app
from app.services.ledger_audit_service import LedgerAuditService
from app.services.ledger_client import NoopLedgerClient


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
    reset_ledger_audit_service()
    reset_tenant_config_override_service()
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
    reset_ledger_audit_service()
    reset_tenant_config_override_service()


def test_get_all_configs_returns_categories():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/configs",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == "security"
    cats = body["categories"]
    assert "citation" in cats
    assert "retrieval" in cats
    assert "pii" in cats


def test_get_config_category_returns_subset():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/configs/citation",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["category"] == "citation"
    assert isinstance(resp.json()["value"], dict)


def test_get_config_unknown_category_404():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/configs/nope",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "unknown_category"


def test_patch_non_restricted_key_succeeds():
    """일반 ADMIN이 restricted_to 아닌 키 patch — 200 + history 적재."""
    with TestClient(app) as client:
        resp = client.patch(
            "/api/security/admin/configs/retrieval",
            json={
                "key": "top_k.fused",
                "value": 60,
                "reason": "더 넓은 후보 풀 시도",
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["category"] == "retrieval"
    assert body["key"] == "top_k.fused"
    assert body["new_value"] == 60
    assert body["changed_by"] == "dev-user-001"

    # history endpoint
    with TestClient(app) as client:
        hist = client.get(
            "/api/security/admin/configs/retrieval/history",
            headers={"Authorization": "Bearer mock-token"},
        )
    items = hist.json()["items"]
    assert len(items) == 1
    assert items[0]["path"] == "top_k.fused"
    assert items[0]["new_value"] == 60


def test_patch_restricted_key_by_admin_returns_403():
    """model.endpoints.tenant_slm.base_url은 platform_admin 전용 — 일반 admin 403."""
    with TestClient(app) as client:
        resp = client.patch(
            "/api/security/admin/configs/model",
            json={
                "key": "endpoints.tenant_slm.base_url",
                "value": "http://different.local:8000/v1",
                "reason": "운영자 임의 변경 시도",
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "config_key_restricted"
    assert detail["required_role"] == "PLATFORM_ADMIN"


def test_patch_restricted_key_by_platform_admin_succeeds_and_publishes_breaking():
    """platform_admin은 restricted_to 키 변경 가능 + breaking 키이므로 Ledger publish."""
    import app.deps as deps

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)
    deps._tenant_config_override_service = None  # ledger 주입을 위해 재빌드

    def _platform_admin():
        return UserContext(
            user_id="platform-admin-001",
            tenant_id="platform",
            roles=["PLATFORM_ADMIN"],
            clearance="secret",
        )

    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        resp = client.patch(
            "/api/security/admin/configs/model",
            json={
                "key": "endpoints.tenant_slm.base_url",
                "value": "http://174.local:8000/v1",
                "reason": "scenario B 채택",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_value"] == "http://174.local:8000/v1"

    events = [
        e for e in noop.published if e.event_type == "config_change_breaking"
    ]
    assert len(events) == 1
    e = events[0]
    assert e.tenant_id == "security"
    assert e.actor == "platform-admin-001"
    assert e.details["category"] == "model"
    assert e.details["path"] == "endpoints.tenant_slm.base_url"
    assert e.details["new_value"] == "http://174.local:8000/v1"


def test_patch_non_breaking_key_does_not_publish_ledger():
    """breaking 목록에 없는 키는 publish 안 함."""
    import app.deps as deps

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)
    deps._tenant_config_override_service = None

    with TestClient(app) as client:
        resp = client.patch(
            "/api/security/admin/configs/retrieval",
            json={"key": "top_k.context", "value": 7, "reason": "tweak"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    breaking = [
        e for e in noop.published if e.event_type == "config_change_breaking"
    ]
    assert breaking == []


def test_reload_returns_204():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/configs/reload",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 204


def test_history_pagination_and_filter():
    with TestClient(app) as client:
        for i in range(3):
            client.patch(
                "/api/security/admin/configs/retrieval",
                json={"key": f"top_k.fused", "value": 50 + i, "reason": f"r{i}"},
                headers={"Authorization": "Bearer mock-token"},
            )
        resp = client.get(
            "/api/security/admin/configs/retrieval/history?page_size=2",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    # 최신부터
    assert items[0]["new_value"] == 52
    assert items[1]["new_value"] == 51
