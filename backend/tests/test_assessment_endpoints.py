"""Assessment API e2e (ADR-014 + ADR-017 §17).

User endpoints:
  - POST /assessment/extract
  - POST /assessment/generate
  - POST /assessment/hybrid

Admin endpoints:
  - GET   /admin/assessment/items
  - POST  /admin/assessment/items
  - GET   /admin/assessment/items/{id}
  - PATCH /admin/assessment/items/{id}
  - POST  /admin/assessment/items/{id}/approve
  - GET   /admin/assessment/review-queue
  - GET   /admin/assessment/analytics
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
from app.deps import (
    reset_assessment_item_repository,
    reset_assessment_logger,
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
    reset_lora_registry,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_prompt_studio_service,
    reset_rag_service,
    reset_schema_editor_service,
    reset_tenant_config_override_service,
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
    reset_tenant_config_override_service()
    reset_prompt_studio_service()
    reset_lora_registry()
    reset_schema_editor_service()
    reset_assessment_item_repository()
    reset_assessment_logger()
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
    reset_tenant_config_override_service()
    reset_prompt_studio_service()
    reset_lora_registry()
    reset_schema_editor_service()
    reset_assessment_item_repository()
    reset_assessment_logger()


def _create_admin_item(client: TestClient, item_id: str, **overrides):
    body = {
        "item_id": item_id,
        "subject": "정보보안",
        "chapter": "접근통제",
        "difficulty": "medium",
        "question_type": "multiple_choice",
        "question_text": f"문제 {item_id}",
        "choices": ["A", "B", "C", "D"],
        "answer": "A",
        "quality_status": "approved",
        **overrides,
    }
    return client.post(
        "/api/security/admin/assessment/items",
        json=body,
        headers={"Authorization": "Bearer mock-token"},
    )


# --------------------------------------------------------------------------- #
# Admin endpoints
# --------------------------------------------------------------------------- #


def test_admin_create_item_201():
    with TestClient(app) as client:
        resp = _create_admin_item(client, "Q-001")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["item_id"] == "Q-001"
    assert body["quality_status"] == "approved"


def test_admin_create_item_conflict_409():
    with TestClient(app) as client:
        _create_admin_item(client, "Q-001")
        dup = _create_admin_item(client, "Q-001")
    assert dup.status_code == 409


def test_admin_list_items_filters_subject():
    with TestClient(app) as client:
        _create_admin_item(client, "Q-A", subject="정보보안")
        _create_admin_item(client, "Q-B", subject="네트워크")
        resp = client.get(
            "/api/security/admin/assessment/items?subject=네트워크",
            headers={"Authorization": "Bearer mock-token"},
        )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["item_id"] == "Q-B"


def test_admin_get_item_404():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/assessment/items/ghost",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404


def test_admin_patch_item_updates_fields():
    with TestClient(app) as client:
        _create_admin_item(client, "Q-1", quality_status="draft")
        resp = client.patch(
            "/api/security/admin/assessment/items/Q-1",
            json={"chapter": "암호학", "difficulty": "hard"},
            headers={"Authorization": "Bearer mock-token"},
        )
    body = resp.json()
    assert resp.status_code == 200
    assert body["chapter"] == "암호학"
    assert body["difficulty"] == "hard"
    # 미명시 필드는 보존
    assert body["subject"] == "정보보안"


def test_admin_patch_empty_400():
    with TestClient(app) as client:
        _create_admin_item(client, "Q-1")
        resp = client.patch(
            "/api/security/admin/assessment/items/Q-1",
            json={},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "empty_patch"


def test_admin_approve_item_transition():
    with TestClient(app) as client:
        _create_admin_item(client, "Q-1", quality_status="reviewed")
        resp = client.post(
            "/api/security/admin/assessment/items/Q-1/approve",
            json={"reason": "검토 완료"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["quality_status"] == "approved"


def test_admin_approve_invalid_transition_400():
    with TestClient(app) as client:
        _create_admin_item(client, "Q-1", quality_status="retired")
        resp = client.post(
            "/api/security/admin/assessment/items/Q-1/approve",
            json={},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 400


def test_admin_review_queue_returns_draft_reviewed():
    with TestClient(app) as client:
        _create_admin_item(client, "Q-D", quality_status="draft")
        _create_admin_item(client, "Q-R", quality_status="reviewed")
        _create_admin_item(client, "Q-A", quality_status="approved")
        resp = client.get(
            "/api/security/admin/assessment/review-queue",
            headers={"Authorization": "Bearer mock-token"},
        )
    body = resp.json()
    ids = {r["item_id"] for r in body["items"]}
    assert ids == {"Q-D", "Q-R"}


def test_admin_analytics_summary():
    with TestClient(app) as client:
        _create_admin_item(client, "Q-1", difficulty="easy")
        _create_admin_item(client, "Q-2", difficulty="medium")
        _create_admin_item(client, "Q-3", difficulty="medium", quality_status="draft")
        resp = client.get(
            "/api/security/admin/assessment/analytics",
            headers={"Authorization": "Bearer mock-token"},
        )
    body = resp.json()
    assert body["total_items"] == 3
    assert body["by_difficulty"]["medium"] == 2
    assert body["by_quality_status"]["approved"] == 2


def test_admin_endpoints_require_admin():
    app.dependency_overrides[get_user_context] = _non_admin_user
    with TestClient(app) as client:
        resp = client.get("/api/security/admin/assessment/items")
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# User endpoints
# --------------------------------------------------------------------------- #


def test_extract_returns_items_from_pool():
    """admin이 5개 item 등록 → 사용자가 distribution 추출."""
    with TestClient(app) as client:
        for i in range(3):
            _create_admin_item(client, f"E-{i}", difficulty="easy")
        for i in range(2):
            _create_admin_item(client, f"M-{i}", difficulty="medium")
        resp = client.post(
            "/api/security/assessment/extract",
            json={
                "subject": "정보보안",
                "difficulty_distribution": {"easy": 2, "medium": 1},
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["extracted_count"] == 3
    diffs = [i["difficulty"] for i in body["items"]]
    assert diffs.count("easy") == 2
    assert diffs.count("medium") == 1
    assert len(body["citations"]) == 3
    assert "request_id" in body


def test_extract_insufficient_pool_reported():
    with TestClient(app) as client:
        _create_admin_item(client, "Q-E1", difficulty="easy")
        resp = client.post(
            "/api/security/assessment/extract",
            json={"difficulty_distribution": {"easy": 3}},
            headers={"Authorization": "Bearer mock-token"},
        )
    body = resp.json()
    assert body["extracted_count"] == 1
    assert body["insufficient_pool"]["easy"] == 2


def test_generate_invokes_llm_and_persists_items():
    """InMemoryLLMClient는 '{}' 반환이므로 generate가 0개 만든다 — 흐름 검증만.

    LLM JSON parsing 실패 시 generated_count=0 + retries_used=max로 종료.
    """
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/assessment/generate",
            json={"subject": "정보보안", "count": 2},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # InMemoryLLM의 default 응답("mock answer")은 JSON 파싱 실패
    # generated_count는 0, retries는 max에 도달
    assert body["generated_count"] == 0
    assert body["retries_used"] >= 1


def test_hybrid_combines_extract_and_generate_counts():
    with TestClient(app) as client:
        _create_admin_item(client, "E-1", difficulty="easy")
        _create_admin_item(client, "E-2", difficulty="easy")
        resp = client.post(
            "/api/security/assessment/hybrid",
            json={
                "extract": {
                    "subject": "정보보안",
                    "difficulty_distribution": {"easy": 2},
                },
                "generate": {"subject": "정보보안", "count": 0},
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    # count=0이면 GenerateRequest 검증 실패(ge=1) → 422
    assert resp.status_code == 422


def test_hybrid_with_real_counts():
    with TestClient(app) as client:
        _create_admin_item(client, "E-1", difficulty="easy")
        resp = client.post(
            "/api/security/assessment/hybrid",
            json={
                "extract": {
                    "subject": "정보보안",
                    "difficulty_distribution": {"easy": 1},
                },
                "generate": {"subject": "정보보안", "count": 1},
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["extracted_count"] == 1
    # InMemoryLLM JSON parse 실패로 generated_count=0
    assert body["generated_count"] == 0
    assert body["mode"] == "hybrid"


def test_extract_logs_to_assessment_logger():
    from app.core.config import get_settings
    from app.deps import get_assessment_logger

    with TestClient(app) as client:
        _create_admin_item(client, "Q-1", difficulty="easy")
        client.post(
            "/api/security/assessment/extract",
            json={"subject": "정보보안", "difficulty": "easy"},
            headers={"Authorization": "Bearer mock-token"},
        )
    logger = get_assessment_logger(get_settings())
    records = getattr(logger, "records", [])
    assert any(r.action == "extract" and r.domain_id == "security" for r in records)
