"""AuthConfigLoader — configs/platform/auth.yaml + tenant auth.yaml 합성 검증."""

from __future__ import annotations

from app.core.auth_config import AuthConfigLoader
from app.core.config import get_settings


def setup_function(_func):
    AuthConfigLoader.reset()


def test_loader_reads_platform_yaml():
    cfg = AuthConfigLoader.load(get_settings())
    assert cfg.auth_mode in {"mock", "authfusion"}
    assert cfg.jwt_algorithm == "RS256"
    assert cfg.jwks_cache_ttl_seconds == 3600
    assert cfg.platform_admin_role == "PLATFORM_ADMIN"


def test_loader_resolves_client_tenant_map_with_tenant_overrides():
    cfg = AuthConfigLoader.load(get_settings())
    # platform 파일의 매핑
    assert cfg.client_tenant_map.get("client-security") == "security"
    assert cfg.client_tenant_map.get("client-legal") == "legal"
    # tenant overrides에 security가 잡혔는지
    assert "security" in cfg.tenant_overrides
    assert cfg.tenant_overrides["security"]["client_id"] == "client-security"


def test_client_id_to_tenant_id_fallback():
    cfg = AuthConfigLoader.load(get_settings())
    # 등록된 client_id
    assert cfg.client_id_to_tenant_id("client-security") == "security"
    # 미등록 — prefix 제거
    assert cfg.client_id_to_tenant_id("client-unknown") == "unknown"
    assert cfg.client_id_to_tenant_id("noprefix") == "noprefix"


def test_service_accounts_loaded():
    cfg = AuthConfigLoader.load(get_settings())
    sa = cfg.service_accounts.get("service-domainrag-indexer")
    assert sa is not None
    assert sa.tenant_id == "platform"
    assert "SERVICE" in sa.roles
    assert sa.clearance == "secret"


def test_mock_default_user_loaded():
    cfg = AuthConfigLoader.load(get_settings())
    assert cfg.mock_default_user is not None
    assert cfg.mock_default_user.tenant_id == "security"
    assert "ADMIN" in cfg.mock_default_user.roles
    assert cfg.mock_forbid_in_production is True


def test_authfusion_endpoints_resolved_from_env():
    """AUTHFUSION_ISSUER env가 Settings로 들어와 cfg.issuer로 노출되는지."""
    cfg = AuthConfigLoader.load(get_settings())
    assert cfg.issuer.startswith("http")
    assert cfg.jwks_uri.startswith("http")
