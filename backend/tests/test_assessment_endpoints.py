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
    get_assessment_import_service,
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


def test_admin_create_item_with_assets_roundtrip():
    """ADR-025 — 이미지 자산·figure_dependent가 create→get에 보존된다."""
    asset = {
        "asset_id": "a1",
        "kind": "image",
        "storage_key": "items/security/Q-IMG/a1.png",
        "content_hash": "sha256:abc",
        "source_page": 4,
        "bbox": [10, 20, 110, 220],
        "vlm_description": "이진트리: 루트 a",
    }
    with TestClient(app) as client:
        created = _create_admin_item(
            client, "Q-IMG", assets=[asset], figure_dependent=True,
        )
        assert created.status_code == 201, created.text
        assert created.json()["figure_dependent"] is True
        assert created.json()["assets"][0]["storage_key"] == asset["storage_key"]
        got = client.get(
            "/api/security/admin/assessment/items/Q-IMG",
            headers={"Authorization": "Bearer mock-token"},
        )
    body = got.json()
    assert body["figure_dependent"] is True
    assert body["assets"] == [asset]


def test_admin_create_item_defaults_no_assets():
    """assets 미지정 시 빈 배열·figure_dependent=False 기본."""
    with TestClient(app) as client:
        resp = _create_admin_item(client, "Q-PLAIN")
    body = resp.json()
    assert body["assets"] == []
    assert body["figure_dependent"] is False


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


def test_admin_approve_item_no_body_200():
    """frontend 단건 approve는 body 없이 호출 — 빈 본문에도 200 (422 회귀 방지)."""
    with TestClient(app) as client:
        _create_admin_item(client, "Q-NB", quality_status="reviewed")
        resp = client.post(
            "/api/security/admin/assessment/items/Q-NB/approve",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["quality_status"] == "approved"


def test_admin_bulk_approve_all_only_draft_reviewed():
    with TestClient(app) as client:
        _create_admin_item(client, "B-1", quality_status="draft")
        _create_admin_item(client, "B-2", quality_status="reviewed")
        _create_admin_item(client, "B-3", quality_status="approved")
        resp = client.post(
            "/api/security/admin/assessment/items/approve-all",
            json={},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["approved"] == 2  # draft + reviewed만
        remaining = client.get(
            "/api/security/admin/assessment/review-queue",
            headers={"Authorization": "Bearer mock-token"},
        ).json()
    assert remaining["total"] == 0


def test_admin_bulk_approve_filtered_by_subject():
    with TestClient(app) as client:
        _create_admin_item(client, "S-1", subject="네트워크", quality_status="draft")
        _create_admin_item(client, "S-2", subject="정보보안", quality_status="draft")
        resp = client.post(
            "/api/security/admin/assessment/items/approve-all",
            json={"subject": "네트워크"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.json()["approved"] == 1


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


class _FakeImportService:
    async def import_pdf(
        self, *, domain_id, pdf_bytes, item_id_prefix,
        answer_page_index=None, default_quality_status="draft", tags=None,
        auto_approve=False,
    ):
        from app.services.assessment_import_service import (
            ImportItemResult,
            ImportResult,
        )

        self.last_pdf_len = len(pdf_bytes)
        return ImportResult(
            created=1, figures_stored=1, parsed_count=1, answer_key_count=1,
            items=[
                ImportItemResult(
                    item_id=f"{item_id_prefix}001", number=1,
                    subject="software_design", figure_dependent=True,
                    asset_count=1, quality_status=default_quality_status,
                    has_answer=True, flags=[],
                )
            ],
        )


def test_admin_import_pdf_returns_summary():
    fake = _FakeImportService()
    app.dependency_overrides[get_assessment_import_service] = lambda: fake
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/assessment/import",
            files={"file": ("exam.pdf", b"%PDF-1.4 fake", "application/pdf")},
            data={"item_id_prefix": "gisa-2022-2-w-", "default_quality_status": "draft"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] == 1
    assert body["figures_stored"] == 1
    assert body["items"][0]["item_id"] == "gisa-2022-2-w-001"
    assert body["items"][0]["figure_dependent"] is True
    assert fake.last_pdf_len == len(b"%PDF-1.4 fake")


def test_admin_import_empty_file_400():
    app.dependency_overrides[get_assessment_import_service] = lambda: _FakeImportService()
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/assessment/import",
            files={"file": ("exam.pdf", b"", "application/pdf")},
            data={"item_id_prefix": "p-"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "empty_file"


def test_admin_import_requires_admin():
    app.dependency_overrides[get_user_context] = _non_admin_user
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/assessment/import",
            files={"file": ("exam.pdf", b"x", "application/pdf")},
            data={"item_id_prefix": "p-"},
        )
    assert resp.status_code == 403


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


# --------------------------------------------------------------------------- #
# Validate endpoint (ADR-025 §6)
# --------------------------------------------------------------------------- #


def test_validate_returns_results_without_persist():
    """검수만 — 저장 없이 validator 결과 반환. InMemoryLLM은 '{}'라 draft로 판정."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/assessment/validate",
            json={"items": [{
                "subject": "정보보안",
                "question_text": "방화벽의 주된 목적은?",
                "choices": ["A", "B", "C", "D"],
                "answer": "A",
            }]},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "validate"
        assert body["validated_count"] == 1
        r0 = body["results"][0]
        assert r0["quality_status"] == "draft"  # InMemoryLLM → validator invalid
        assert r0["persisted"] is False
        assert set(r0["validator_results"].keys()) == {
            "answer", "explanation", "choices", "difficulty"
        }
        # 저장 안 됨 — review-queue 비어 있음
        rq = client.get(
            "/api/security/admin/assessment/review-queue",
            headers={"Authorization": "Bearer mock-token"},
        )
        assert rq.json()["total"] == 0


def test_validate_persist_requires_admin():
    app.dependency_overrides[get_user_context] = _non_admin_user
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/assessment/validate",
            json={"persist": True, "items": [{
                "subject": "정보보안", "question_text": "Q?",
                "choices": ["A", "B"], "answer": "A",
            }]},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "insufficient_role"


def test_validate_persist_saves_item_with_validator_results():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/assessment/validate",
            json={"persist": True, "items": [{
                "item_id": "V-1",
                "subject": "정보보안",
                "question_text": "대칭키 암호의 예는?",
                "choices": ["AES", "RSA", "ECC", "DH"],
                "answer": "AES",
            }]},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert resp.status_code == 200, resp.text
        r0 = resp.json()["results"][0]
        assert r0["persisted"] is True and r0["item_id"] == "V-1"
        # 저장됨 — validator_results가 채워진 채 은행에 존재
        got = client.get(
            "/api/security/admin/assessment/items/V-1",
            headers={"Authorization": "Bearer mock-token"},
        )
        assert got.status_code == 200
        item = got.json()
        assert item["quality_status"] == "draft"
        assert item["validator_results"]["answer"]["valid"] is False
        assert item["source"] == "imported"


def test_validate_persist_requires_subject():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/assessment/validate",
            json={"persist": True, "items": [{
                "question_text": "정답 없는 문제",
                "choices": ["A", "B"], "answer": "A",
            }]},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "subject_required_for_persist"


def test_validate_figure_item_degrades_to_draft():
    """그림 문항은 VLM validator 미배선(Phase 3) → degrade 표시 + draft 고정."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/assessment/validate",
            json={"items": [{
                "subject": "정보보안",
                "question_text": "아래 트리의 후위순회 결과는?",
                "choices": ["A", "B", "C", "D"],
                "answer": "A",
                "figure_dependent": True,
                "assets": [{"asset_id": "x", "kind": "image",
                            "storage_key": "items/security/x.png"}],
            }]},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    r0 = resp.json()["results"][0]
    assert r0["figure_validation"]["status"] == "skipped"
    assert r0["figure_validation"]["reason"] == "vlm_validator_not_wired"
    assert r0["quality_status"] == "draft"


def test_validate_logs_to_assessment_logger():
    from app.core.config import get_settings
    from app.deps import get_assessment_logger

    with TestClient(app) as client:
        client.post(
            "/api/security/assessment/validate",
            json={"items": [{
                "subject": "정보보안", "question_text": "Q?",
                "choices": ["A", "B", "C", "D"], "answer": "A",
            }]},
            headers={"Authorization": "Bearer mock-token"},
        )
    logger = get_assessment_logger(get_settings())
    records = getattr(logger, "records", [])
    assert any(r.action == "validate" and r.domain_id == "security" for r in records)
