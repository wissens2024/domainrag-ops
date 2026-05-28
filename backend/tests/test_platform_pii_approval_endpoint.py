"""POST/GET/DELETE /api/platform/admin/tenants/{domain_id}/pii-storage-approvals
+ chat 흐름이 승인 상태를 반영하는지 검증 (ADR-020 §4).

env: AUTH_MODE=mock + RAG_BACKEND=inmemory (conftest.py).
platform_admin 권한이 필요한 endpoint들은 dependency_override로 PLATFORM_ADMIN role을
부여한 UserContext를 주입한다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
from app.deps import (
    get_pii_approval_service,
    reset_indexing_orchestrator,
    reset_pii_approval_service,
    reset_rag_service,
)
from app.main import app


def _platform_admin_user() -> UserContext:
    return UserContext(
        user_id="platform-admin-001",
        domain_id="platform",
        roles=["PLATFORM_ADMIN"],
        clearance="secret",
    )


def _regular_user() -> UserContext:
    return UserContext(
        user_id="dev-user-001",
        domain_id="security",
        roles=["USER", "ADMIN"],
        clearance="confidential",
    )


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    yield
    app.dependency_overrides.clear()
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()


def test_approve_plain_returns_201_and_records_audit():
    """platform_admin이 plain 보관 정책을 승인하면 201 + active status + audit event."""
    app.dependency_overrides[get_user_context] = _platform_admin_user

    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/admin/tenants/security/pii-storage-approvals",
            json={"reason": "보안 사고 조사 — 2026-Q2"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["domain_id"] == "security"
    assert body["policy"] == "plain"
    assert body["status"] == "active"
    assert body["approved_by"] == "platform-admin-001"
    assert body["reason"].startswith("보안 사고")

    # audit 기록 검증 (InMemory variant는 audit_events list 노출)
    from app.core.config import get_settings

    settings = get_settings()
    approval_svc = get_pii_approval_service(settings)
    assert any(
        e["action"] == "pii_storage_plain_approved" and e["domain_id"] == "security"
        for e in approval_svc.audit_events
    )


def test_approve_returns_409_on_duplicate_active():
    """이미 active 승인이 있는 tenant에 다시 승인 요청 → 409."""
    app.dependency_overrides[get_user_context] = _platform_admin_user

    with TestClient(app) as client:
        first = client.post(
            "/api/platform/admin/tenants/security/pii-storage-approvals",
            json={"reason": "초기 승인"},
        )
        assert first.status_code == 201, first.text
        second = client.post(
            "/api/platform/admin/tenants/security/pii-storage-approvals",
            json={"reason": "중복 승인 시도"},
        )
    assert second.status_code == 409, second.text
    err = second.json()["detail"]
    assert err["error"] == "approval_active"
    assert err["existing_id"] == first.json()["approval_id"]


def test_revoke_active_returns_200_and_records_audit():
    """active 승인이 있는 상태에서 revoke 호출 → 200 + status=revoked + audit event."""
    app.dependency_overrides[get_user_context] = _platform_admin_user

    with TestClient(app) as client:
        client.post(
            "/api/platform/admin/tenants/security/pii-storage-approvals",
            json={"reason": "초기 승인"},
        )
        revoke = client.request(
            "DELETE",
            "/api/platform/admin/tenants/security/pii-storage-approvals/active",
            json={"reason": "사고 종료 — 마스킹 복귀"},
        )
    assert revoke.status_code == 200, revoke.text
    body = revoke.json()
    assert body["status"] == "revoked"
    assert body["revoked_by"] == "platform-admin-001"
    assert body["revoke_reason"].startswith("사고 종료")

    from app.core.config import get_settings

    approval_svc = get_pii_approval_service(get_settings())
    assert any(
        e["action"] == "pii_storage_plain_revoked" and e["domain_id"] == "security"
        for e in approval_svc.audit_events
    )


def test_revoke_returns_404_when_no_active_approval():
    app.dependency_overrides[get_user_context] = _platform_admin_user
    with TestClient(app) as client:
        resp = client.request(
            "DELETE",
            "/api/platform/admin/tenants/security/pii-storage-approvals/active",
            json={"reason": "x"},
        )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "no_active_approval"


def test_regular_admin_user_cannot_approve():
    """일반 ADMIN(tenant scope) 사용자는 platform endpoint 호출 시 403."""
    app.dependency_overrides[get_user_context] = _regular_user
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/admin/tenants/security/pii-storage-approvals",
            json={"reason": "권한 시도"},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "insufficient_role"


def test_list_returns_history_in_recent_first_order():
    app.dependency_overrides[get_user_context] = _platform_admin_user
    with TestClient(app) as client:
        client.post(
            "/api/platform/admin/tenants/security/pii-storage-approvals",
            json={"reason": "1차 승인"},
        )
        client.request(
            "DELETE",
            "/api/platform/admin/tenants/security/pii-storage-approvals/active",
            json={"reason": "1차 회수"},
        )
        client.post(
            "/api/platform/admin/tenants/security/pii-storage-approvals",
            json={"reason": "2차 승인"},
        )
        resp = client.get(
            "/api/platform/admin/tenants/security/pii-storage-approvals",
        )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 2
    # InMemory variant의 list는 history reversed — active(2차)가 먼저, revoked(1차)가 뒤
    assert items[0]["status"] == "active"
    assert items[0]["reason"] == "2차 승인"
    assert items[1]["status"] == "revoked"


def test_chat_uses_mask_until_approval_granted():
    """ADR-020 §4 — tenant_config가 plain 정책이라도 승인 없으면 chat_logs에 mask 적용.

    승인 후에는 reset_rag_service로 새 RAGService가 생성되며 plain 적용을 검증한다.
    """
    # tenant config를 plain으로 강제하는 patch — TenantConfigService.load 결과에 storage.plain 주입.
    from app.core.config import get_settings
    from app.deps import get_rag_service
    from app.services import rag_service as rag_service_module

    settings = get_settings()
    approval_svc = get_pii_approval_service(settings)
    rag = get_rag_service(settings)
    # config_loader를 가로채 storage.pii_storage_policy='plain' 강제 (plain_approved는
    # _build_inmemory_service 내부 loader가 approval_svc로 결정).
    original_loader = rag._deps.config_loader  # type: ignore[attr-defined]

    async def _patched(domain_id: str):
        cfg = original_loader(domain_id)
        if hasattr(cfg, "__await__"):
            cfg = await cfg
        cfg.setdefault("pii", {}).setdefault("storage", {})[
            "pii_storage_policy"
        ] = "plain"
        return cfg

    rag._deps.config_loader = _patched  # type: ignore[attr-defined]
    # graph는 이미 compiled — config_loader는 노드 호출 시점에 deps에서 lookup되므로 OK.

    # 1) 승인 전 — plain 정책이어도 mask 적용
    app.dependency_overrides[get_user_context] = _regular_user
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/chat",
            json={
                "conversation_id": None,
                "question": "패스워드 정책은? 문의는 user@example.com",
            },
        )
    assert resp.status_code == 200
    log_writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
    assert log_writer.records[-1].pii_storage_policy == "mask"

    # 2) platform_admin 승인 후 — plain 적용
    import asyncio

    asyncio.new_event_loop().run_until_complete(
        approval_svc.approve_plain(
            domain_id="security",
            reason="조사",
            approved_by="platform-admin-001",
        )
    )
    with TestClient(app) as client:
        resp2 = client.post(
            "/api/security/chat",
            json={
                "conversation_id": None,
                "question": "패스워드 정책은? 문의는 user@example.com",
            },
        )
    assert resp2.status_code == 200
    assert log_writer.records[-1].pii_storage_policy == "plain"
