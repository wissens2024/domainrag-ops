"""DELETE /api/{domain_id}/me/chat_logs — endpoint e2e (ADR-020 §10).

env: AUTH_MODE=mock + RAG_BACKEND=inmemory. mock default_user는 user_id=dev-user-001,
domain_id=security, roles=USER/ADMIN.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
from app.deps import (
    get_rag_service,
    reset_chat_log_eraser,
    reset_indexing_orchestrator,
    reset_pii_approval_service,
    reset_rag_service,
)
from app.main import app


def _service_account_user() -> UserContext:
    return UserContext(
        user_id="service-001",
        domain_id="security",
        roles=["SERVICE", "INDEXER"],
        clearance="secret",
    )


@pytest.fixture(autouse=True)
def _reset():
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    yield
    app.dependency_overrides.clear()
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()


def _send_chat(client: TestClient, question: str) -> None:
    resp = client.post(
        "/api/security/chat",
        json={"conversation_id": None, "question": question},
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp.status_code == 200, resp.text


def test_erase_default_mode_masks_user_logs_preserving_metrics():
    """기본 mode=mask_only — question/answer/citations 마스킹, latency/routing 보존."""
    from app.core.config import get_settings

    with TestClient(app) as client:
        _send_chat(client, "패스워드 정책은?")
        _send_chat(client, "보안 가이드?")

        resp = client.request(
            "DELETE",
            "/api/security/me/chat_logs",
            json={"reason": "personal data request"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "mask_only"
    assert body["affected_rows"] == 2
    assert body["domain_id"] == "security"

    # 직접 InMemoryChatLogWriter 검증
    rag = get_rag_service(get_settings())
    writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
    for r in writer.records:
        if r.domain_id == "security":
            assert r.question is None
            assert r.answer is None
            assert r.citations == []
            assert r.retrieved_chunks == []
            assert r.pii_storage_policy == "erased"
            # 운영 지표 보존 (routing_decision은 graph에서 채워진다)
            assert r.routing_decision


def test_erase_hard_delete_removes_rows():
    from app.core.config import get_settings

    with TestClient(app) as client:
        _send_chat(client, "정책 1")
        _send_chat(client, "정책 2")
        resp = client.request(
            "DELETE",
            "/api/security/me/chat_logs",
            json={"mode": "hard_delete", "reason": "purge"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "hard_delete"
    assert resp.json()["affected_rows"] == 2

    rag = get_rag_service(get_settings())
    writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
    assert all(
        not (r.domain_id == "security" and r.user_id == "dev-user-001")
        for r in writer.records
    )


def test_service_account_cannot_erase():
    app.dependency_overrides[get_user_context] = _service_account_user
    with TestClient(app) as client:
        resp = client.request(
            "DELETE",
            "/api/security/me/chat_logs",
            json={"reason": "x"},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "service_account_cannot_erase"


def test_other_users_logs_remain_after_erase():
    """다른 user_id의 logs는 영향 없음."""
    from app.core.config import get_settings
    from rag_core.services.chat_log_writer import ChatLogPayload

    rag = get_rag_service(get_settings())
    writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]

    # 다른 user의 log 1건 사전 삽입
    import asyncio

    asyncio.new_event_loop().run_until_complete(
        writer.write(
            ChatLogPayload(
                domain_id="security",
                request_id="req-other",
                user_id="other-user",
                conversation_id=None,
                question="다른 사용자 질문",
                answer="다른 사용자 답변",
            )
        )
    )

    with TestClient(app) as client:
        _send_chat(client, "내 질문")
        resp = client.request(
            "DELETE",
            "/api/security/me/chat_logs",
            json={"reason": "remove mine"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    # affected_rows에 다른 user는 포함 안 됨
    assert resp.json()["affected_rows"] == 1

    other = [r for r in writer.records if r.user_id == "other-user"]
    assert len(other) == 1
    assert other[0].question == "다른 사용자 질문"


def test_reason_required():
    with TestClient(app) as client:
        resp = client.request(
            "DELETE",
            "/api/security/me/chat_logs",
            json={"mode": "mask_only"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422
