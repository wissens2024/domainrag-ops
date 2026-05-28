"""POST /api/{domain_id}/chat — chat_structured slice 통합 테스트.

env 고정: AUTH_MODE=mock + RAG_BACKEND=inmemory (conftest.py).
DB·Qdrant·vLLM 의존 없이 동작해야 한다.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_chat_returns_structured_answer():
    # 싱글턴 RAGService를 reset해서 매 테스트마다 InMemory seed가 깨끗이 시작.
    from app.deps import reset_rag_service

    reset_rag_service()
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/chat",
            json={
                "conversation_id": None,
                "question": "패스워드 정책은?",
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success", body
    assert body["conversation_id"]
    assert body["message_id"]
    assert body["answer"]
    assert isinstance(body["citations"], list)
    assert len(body["citations"]) >= 1
    # ADR-017 §3.1 citation 객체 핵심 필드 + verifier 결선 결과
    cite = body["citations"][0]
    assert cite["domain_id"] == "security"
    assert cite["marker"].startswith("[")
    assert cite["claim_text"]
    assert cite["support_level"] in {"strong", "medium"}
    assert cite["verified"] is True
    assert cite["similarity"] is not None
    # metadata
    meta = body["metadata"]
    assert meta["ui_mode"] == "chat_structured"
    assert "latency_ms" in meta
    assert "gate1_metrics" in meta
    assert meta["confidence"] >= 0.3
    assert meta["verifier"]["tier1_markers_removed"] == 0
    assert meta["verifier"]["tier2_avg_similarity"] > 0.0
    assert isinstance(meta["citation_types"], list)


def test_chat_health_endpoint_works():
    """/api/health는 인증 불필요 — 200."""
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
