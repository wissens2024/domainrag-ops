"""GET /api/{tenant_id}/admin/dashboard — endpoint e2e (ADR-017 §10).

env: AUTH_MODE=mock + RAG_BACKEND=inmemory.
InMemory backend라 실제 chat + upload 호출로 in-memory state 시드 → endpoint 검증.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
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


def _send_chat(client: TestClient, question: str) -> None:
    resp = client.post(
        "/api/security/chat",
        json={"conversation_id": None, "question": question},
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp.status_code == 200, resp.text


def _upload(client: TestClient, *, doc_id: str) -> None:
    resp = client.post(
        "/api/security/admin/documents/upload",
        files={"file": (f"{doc_id}.txt", io.BytesIO(b"sample"), "text/plain")},
        data={"doc_id": doc_id, "title": doc_id, "version": "v1",
              "metadata": '{"acl":["group:security"]}'},
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp.status_code == 202, resp.text


def test_dashboard_returns_all_metrics():
    with TestClient(app) as client:
        _send_chat(client, "패스워드 정책은?")
        _send_chat(client, "VPN 설정?")
        resp = client.get(
            "/api/security/admin/dashboard",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ADR-017 §10 12 메트릭 모두 존재
    for key in (
        "total_documents", "total_chunks", "uploaded_today",
        "indexing_completed_today", "indexing_failed_today",
        "questions_today", "avg_latency_ms", "answers_without_citation",
        "negative_feedback_rate", "citation_type_distribution",
        "fallback_distribution", "routing_distribution",
    ):
        assert key in body, key
    assert body["tenant_id"] == "security"
    # 2 chat 호출 — questions_today ≥ 2 (RAG가 fallback 분기 가능하므로 ≥)
    assert body["questions_today"] >= 2
    # routing decision은 graph가 매 응답 채움 — distribution에 최소 1 entry
    assert sum(body["routing_distribution"].values()) >= 2


def test_dashboard_counts_uploaded_documents():
    with TestClient(app) as client:
        _upload(client, doc_id="DOC-DASH-1")
        _upload(client, doc_id="DOC-DASH-2")
        resp = client.get(
            "/api/security/admin/dashboard",
            headers={"Authorization": "Bearer mock-token"},
        )
    body = resp.json()
    assert body["total_documents"] >= 2
    assert body["uploaded_today"] >= 2


def test_dashboard_requires_admin():
    app.dependency_overrides[get_user_context] = _non_admin_user
    with TestClient(app) as client:
        resp = client.get("/api/security/admin/dashboard")
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "insufficient_role"


def test_dashboard_isolates_tenant_state():
    """InMemoryDashboardAnalytics가 tenant_id 필터링하는지 검증.

    security tenant에서 chat 2건 호출 후, legal tenant 대시보드 호출 — questions_today=0.
    legal admin은 platform_admin이 아니므로 실제 운영에선 tenant_mismatch 403이지만,
    여기서는 dependency_override로 legal admin context를 강제 주입하고 path도 legal.
    """
    def _legal_admin() -> UserContext:
        return UserContext(
            user_id="legal-admin", tenant_id="legal",
            roles=["USER", "ADMIN"], clearance="confidential",
        )

    app.dependency_overrides[get_user_context] = _legal_admin
    with TestClient(app) as client:
        # security tenant 데이터 시드 (다른 user context로 강제 호출 — admin context로도 가능)
        _send_chat_other = client.post(
            "/api/security/chat",
            json={"conversation_id": None, "question": "security 질문"},
            headers={"Authorization": "Bearer mock-token"},
        )
        # legal admin path 호출 — legal에는 데이터 없음
        resp = client.get(
            "/api/legal/admin/dashboard",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == "legal"
    assert body["questions_today"] == 0
    assert body["total_documents"] == 0
