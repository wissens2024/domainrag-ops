"""Prompt Studio API e2e (ADR-017 §12).

4 endpoint:
  - GET   /admin/prompts                                  목록
  - GET   /admin/prompts/{task}                           단건 (version/ab_slot Query)
  - PATCH /admin/prompts/{task}/{version}/{ab_slot}       runtime override 갱신
  - POST  /admin/prompts/{task}/preview                   Jinja2 렌더 + 선택 LLM
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
    reset_prompt_studio_service,
    reset_rag_service,
    reset_tenant_config_override_service,
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
    reset_feedback_writer()
    reset_conversation_repository()
    reset_citation_distribution()
    reset_citation_reverify_service()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_ledger_audit_service()
    reset_tenant_config_override_service()
    reset_prompt_studio_service()
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


def test_list_prompts_includes_platform_tasks():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/prompts",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    tasks = {item["task"] for item in body["items"]}
    # platform/prompts/*.yaml에 등록된 task들 (name 필드 기준)
    assert "chat_answer" in tasks
    assert body["total"] >= 1
    # source는 'platform' (override 없으므로)
    chat_answer = next(i for i in body["items"] if i["task"] == "chat_answer")
    assert chat_answer["source"] == "platform"
    assert "{{ question }}" in chat_answer["user"]


def test_get_prompt_returns_platform_default():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/prompts/chat_answer",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"] == "chat_answer"
    assert body["source"] == "platform"


def test_get_prompt_unknown_404():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/prompts/nope_task",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "prompt_not_found"


def test_patch_overrides_runtime_and_get_returns_new_value():
    with TestClient(app) as client:
        patch_resp = client.patch(
            "/api/security/admin/prompts/chat_answer/v1/control",
            json={"system": "새 system prompt", "reason": "test"},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        get_resp = client.get(
            "/api/security/admin/prompts/chat_answer",
            headers={"Authorization": "Bearer mock-token"},
        )
    new = get_resp.json()
    assert new["system"] == "새 system prompt"
    assert new["source"] == "tenant_runtime"
    # user 템플릿은 base 유지
    assert "{{ question }}" in new["user"]


def test_patch_user_only_keeps_existing_system():
    with TestClient(app) as client:
        resp = client.patch(
            "/api/security/admin/prompts/chat_answer/v1/control",
            json={"user": "{{ question }}만 보내고 끝"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    record = resp.json()["new"]
    assert record["user"] == "{{ question }}만 보내고 끝"
    # system은 platform default 유지
    assert "AI 챗봇" in record["system"]


def test_patch_empty_returns_400():
    with TestClient(app) as client:
        resp = client.patch(
            "/api/security/admin/prompts/chat_answer/v1/control",
            json={"reason": "no fields"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "empty_patch"


def test_preview_renders_with_sample_question_and_default_contexts():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/prompts/chat_answer/preview",
            json={"sample_question": "패스워드 정책은?"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "패스워드 정책은?" in body["rendered_user"]
    # contexts dummy 1건이 sample_contexts default
    assert "샘플 문서" in body["rendered_user"]
    # invoke_llm 미지정 → sample_answer null
    assert body["sample_answer"] is None


def test_preview_custom_template_with_invoke_llm():
    """invoke_llm=True + InMemoryLLM → 'mock answer' 응답."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/prompts/chat_answer/preview",
            json={
                "system": "system test",
                "user": "Q: {{ question }}",
                "sample_question": "테스트",
                "invoke_llm": True,
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rendered_system"] == "system test"
    assert body["rendered_user"] == "Q: 테스트"
    # InMemoryLLMClient의 결정론적 응답
    assert body["sample_answer"] is not None
    assert "answer" in body["sample_answer"].lower()


def test_preview_invalid_jinja_returns_422():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/prompts/chat_answer/preview",
            json={
                "user": "{{ unknown_var }}",  # StrictUndefined → 에러
                "sample_question": "x",
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "prompt_render_failed"


def test_list_prompts_requires_admin():
    app.dependency_overrides[get_user_context] = _non_admin_user
    with TestClient(app) as client:
        resp = client.get("/api/security/admin/prompts")
    assert resp.status_code == 403
