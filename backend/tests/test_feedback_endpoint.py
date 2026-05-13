"""POST /api/{tenant_id}/feedback — endpoint e2e (ADR-017 §5).

env: AUTH_MODE=mock + RAG_BACKEND=inmemory. mock default_user는
user_id=dev-user-001, tenant_id=security.

확인 항목:
  - 본인 message에 feedback 적용 → 204 + chat_logs.feedback 업데이트
  - 타인 message (cross-user) → 404 message_not_found
  - unknown message_id → 404
  - dashboard negative_feedback_rate가 bad 비율로 반영되는지 결선
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
    reset_dashboard_analytics,
    reset_feedback_writer,
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
    reset_feedback_writer()
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
    reset_feedback_writer()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_ledger_audit_service()


def _send_chat(client: TestClient, question: str) -> str:
    resp = client.post(
        "/api/security/chat",
        json={"conversation_id": None, "question": question},
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["message_id"]


def _post_feedback(client: TestClient, *, message_id: str, value: str, comment=None):
    body = {"message_id": message_id, "feedback": value}
    if comment:
        body["comment"] = comment
    return client.post(
        "/api/security/feedback",
        json=body,
        headers={"Authorization": "Bearer mock-token"},
    )


def test_feedback_204_and_chat_log_updated():
    """본인 message에 feedback 'good' → 204, chat_logs.feedback 업데이트 검증."""
    from app.core.config import get_settings

    with TestClient(app) as client:
        mid = _send_chat(client, "패스워드 정책은?")
        resp = _post_feedback(client, message_id=mid, value="good", comment="유용함")
    assert resp.status_code == 204

    rag = get_rag_service(get_settings())
    writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
    target = next(r for r in writer.records if r.request_id == mid)
    assert target.feedback == "good"
    assert target.feedback_comment == "유용함"


def test_feedback_message_id_equals_request_id():
    """ADR-017 §5 — message_id == request_id 통합 검증."""
    with TestClient(app) as client:
        mid = _send_chat(client, "감사 절차?")
    # message_id는 32자 hex (uuid4().hex) — 별도 IDs였다면 다른 길이일 수도 있으나 통합 후 동일
    assert len(mid) == 32
    assert mid.isalnum()


def test_feedback_cross_user_returns_404():
    """다른 user의 message에는 feedback 못 단다."""
    # 다른 user로 메시지 생성 (override로 user 교체)
    def _other_user() -> UserContext:
        return UserContext(
            user_id="other-user", tenant_id="security",
            roles=["USER"], clearance="confidential",
        )

    app.dependency_overrides[get_user_context] = _other_user
    with TestClient(app) as client:
        mid = _send_chat(client, "다른 사용자 질문")
    # mock 기본 user로 돌아가서 feedback 시도
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        resp = _post_feedback(client, message_id=mid, value="bad")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "message_not_found"


def test_feedback_unknown_message_returns_404():
    with TestClient(app) as client:
        resp = _post_feedback(client, message_id="nonexistent-id", value="good")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "message_not_found"


def test_feedback_validation_bad_value():
    """feedback 값이 good/bad 외 → 422."""
    with TestClient(app) as client:
        mid = _send_chat(client, "정책")
        resp = client.post(
            "/api/security/feedback",
            json={"message_id": mid, "feedback": "meh"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422


def test_dashboard_negative_feedback_rate_reflects_bad():
    """feedback='bad' 1건 / total chat 2건 → 0.5."""
    with TestClient(app) as client:
        m1 = _send_chat(client, "질문 A")
        _send_chat(client, "질문 B")
        assert _post_feedback(client, message_id=m1, value="bad").status_code == 204
        resp = client.get(
            "/api/security/admin/dashboard",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["questions_today"] == 2
    assert body["negative_feedback_rate"] == 0.5
