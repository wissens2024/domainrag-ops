"""AuthConfigLoader — configs/platform/auth.yaml + tenant auth.yaml 합성 검증."""

from __future__ import annotations

from app.core.auth_config import AuthConfigLoader
from app.core.config import get_settings


def setup_function(_func):
    AuthConfigLoader.reset()


def test_loader_reads_platform_yaml():
    cfg = AuthConfigLoader.load(get_settings())
    # ADR-018 §9 — AUTH_MODE는 oidc 단일 (AuthFusion 가이드 표준). mock은 격리됨.
    assert cfg.auth_mode == "oidc"
    assert cfg.jwt_algorithm == "RS256"
    assert cfg.jwks_cache_ttl_seconds == 3600
    assert cfg.platform_admin_role == "PLATFORM_ADMIN"


def test_loader_resolves_client_tenant_map_with_tenant_overrides():
    cfg = AuthConfigLoader.load(get_settings())
    # platform 파일의 매핑 — 내부 표현은 항상 list (single-client multi-tenant 지원)
    assert "security" in (cfg.client_tenant_map.get("client-security") or [])
    assert "legal" in (cfg.client_tenant_map.get("client-legal") or [])
    # tenant overrides에 security가 잡혔는지. 운영은 공용 단일 client UUID 공유 (ADR-022 §1)
    # — security와 exam-engineer가 같은 client_id를 가리킨다.
    assert "security" in cfg.tenant_overrides
    sec_client = cfg.tenant_overrides["security"]["client_id"]
    assert sec_client == cfg.tenant_overrides["exam-engineer"]["client_id"]


def test_client_id_to_domain_id_fallback():
    cfg = AuthConfigLoader.load(get_settings())
    # 등록된 client_id
    assert cfg.client_id_to_domain_id("client-security") == "security"
    # 미등록 — prefix 제거
    assert cfg.client_id_to_domain_id("client-unknown") == "unknown"
    assert cfg.client_id_to_domain_id("noprefix") == "noprefix"


def test_service_accounts_loaded():
    cfg = AuthConfigLoader.load(get_settings())
    sa = cfg.service_accounts.get("service-domainrag-indexer")
    assert sa is not None
    assert sa.domain_id == "platform"
    assert "SERVICE" in sa.roles
    assert sa.clearance == "secret"


def test_no_mock_fields_in_auth_config():
    """ADR-018 §9 — AuthConfig는 mock 필드를 더 이상 노출하지 않는다.

    mock default_user는 backend/tests/_fixtures/mock_auth.py:MockUserConfig 상수로 격리.
    """
    cfg = AuthConfigLoader.load(get_settings())
    assert not hasattr(cfg, "mock_default_user")
    assert not hasattr(cfg, "mock_forbid_in_production")


def test_authfusion_endpoints_resolved_from_env():
    """AUTHFUSION_ISSUER env가 Settings로 들어와 cfg.issuer로 노출되는지."""
    cfg = AuthConfigLoader.load(get_settings())
    assert cfg.issuer.startswith("http")
    assert cfg.jwks_uri.startswith("http")
