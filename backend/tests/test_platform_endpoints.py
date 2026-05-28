"""Platform Admin endpoints e2e — ADR-017 §18.

3 endpoint:
  - GET /platform/admin/endpoints       — endpoint health 종합
  - GET /platform/admin/analytics/usage — cross-tenant chat 사용량
  - GET /platform/admin/analytics/health — endpoints health 재활용
  - GET/PUT /platform/admin/configs/{cat} — platform yaml read/write
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
from app.deps import (
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_chat_log_reader,
    reset_dashboard_analytics,
    reset_indexing_orchestrator,
    reset_ledger_audit_service,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_rag_service,
)
from app.main import app


def _platform_admin() -> UserContext:
    return UserContext(
        user_id="platform-001", domain_id="platform",
        roles=["SERVICE", "PLATFORM_ADMIN"], clearance="top_secret",
    )


@pytest.fixture(autouse=True)
def _reset():
    app.dependency_overrides[get_user_context] = _platform_admin
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_chat_log_reader()
    reset_dashboard_analytics()
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
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_ledger_audit_service()


def test_list_endpoints_returns_configured_set():
    with TestClient(app) as client:
        resp = client.get("/api/platform/admin/endpoints")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == "inmemory"
    names = {it["name"] for it in body["items"]}
    assert {"tenant_slm", "shared_llm", "embedder", "reranker", "qdrant",
            "postgres", "minio", "ollama"}.issubset(names)
    # InMemory backend는 모두 'configured'
    assert all(it["status"] == "configured" for it in body["items"])


def test_endpoints_requires_platform_admin():
    """일반 admin은 403."""
    def _tenant_admin() -> UserContext:
        return UserContext(
            user_id="t1-admin", domain_id="security",
            roles=["USER", "ADMIN"], clearance="confidential",
        )

    app.dependency_overrides[get_user_context] = _tenant_admin
    with TestClient(app) as client:
        resp = client.get("/api/platform/admin/endpoints")
    assert resp.status_code == 403


def test_analytics_usage_aggregates_cross_tenant():
    """security에서 chat 2번 호출 → usage에 security tenant 노출."""
    # 사용자가 chat 호출하려면 일반 user context 필요. dependency_override 교체.
    def _security_user() -> UserContext:
        return UserContext(
            user_id="dev-user-001", domain_id="security",
            roles=["USER"], clearance="confidential",
        )

    app.dependency_overrides[get_user_context] = _security_user
    with TestClient(app) as client:
        for q in ["질문 1", "질문 2"]:
            resp = client.post(
                "/api/security/chat",
                json={"conversation_id": None, "question": q},
                headers={"Authorization": "Bearer mock-token"},
            )
            assert resp.status_code == 200, resp.text

    # platform_admin으로 다시 전환해 usage 조회
    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        resp = client.get("/api/platform/admin/analytics/usage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    secs = [t for t in body["items"] if t["domain_id"] == "security"]
    assert len(secs) == 1
    assert secs[0]["messages"] >= 2


def test_analytics_health_returns_endpoint_summary():
    with TestClient(app) as client:
        resp = client.get("/api/platform/admin/analytics/health")
    assert resp.status_code == 200
    assert resp.json()["backend"] == "inmemory"


def test_get_platform_config_returns_yaml_content():
    with TestClient(app) as client:
        resp = client.get("/api/platform/admin/configs/citation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["category"] == "citation"
    assert body["exists"] is True
    # citation.yaml에 verification 키가 있어야 함 (ADR-010)
    assert "verification" in body["value"] or "gates" in body["value"]


def test_get_platform_config_unknown_404():
    with TestClient(app) as client:
        resp = client.get("/api/platform/admin/configs/nope")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "unknown_category"


def test_put_platform_config_writes_and_invalidates_cache(tmp_path, monkeypatch):
    """파일 시스템 mutation — 원본 보존 위해 monkeypatch로 임시 config_dir 사용."""
    from app.core.config import get_settings
    from app.core.tenant_config_service import TenantConfigService

    settings = get_settings()
    orig_dir = settings.config_dir

    # 임시 configs 디렉토리 + platform/ 복제
    import shutil

    tmp_configs = tmp_path / "configs"
    shutil.copytree(orig_dir, tmp_configs)
    monkeypatch.setattr(settings, "config_dir", tmp_configs)
    TenantConfigService.invalidate()

    new_value = {"verification": {"tier2": {"thresholds": {"strong": 0.99, "medium": 0.5}}}}
    with TestClient(app) as client:
        resp = client.put(
            "/api/platform/admin/configs/citation",
            json={"value": new_value, "reason": "test"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["category"] == "citation"
    assert body["value"]["verification"]["tier2"]["thresholds"]["strong"] == 0.99

    # 파일 실제 갱신 확인
    written = yaml.safe_load(
        (tmp_configs / "platform" / "citation.yaml").read_text(encoding="utf-8")
    )
    assert written == new_value


def test_put_platform_config_unknown_404():
    with TestClient(app) as client:
        resp = client.put(
            "/api/platform/admin/configs/nope",
            json={"value": {}},
        )
    assert resp.status_code == 404
