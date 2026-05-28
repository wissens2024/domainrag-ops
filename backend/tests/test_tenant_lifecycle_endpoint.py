"""ADR-008/012 + ADR-017 §18 — Platform admin tenant lifecycle e2e."""

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
    reset_tenant_lifecycle_service,
)
from app.main import app
from app.services.ledger_audit_service import LedgerAuditService
from app.services.ledger_client import NoopLedgerClient


def _platform_admin() -> UserContext:
    return UserContext(
        user_id="platform-admin-001",
        domain_id="platform",
        roles=["PLATFORM_ADMIN"],
        clearance="secret",
    )


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
    reset_tenant_lifecycle_service()
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
    reset_tenant_lifecycle_service()


def test_register_tenant_returns_201_and_creates_collection():
    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/admin/tenants",
            json={
                "domain_id": "legal",
                "display_name": "법무 도메인",
                "domain_type": "legal",
                "modules": ["rag"],
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["domain_id"] == "legal"
    assert body["status"] == "active"

    # vector store에 collection 생성됨
    from app.core.config import get_settings
    from app.deps import get_indexing_orchestrator

    orch = get_indexing_orchestrator(get_settings())
    vs = orch.service.vector_store
    assert "legal" in vs._collections  # type: ignore[attr-defined]


def test_register_duplicate_returns_409():
    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        client.post(
            "/api/platform/admin/tenants",
            json={"domain_id": "legal", "display_name": "법무"},
        )
        resp = client.post(
            "/api/platform/admin/tenants",
            json={"domain_id": "legal", "display_name": "법무 v2"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "tenant_already_exists"


def test_get_tenant_404_when_missing():
    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        resp = client.get("/api/platform/admin/tenants/ghost")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "tenant_not_found"


def test_list_tenants_filters_by_status():
    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        client.post("/api/platform/admin/tenants",
                    json={"domain_id": "t-a", "display_name": "A"})
        client.post("/api/platform/admin/tenants",
                    json={"domain_id": "t-b", "display_name": "B"})
        client.patch("/api/platform/admin/tenants/t-b",
                     json={"status": "suspended", "reason": "점검"})
        resp = client.get("/api/platform/admin/tenants?status=suspended")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert {t["domain_id"] for t in items} == {"t-b"}


def test_status_transition_active_to_archived_via_suspended():
    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        client.post("/api/platform/admin/tenants",
                    json={"domain_id": "t-1", "display_name": "T1"})

        # active → suspended
        r1 = client.patch("/api/platform/admin/tenants/t-1",
                          json={"status": "suspended"})
        assert r1.status_code == 200 and r1.json()["status"] == "suspended"

        # suspended → archived
        r2 = client.patch("/api/platform/admin/tenants/t-1",
                          json={"status": "archived"})
        assert r2.status_code == 200 and r2.json()["status"] == "archived"

        # archived → active (복구)
        r3 = client.patch("/api/platform/admin/tenants/t-1",
                          json={"status": "active"})
        assert r3.status_code == 200 and r3.json()["status"] == "active"


def test_invalid_status_transition_returns_400():
    """active → deleted 직접 전이는 금지."""
    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        client.post("/api/platform/admin/tenants",
                    json={"domain_id": "t-2", "display_name": "T2"})
        resp = client.patch("/api/platform/admin/tenants/t-2",
                            json={"status": "deleted"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_status_transition"


def test_hard_delete_only_allowed_for_archived():
    """active tenant에 hard delete 요청 → 400."""
    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        client.post("/api/platform/admin/tenants",
                    json={"domain_id": "t-3", "display_name": "T3"})
        resp = client.request(
            "DELETE",
            "/api/platform/admin/tenants/t-3/hard",
            json={"reason": "compliance"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "tenant_must_be_archived"


def test_hard_delete_succeeds_after_archived_and_drops_collection():
    import app.deps as deps
    from app.core.config import get_settings
    from app.deps import get_indexing_orchestrator

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)
    deps._tenant_lifecycle_service = None  # 재빌드해 ledger 주입

    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        client.post("/api/platform/admin/tenants",
                    json={"domain_id": "t-4", "display_name": "T4"})
        client.patch("/api/platform/admin/tenants/t-4",
                     json={"status": "archived"})
        resp = client.request(
            "DELETE",
            "/api/platform/admin/tenants/t-4/hard",
            json={"reason": "compliance"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deleted"
    assert body["delete_status"] in {"completed", "partial"}

    # vector store collection 사라짐
    orch = get_indexing_orchestrator(get_settings())
    vs = orch.service.vector_store
    assert "t-4" not in vs._collections  # type: ignore[attr-defined]

    # Ledger: tenant_hard_deleted publish
    hard = [
        e for e in noop.published
        if e.event_type == "platform_admin_action"
        and e.details.get("action") == "tenant_hard_deleted"
    ]
    assert len(hard) == 1
    assert hard[0].domain_id == "t-4"


def test_register_publishes_platform_admin_action():
    import app.deps as deps

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)
    deps._tenant_lifecycle_service = None

    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        client.post("/api/platform/admin/tenants",
                    json={"domain_id": "t-5", "display_name": "T5"})
    actions = [
        e for e in noop.published
        if e.event_type == "platform_admin_action"
        and e.details.get("action") == "tenant_registered"
    ]
    assert len(actions) == 1
    assert actions[0].details["display_name"] == "T5"


def test_non_platform_admin_cannot_register():
    """일반 ADMIN(tenant scope)는 platform_admin endpoint 호출 시 403."""
    def _regular_admin():
        return UserContext(
            user_id="u",
            domain_id="security",
            roles=["USER", "ADMIN"],
            clearance="confidential",
        )

    app.dependency_overrides[get_user_context] = _regular_admin
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/admin/tenants",
            json={"domain_id": "x", "display_name": "X"},
        )
    assert resp.status_code == 403
