"""/api/auth/{authorize|callback|refresh|logout} 통합 테스트 (ADR-018 §2·§6).

전략:
  - mock 모드(auth_mode=mock) 테스트: stub 응답 + Set-Cookie 검증
  - authfusion 모드 테스트: dependency_override로 AuthConfig + TokenClient mock 주입
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.core.auth_config import AuthConfig, AuthConfigLoader
from tests._fixtures.mock_auth import MockUserConfig
from app.core.authfusion_token_client import TokenExchangeError, TokenResponse
from app.core.oauth_state_store import (
    InMemoryOAuthStateStore,
    OAuthStateEntry,
    generate_pkce_pair,
    generate_state,
)
from app.deps import (
    get_authfusion_token_client,
    get_oauth_state_store,
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_indexing_orchestrator,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_rag_service,
)
from app.main import app


_ORIGINAL_AUTH_CONFIG_LOAD = AuthConfigLoader.load


@pytest.fixture(autouse=True)
def _reset_all():
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    AuthConfigLoader.reset()
    yield
    # _force_authfusion_mode가 AuthConfigLoader.load를 swap한 경우 복원
    AuthConfigLoader.load = _ORIGINAL_AUTH_CONFIG_LOAD  # type: ignore[method-assign]
    app.dependency_overrides.clear()
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    AuthConfigLoader.reset()


# Mock 모드 endpoint stub 테스트는 ADR-018 §9 옵션 C 적용으로 삭제 (2026-05-18).
# 운영 코드에 mock 분기가 더 이상 없으므로 그 분기 검증도 불필요.
# AuthFusion 모드 흐름은 아래 dependency_override + AuthFusionTokenClient stub 사용.

# ----------------------------------------------------------------------------
# AuthFusion 모드 — dependency_override로 mock token client 주입
# ----------------------------------------------------------------------------


@dataclass
class _Calls:
    exchanges: list[dict]
    refreshes: list[dict]
    revokes: list[dict]


class _MockTokenClient:
    """AuthFusionTokenClient Protocol을 충족하는 dataclass-friendly mock."""

    def __init__(
        self,
        *,
        access: str = "real-access",
        refresh: str = "real-refresh",
        expires_in: int = 900,
        exchange_should_fail: bool = False,
    ) -> None:
        self.calls = _Calls(exchanges=[], refreshes=[], revokes=[])
        self._access = access
        self._refresh = refresh
        self._expires_in = expires_in
        self._fail = exchange_should_fail

    async def exchange_code(self, **kwargs) -> TokenResponse:
        self.calls.exchanges.append(kwargs)
        if self._fail:
            raise TokenExchangeError(status_code=400, payload={"error": "invalid_grant"})
        return TokenResponse(
            access_token=self._access,
            refresh_token=self._refresh,
            expires_in=self._expires_in,
        )

    async def refresh(self, **kwargs) -> TokenResponse:
        self.calls.refreshes.append(kwargs)
        return TokenResponse(
            access_token=self._access + "-refreshed",
            refresh_token=self._refresh + "-refreshed",
            expires_in=self._expires_in,
        )

    async def revoke(self, **kwargs) -> None:
        self.calls.revokes.append(kwargs)


def _authfusion_auth_config(**overrides) -> AuthConfig:
    base = dict(
        auth_mode="oidc",
        issuer="https://sso.test",
        jwks_uri="https://sso.test/jwks",
        token_endpoint="https://sso.test/oauth2/token",
        revoke_endpoint="https://sso.test/oauth2/revoke",
        authorize_endpoint="https://sso.test/oauth2/authorize",
        userinfo_endpoint=None,
        end_session_endpoint="https://sso.test/oauth2/logout",
        discovery_url=None,
        scopes=["openid", "profile"],
        state_ttl_seconds=600,
        app_base_url="http://localhost:3010",
        redirect_path="/auth/callback",
        jwt_algorithm="RS256",
        jwks_cache_ttl_seconds=3600,
        platform_admin_role="PLATFORM_ADMIN",
        client_id=None,
        client_secret=None,
        require_verified_email=False,
        rp_slug="domainrag-ops",
        client_tenant_map={"client-security": ["security"]},
        service_accounts={},
        tenant_overrides={"security": {"client_id": "client-security"}},
    )
    base.update(overrides)
    return AuthConfig(**base)


def _force_authfusion_mode(token_client: _MockTokenClient):
    """AuthConfigLoader.load 결과를 authfusion 모드로 강제 + token client 주입."""

    def _patched(settings=None):  # type: ignore[unused-argument]
        return _authfusion_auth_config()

    AuthConfigLoader.load = staticmethod(_patched)  # type: ignore[method-assign]
    app.dependency_overrides[get_authfusion_token_client] = lambda: token_client


def test_authorize_builds_url_with_pkce_in_authfusion_mode():
    _force_authfusion_mode(_MockTokenClient())
    with TestClient(app) as client:
        resp = client.get("/api/auth/authorize/security")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "mock_mode" not in body
    url = body["authorize_url"]
    assert "https://sso.test/oauth2/authorize?" in url
    assert "code_challenge_method=S256" in url
    assert "client_id=client-security" in url
    # state는 server-side store에 저장됨 — 후속 callback에서 사용
    assert body["state"]


def test_callback_exchanges_code_and_sets_cookies():
    token_client = _MockTokenClient()
    _force_authfusion_mode(token_client)
    # state를 사전 등록 — authorize endpoint를 통해 만든다
    with TestClient(app) as client:
        auth_resp = client.get("/api/auth/authorize/security")
        state = auth_resp.json()["state"]
        resp = client.get(f"/api/auth/callback?code=real-code&state={state}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain_id"] == "security"
    assert body["expires_in"] == 900
    # token client가 정확한 verifier로 호출되어야 한다
    assert len(token_client.calls.exchanges) == 1
    call = token_client.calls.exchanges[0]
    assert call["code"] == "real-code"
    assert call["client_id"] == "client-security"
    assert call["redirect_uri"] == "http://localhost:3010/auth/callback"
    # Set-Cookie
    assert resp.cookies.get("domainrag_access") == "real-access"
    assert resp.cookies.get("domainrag_refresh") == "real-refresh"


def test_callback_rejects_invalid_state():
    token_client = _MockTokenClient()
    _force_authfusion_mode(token_client)
    with TestClient(app) as client:
        resp = client.get("/api/auth/callback?code=x&state=does-not-exist")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_or_expired_state"
    assert token_client.calls.exchanges == []  # 호출 안 됨


def test_callback_rejects_expired_state():
    token_client = _MockTokenClient()
    _force_authfusion_mode(token_client)
    # state store에 만료된 entry 직접 삽입
    state = generate_state()
    store = InMemoryOAuthStateStore()
    store.put(
        OAuthStateEntry(
            state=state,
            domain_id="security",
            code_verifier="v",
            redirect_uri="http://localhost:3010/auth/callback",
            client_id="client-security",
            expires_at=0,  # 이미 만료
        )
    )
    app.dependency_overrides[get_oauth_state_store] = lambda: store
    with TestClient(app) as client:
        resp = client.get(f"/api/auth/callback?code=x&state={state}")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_or_expired_state"


def test_callback_surfaces_upstream_token_error():
    token_client = _MockTokenClient(exchange_should_fail=True)
    _force_authfusion_mode(token_client)
    with TestClient(app) as client:
        auth_resp = client.get("/api/auth/authorize/security")
        state = auth_resp.json()["state"]
        resp = client.get(f"/api/auth/callback?code=bad&state={state}")
    assert resp.status_code == 400
    err = resp.json()["detail"]
    assert err["error"] == "token_exchange_failed"
    assert err["upstream"]["error"] == "invalid_grant"


def test_refresh_uses_cookie_when_body_missing():
    token_client = _MockTokenClient()
    _force_authfusion_mode(token_client)
    with TestClient(app) as client:
        # 먼저 callback으로 cookie set
        auth_resp = client.get("/api/auth/authorize/security")
        state = auth_resp.json()["state"]
        client.get(f"/api/auth/callback?code=x&state={state}")
        # body에 refresh_token 없이 호출 — cookie를 사용
        resp = client.post(
            "/api/auth/refresh", json={"domain_id": "security"}
        )
    assert resp.status_code == 200, resp.text
    assert len(token_client.calls.refreshes) == 1
    assert token_client.calls.refreshes[0]["refresh_token"] == "real-refresh"


def test_logout_calls_revoke_for_both_tokens():
    token_client = _MockTokenClient()
    _force_authfusion_mode(token_client)
    with TestClient(app) as client:
        auth_resp = client.get("/api/auth/authorize/security")
        state = auth_resp.json()["state"]
        client.get(f"/api/auth/callback?code=x&state={state}")
        resp = client.post("/api/auth/logout", json={"domain_id": "security"})
    assert resp.status_code == 200
    # ADR-022 — IdP end_session URL 반환 (RP-Initiated Logout)
    logout_url = resp.json()["logout_url"]
    assert logout_url is not None
    assert logout_url.startswith("https://sso.test/oauth2/logout")
    assert "post_logout_redirect_uri=" in logout_url
    # access + refresh 두 번 revoke
    assert len(token_client.calls.revokes) == 2
    type_hints = {c["token_type_hint"] for c in token_client.calls.revokes}
    assert type_hints == {"access_token", "refresh_token"}
