"""Routing Rules API e2e (ADR-017 §13 + ADR-013).

3 endpoint:
  - GET  /admin/routing                — effective routing dict
  - PUT  /admin/routing                — yaml 전체 교체 + schema 검증
  - POST /admin/routing/dryrun         — ModelRouter.decide() 시뮬레이션
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
from app.core.tenant_config_service import TenantConfigService
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
    TenantConfigService.clear_runtime_overrides()
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
    yield
    app.dependency_overrides.clear()
    TenantConfigService.clear_runtime_overrides()
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


def test_get_routing_returns_platform_default():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/routing",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain_id"] == "security"
    routing = body["routing"]
    # platform routing.yaml에서 로드된 rules 존재
    assert isinstance(routing.get("rules"), list)
    assert len(routing["rules"]) >= 1
    assert routing.get("default", {}).get("model") == "tenant_slm"


def test_get_routing_requires_admin():
    app.dependency_overrides[get_user_context] = _non_admin_user
    with TestClient(app) as client:
        resp = client.get("/api/security/admin/routing")
    assert resp.status_code == 403


def test_put_routing_replaces_and_applies_runtime():
    """PUT 후 GET이 새 routing 반환 + runtime override 적용."""
    new_routing = {
        "schema_version": 1,
        "default": {"model": "shared_llm", "use_lora": False, "use_rag": True,
                    "ui_mode": "chat_structured"},
        "rules": [
            {"name": "all_to_shared",
             "when": {"query_type": "document_qa"},
             "use_model": "shared_llm", "use_lora": False, "use_rag": True,
             "ui_mode": "chat_structured"}
        ],
    }
    with TestClient(app) as client:
        put_resp = client.put(
            "/api/security/admin/routing",
            json={"value": new_routing, "reason": "test override"},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert put_resp.status_code == 200, put_resp.text
        get_resp = client.get(
            "/api/security/admin/routing",
            headers={"Authorization": "Bearer mock-token"},
        )
    body = get_resp.json()
    # runtime override가 적용되어 default.model='shared_llm' 반영
    assert body["routing"]["default"]["model"] == "shared_llm"
    # rules 교체 검증 — 새 rule만 존재해야 함(deep_merge: list 교체)
    rule_names = [r["name"] for r in body["routing"]["rules"]]
    assert "all_to_shared" in rule_names


def test_put_routing_schema_invalid_422():
    """rules가 dict가 아니면 422 + errors[]."""
    with TestClient(app) as client:
        resp = client.put(
            "/api/security/admin/routing",
            json={"value": {"rules": "not_a_list"}},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["error"] == "routing_schema_invalid"
    assert "rules_not_list" in body["detail"]["errors"]


def test_put_routing_rule_missing_action_422():
    """rule에 use_model/model/action 모두 없으면 422."""
    with TestClient(app) as client:
        resp = client.put(
            "/api/security/admin/routing",
            json={"value": {
                "rules": [{"name": "bad", "when": {"query_type": "x"}, "use_rag": True}]
            }},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422
    assert "rules[0]_missing_use_model_or_action" in resp.json()["detail"]["errors"]


def test_put_routing_unknown_when_key_422():
    with TestClient(app) as client:
        resp = client.put(
            "/api/security/admin/routing",
            json={"value": {"rules": [
                {"name": "x", "when": {"weather": "rainy"}, "use_model": "tenant_slm"}
            ]}},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422
    err = resp.json()["detail"]["errors"][0]
    assert "weather" in err


def test_dryrun_uses_current_effective_routing():
    """routing_config 미지정 — platform default rules로 매칭."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/routing/dryrun",
            json={
                "sample_query": "테스트 질문",
                "classifier_decision": {
                    "query_type": "document_qa",
                    "support_type": "direct",
                    "complexity": "low",
                },
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    decision = resp.json()["decision"]
    # platform routing.yaml: simple_document_qa rule이 매칭 (low complexity)
    assert decision["matched_rule"] == "simple_document_qa"
    assert decision["model"] == "tenant_slm"


def test_dryrun_with_custom_routing_config_preview():
    """routing_config 명시 — 미리 시뮬레이션 (PUT 전 프리뷰)."""
    custom_routing = {
        "default": {"model": "shared_llm"},
        "rules": [
            {"name": "only_inference_to_shared",
             "when": {"support_type": "inference"},
             "use_model": "shared_llm", "use_lora": False, "use_rag": True}
        ],
    }
    with TestClient(app) as client:
        # matching path
        m_resp = client.post(
            "/api/security/admin/routing/dryrun",
            json={
                "classifier_decision": {
                    "query_type": "document_qa", "support_type": "inference",
                    "complexity": "high",
                },
                "routing_config": custom_routing,
            },
            headers={"Authorization": "Bearer mock-token"},
        )
        # non-matching path — default 사용
        d_resp = client.post(
            "/api/security/admin/routing/dryrun",
            json={
                "classifier_decision": {
                    "query_type": "document_qa", "support_type": "direct",
                    "complexity": "low",
                },
                "routing_config": custom_routing,
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert m_resp.status_code == 200
    assert m_resp.json()["decision"]["matched_rule"] == "only_inference_to_shared"
    assert m_resp.json()["decision"]["model"] == "shared_llm"

    assert d_resp.status_code == 200
    assert d_resp.json()["decision"]["matched_rule"] == "default"
    assert d_resp.json()["decision"]["model"] == "shared_llm"


def test_dryrun_retrieval_confidence_below_rule_triggers():
    """retrieval_confidence < threshold일 때 fallback_refusal rule이 매칭."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/routing/dryrun",
            json={
                "classifier_decision": {"query_type": "document_qa"},
                "retrieval_confidence": 0.2,
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    decision = resp.json()["decision"]
    assert decision["matched_rule"] == "low_retrieval_confidence"
    assert decision["action"] == "fallback_refusal"


def test_dryrun_invalid_routing_config_422():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/routing/dryrun",
            json={
                "classifier_decision": {"query_type": "document_qa"},
                "routing_config": {"rules": [{"name": "x"}]},  # missing use_model
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "routing_schema_invalid"
