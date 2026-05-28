"""Conversation API — `/api/{tid}/conversations*` endpoint e2e (ADR-017 §4).

env: AUTH_MODE=mock + RAG_BACKEND=inmemory. mock default_user는
user_id=dev-user-001, domain_id=security.

InMemoryConversationRepository는 InMemoryChatLogWriter.records 위에서 동작 —
실제 chat 호출로 conversation을 자동 생성한 뒤 4개 endpoint를 검증.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
from app.deps import (
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_chat_log_reader,
    reset_conversation_repository,
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
    reset_conversation_repository()
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
    reset_conversation_repository()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_ledger_audit_service()


def _send_chat(client: TestClient, question: str, *, conversation_id=None) -> dict:
    resp = client.post(
        "/api/security/chat",
        json={"conversation_id": conversation_id, "question": question},
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_list_conversations_after_chats():
    """2개 대화 자동 생성 → list 응답에 둘 다 노출."""
    with TestClient(app) as client:
        a = _send_chat(client, "패스워드 정책")
        b = _send_chat(client, "VPN 설정")  # conversation_id=None → 새 대화
        resp = client.get(
            "/api/security/conversations",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    ids = [c["id"] for c in body["items"]]
    assert a["conversation_id"] in ids
    assert b["conversation_id"] in ids


def test_get_conversation_returns_messages_in_order():
    with TestClient(app) as client:
        first = _send_chat(client, "패스워드 정책")
        cid = first["conversation_id"]
        # 같은 conversation에 후속 메시지
        _send_chat(client, "더 자세히", conversation_id=cid)
        resp = client.get(
            f"/api/security/conversations/{cid}",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == cid
    assert body["message_count"] == 2
    messages = body["messages"]
    # 메시지 4개 (user + assistant 쌍 2개)
    assert len(messages) == 4
    # 오래된 순(첫 질문 → 두 번째 질문)
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "패스워드 정책"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["citations"] is not None
    assert messages[2]["content"] == "더 자세히"


def test_get_unknown_conversation_404():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/conversations/conv-nope",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "conversation_not_found"


def test_patch_title_updates_default():
    with TestClient(app) as client:
        a = _send_chat(client, "패스워드 정책")
        cid = a["conversation_id"]
        resp = client.patch(
            f"/api/security/conversations/{cid}",
            json={"title": "보안 정책 모음"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "보안 정책 모음"


def test_patch_title_unknown_404():
    with TestClient(app) as client:
        resp = client.patch(
            "/api/security/conversations/conv-nope",
            json={"title": "ghost"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404


def test_patch_title_empty_returns_422():
    with TestClient(app) as client:
        a = _send_chat(client, "정책")
        cid = a["conversation_id"]
        resp = client.patch(
            f"/api/security/conversations/{cid}",
            json={"title": ""},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422


def test_delete_conversation_hides_from_list():
    with TestClient(app) as client:
        a = _send_chat(client, "패스워드 정책")
        cid = a["conversation_id"]
        del_resp = client.delete(
            f"/api/security/conversations/{cid}",
            headers={"Authorization": "Bearer mock-token"},
        )
        list_resp = client.get(
            "/api/security/conversations",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert del_resp.status_code == 204
    assert cid not in [c["id"] for c in list_resp.json()["items"]]


def test_delete_unknown_404():
    with TestClient(app) as client:
        resp = client.delete(
            "/api/security/conversations/conv-nope",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404


def test_other_user_cannot_access_my_conversation():
    """다른 user는 본인 conversation_id를 alice의 conversation으로 조회 못한다."""
    with TestClient(app) as client:
        a = _send_chat(client, "alice 대화")
        cid = a["conversation_id"]

    def _other() -> UserContext:
        return UserContext(
            user_id="other-user", domain_id="security",
            roles=["USER"], clearance="confidential",
        )

    app.dependency_overrides[get_user_context] = _other
    with TestClient(app) as client:
        get_resp = client.get(f"/api/security/conversations/{cid}")
        list_resp = client.get("/api/security/conversations")
    assert get_resp.status_code == 404
    # other-user의 list는 빈 결과
    assert list_resp.json()["total"] == 0
