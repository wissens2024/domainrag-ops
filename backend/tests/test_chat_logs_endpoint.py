"""GET /api/{tenant_id}/admin/logs/chat — endpoint e2e (ADR-017 §8).

env: AUTH_MODE=mock + RAG_BACKEND=inmemory. mock default_user는
user_id=dev-user-001, tenant_id=security, roles=USER/ADMIN.

InMemory backend라 실제 chat 호출로 chat_logs을 시드한 뒤 admin endpoint를 검증한다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
from app.deps import (
    get_rag_service,
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_chat_log_reader,
    reset_indexing_orchestrator,
    reset_ledger_audit_service,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_rag_service,
)
from app.main import app


def _non_admin_user() -> UserContext:
    return UserContext(
        user_id="dev-user-001",
        tenant_id="security",
        roles=["USER"],
        clearance="confidential",
    )


def _other_tenant_admin() -> UserContext:
    """legal tenant admin — security tenant 호출 시 tenant_mismatch 403 검증용."""
    return UserContext(
        user_id="legal-admin-001",
        tenant_id="legal",
        roles=["USER", "ADMIN"],
        clearance="confidential",
    )


@pytest.fixture(autouse=True)
def _reset():
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_chat_log_reader()
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
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_ledger_audit_service()


def _seed(client: TestClient, question: str) -> str:
    resp = client.post(
        "/api/security/chat",
        json={"conversation_id": None, "question": question},
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["message_id"]


def _get_request_id_for(question: str) -> str:
    """InMemoryChatLogWriter에서 question으로 request_id 역추적 (테스트 헬퍼)."""
    from app.core.config import get_settings

    rag = get_rag_service(get_settings())
    writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
    for r in writer.records:
        if r.question == question:
            return r.request_id
    raise AssertionError(f"no chat_log seeded for question={question!r}")


def test_list_chat_logs_returns_seeded_items():
    with TestClient(app) as client:
        _seed(client, "패스워드 정책은?")
        _seed(client, "VPN 설정 방법은?")
        resp = client.get(
            "/api/security/admin/logs/chat",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 2
    assert body["page"] == 1
    assert body["page_size"] == 20
    questions = [item["question"] for item in body["items"]]
    assert "패스워드 정책은?" in questions
    assert "VPN 설정 방법은?" in questions
    # ADR-019 §2 컬럼이 dict로 노출되는지 spot check
    item = body["items"][0]
    for key in (
        "request_id", "tenant_id", "citations", "citation_types",
        "verifier_metrics", "routing_decision", "classifier_decision",
        "ui_mode", "confidence",
    ):
        assert key in item, key


def test_list_chat_logs_pagination():
    with TestClient(app) as client:
        for i in range(3):
            _seed(client, f"질문 {i}")
        page1 = client.get(
            "/api/security/admin/logs/chat?page=1&page_size=2",
            headers={"Authorization": "Bearer mock-token"},
        ).json()
        page2 = client.get(
            "/api/security/admin/logs/chat?page=2&page_size=2",
            headers={"Authorization": "Bearer mock-token"},
        ).json()
    assert page1["total"] == 3
    assert page1["page_size"] == 2
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    # 최신순 — page1의 첫 행이 마지막 seed 질문
    assert page1["items"][0]["question"] == "질문 2"
    assert page2["items"][0]["question"] == "질문 0"


def test_list_chat_logs_keyword_filter():
    with TestClient(app) as client:
        _seed(client, "패스워드 정책은?")
        _seed(client, "VPN 설정 절차")
        resp = client.get(
            "/api/security/admin/logs/chat?keyword=VPN",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["question"] == "VPN 설정 절차"


def test_list_chat_logs_filters_user_id():
    """다른 user의 log 1건 직접 inject 후 user_id 필터로 분리되는지 검증."""
    from app.core.config import get_settings
    from rag_core.services.chat_log_writer import ChatLogPayload

    with TestClient(app) as client:
        _seed(client, "내 질문")
        rag = get_rag_service(get_settings())
        writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
        import asyncio

        asyncio.new_event_loop().run_until_complete(
            writer.write(
                ChatLogPayload(
                    tenant_id="security",
                    request_id="req-other",
                    user_id="other-user",
                    conversation_id=None,
                    question="타인 질문",
                    answer="타인 답변",
                )
            )
        )
        resp = client.get(
            "/api/security/admin/logs/chat?user_id=other-user",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["user_id"] == "other-user"
    assert body["items"][0]["question"] == "타인 질문"


def test_get_chat_log_detail_returns_all_columns():
    with TestClient(app) as client:
        _seed(client, "감사 로그 정책")
        rid = _get_request_id_for("감사 로그 정책")
        resp = client.get(
            f"/api/security/admin/logs/chat/{rid}",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_id"] == rid
    assert body["question"] == "감사 로그 정책"
    assert body["tenant_id"] == "security"
    assert isinstance(body["citations"], list)
    assert isinstance(body["citation_types"], list)
    # ADR-013 §10 운영 메타 컬럼 노출 확인
    assert "routing_decision" in body
    assert "verifier_metrics" in body
    assert "classifier_decision" in body


def test_get_chat_log_detail_unknown_returns_404():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/logs/chat/req-nope",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "chat_log_not_found"


def test_list_requires_admin_role():
    app.dependency_overrides[get_user_context] = _non_admin_user
    with TestClient(app) as client:
        resp = client.get("/api/security/admin/logs/chat")
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "insufficient_role"


def test_list_rejects_tenant_mismatch():
    """legal admin이 security tenant 호출 → 403 tenant_mismatch + Ledger publish."""
    app.dependency_overrides[get_user_context] = _other_tenant_admin

    captured = []

    class _RecordingLedger:
        async def publish_tenant_mismatch(self, **kwargs):
            captured.append(kwargs)

        async def publish_auth_failure(self, **kwargs):
            captured.append({"_type": "auth_failure", **kwargs})

    from app import deps as deps_module

    deps_module._ledger_audit_service = _RecordingLedger()  # type: ignore[assignment]
    try:
        with TestClient(app) as client:
            resp = client.get("/api/security/admin/logs/chat")
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "tenant_mismatch"
        # Ledger publish 흔적 확인 (ADR-020 §8)
        assert any(c.get("tenant_id") == "security" for c in captured)
    finally:
        reset_ledger_audit_service()
