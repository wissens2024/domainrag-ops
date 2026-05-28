"""Citation Inspector API e2e (ADR-017 §9 + ADR-010).

3 endpoint:
  - GET  /admin/citation-inspector/distribution
  - GET  /admin/citation-inspector/segments/{message_id}
  - POST /admin/citation-inspector/reverify
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
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_rag_service,
)
from app.main import app


def _non_admin_user() -> UserContext:
    return UserContext(
        user_id="dev-user-001", domain_id="security",
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


def _send_chat(client: TestClient, question: str) -> str:
    resp = client.post(
        "/api/security/chat",
        json={"conversation_id": None, "question": question},
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["message_id"]


def test_distribution_returns_all_bucket_with_counts():
    """InMemory에서는 단일 'all' 버킷 + citation_type별 카운트."""
    with TestClient(app) as client:
        _send_chat(client, "패스워드 정책")
        _send_chat(client, "VPN 설정")
        resp = client.get(
            "/api/security/admin/citation-inspector/distribution",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["granularity"] == "all"
    assert body["total_messages"] >= 2
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["bucket"] == "all"
    # graph가 direct citation을 생성 — counts에 'direct' 있어야 함
    assert "direct" in body["buckets"][0]["counts"]


def test_distribution_requires_admin():
    app.dependency_overrides[get_user_context] = _non_admin_user
    with TestClient(app) as client:
        resp = client.get("/api/security/admin/citation-inspector/distribution")
    assert resp.status_code == 403


def test_segments_returns_claim_chunk_mapping():
    with TestClient(app) as client:
        mid = _send_chat(client, "패스워드 정책")
        resp = client.get(
            f"/api/security/admin/citation-inspector/segments/{mid}",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message_id"] == mid
    assert body["domain_id"] == "security"
    assert body["question"] == "패스워드 정책"
    # ADR-010 §4 — 검증된 citation은 claim_text + chunk_id + support_level + similarity
    assert isinstance(body["citations"], list)
    assert isinstance(body["citations_by_type"], dict)
    assert "direct" in body["citations_by_type"]
    first = body["citations_by_type"]["direct"][0]
    for k in ("claim_text", "chunk_id", "support_level", "similarity", "excerpt"):
        assert k in first
    assert isinstance(body["verifier_metrics"], dict)


def test_segments_unknown_message_returns_404():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/citation-inspector/segments/no-such-id",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "message_not_found"


def test_reverify_updates_citations_and_returns_summary():
    """기본 chat 후 reverify — scanned/updated/avg_similarity 등 summary 반환."""
    from app.core.config import get_settings
    from app.deps import get_rag_service

    with TestClient(app) as client:
        _send_chat(client, "패스워드 정책")
        _send_chat(client, "VPN 절차")
        resp = client.post(
            "/api/security/admin/citation-inspector/reverify",
            json={"max_records": 50},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scanned"] >= 2
    assert body["updated"] >= 1
    # avg_similarity_after는 0~1 또는 None
    if body["avg_similarity_after"] is not None:
        assert 0.0 <= body["avg_similarity_after"] <= 1.0
    # citations 실제 UPDATE 확인 — verifier_metrics에 reverified_at 추가됨
    rag = get_rag_service(get_settings())
    writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
    updated_metrics = [
        r.verifier_metrics for r in writer.records
        if r.domain_id == "security" and r.verifier_metrics.get("reverified_at")
    ]
    assert len(updated_metrics) >= 1
    assert updated_metrics[0].get("reverified_by") == "dev-user-001"


def test_reverify_max_records_capped():
    """max_records > 500은 422 (Pydantic Field constraint)."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/citation-inspector/reverify",
            json={"max_records": 1000},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422
