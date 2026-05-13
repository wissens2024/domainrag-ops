"""Runtime connection 통합 테스트 — Prompt PATCH→chat / Schema PUT→upload.

ADR-017 §12·§15 follow-up:
  - Prompt Studio PATCH가 chat_structured 흐름에 즉시 반영되는지
  - Schema Editor PUT이 /documents/upload metadata validation에 즉시 반영되는지
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

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
    reset_input_schema_service,
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
    reset_input_schema_service()
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
    reset_input_schema_service()


# --------------------------------------------------------------------------- #
# Connection 1 — Prompt Studio PATCH → chat 흐름 즉시 반영
# --------------------------------------------------------------------------- #


def test_prompt_studio_patch_takes_effect_in_chat_call():
    """PATCH /admin/prompts/chat_answer/v1/control이 다음 chat 호출에서 새 system을
    사용하는지. InMemoryLLMClient.calls를 검사해 prompt 본문에 새 system이 들어갔는지.
    """
    from app.core.config import get_settings
    from app.deps import get_rag_service

    sentinel = "SENTINEL_NEW_SYSTEM_PROMPT_FROM_PUT"
    with TestClient(app) as client:
        # 먼저 baseline chat 호출 — fallback prompt 사용 시 sentinel 없음
        baseline_resp = client.post(
            "/api/security/chat",
            json={"conversation_id": None, "question": "기준 질문"},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert baseline_resp.status_code == 200, baseline_resp.text

        rag = get_rag_service(get_settings())
        llm = rag._deps.generation_service._llm  # type: ignore[attr-defined]
        baseline_prompts = [c["prompt"] for c in llm.calls if c["kind"] == "generate"]
        assert all(sentinel not in p for p in baseline_prompts)

        # Prompt Studio PATCH — sentinel 포함 system
        patch_resp = client.patch(
            "/api/security/admin/prompts/chat_answer/v1/control",
            json={"system": sentinel, "user": "Q: {{ question }}"},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert patch_resp.status_code == 200, patch_resp.text

        # 두번째 chat — 새 system이 LLM prompt에 반영되어야 함
        client.post(
            "/api/security/chat",
            json={"conversation_id": None, "question": "두번째 질문"},
            headers={"Authorization": "Bearer mock-token"},
        )

    all_prompts = [c["prompt"] for c in llm.calls if c["kind"] == "generate"]
    # baseline call에는 sentinel이 없고, 두번째 call에는 있다
    later_prompts = all_prompts[len(baseline_prompts):]
    assert any(sentinel in p for p in later_prompts), (
        f"new system not found after PATCH; calls={len(later_prompts)}"
    )


# --------------------------------------------------------------------------- #
# Connection 2 — Schema Editor PUT → /documents/upload validation 즉시 반영
# --------------------------------------------------------------------------- #


def test_schema_editor_put_takes_effect_in_upload_validation():
    """PUT /admin/schema가 새 input_type을 등록하면 upload에서 그 type을 수락.

    InputSchemaLoader._runtime_yaml에 새 schema가 push되어 다음 upload validation이
    runtime schema를 사용한다.
    """
    new_input_type_name = "custom_runtime_type"
    new_schema_yaml = {
        "input_types": [
            # ADR-015 §1 list 포맷. PUT 시 dict 포맷으로 정규화되어 runtime layer 진입.
            {
                "name": new_input_type_name,
                "title": "런타임 등록 타입",
                "domain_fields": {
                    "custom_field": {
                        "type": "string", "required": False
                    }
                },
            },
        ]
    }

    with TestClient(app) as client:
        # 1) Schema Editor PUT
        put_resp = client.put(
            "/api/security/admin/schema",
            json={"schema_yaml": new_schema_yaml, "base_version": None},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert put_resp.status_code == 200, put_resp.text

        # 2) input_type이 등록되었는지 확인 — GET /input_schemas
        list_resp = client.get(
            "/api/security/admin/input_schemas",
            headers={"Authorization": "Bearer mock-token"},
        )
        assert list_resp.status_code == 200
        names = {t["name"] for t in list_resp.json()["input_types"]}
        assert new_input_type_name in names, names

        # 3) upload — 새 input_type 사용 시 수락. common_required(security_level/owner)도 함께 제공.
        metadata = json.dumps({
            "acl": ["group:security"],
            "custom_field": "hello",
            "security_level": "internal",
            "owner": "dev-user-001",
        })
        upload_resp = client.post(
            "/api/security/admin/documents/upload",
            files={"file": ("doc.txt", io.BytesIO(b"sample"), "text/plain")},
            data={
                "doc_id": "DOC-RUNTIME-1",
                "title": "런타임 타입 문서",
                "version": "v1",
                "input_type": new_input_type_name,
                "metadata": metadata,
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    # input_type이 등록되지 않았다면 422 unknown_input_type. 런타임 override가 작동했다면 202.
    assert upload_resp.status_code == 202, upload_resp.text


def test_schema_editor_runtime_does_not_leak_across_resets():
    """reset_input_schema_service가 runtime override를 비우는지 회귀 방어."""
    from app.core.input_schema import InputSchemaLoader

    with TestClient(app) as client:
        client.put(
            "/api/security/admin/schema",
            json={
                "schema_yaml": {
                    "input_types": [{"name": "leak_check_type"}]
                },
                "base_version": None,
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert "security" in InputSchemaLoader._runtime_yaml
    reset_input_schema_service()
    assert "security" not in InputSchemaLoader._runtime_yaml
