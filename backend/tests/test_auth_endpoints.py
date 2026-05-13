"""/api/auth/{authorize|callback|refresh|logout} 통합 테스트 (ADR-018 §2·§6).

전략:
  - mock 모드(auth_mode=mock) 테스트: stub 응답 + Set-Cookie 검증
  - authfusion 모드 테스트: dependency_override로 AuthConfig + TokenClient mock 주입
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.core.auth_config import AuthConfig, AuthConfigLoader, MockUserConfig
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


# ----------------------------------------------------------------------------
# Mock 모드 — endpoint stub 흐름 검증
# ----------------------------------------------------------------------------


def test_authorize_returns_mock_notice_in_mock_mode():
    """mock 모드 — authorize endpoint는 mock_mode=True 응답."""
    with TestClient(app) as client:
        resp = client.get("/api/auth/authorize/security")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mock_mode"] is True
    assert body["authorize_url"] is None


def test_callback_mock_returns_cookies_and_metadata():
    with TestClient(app) as client:
        resp = client.get("/api/auth/callback?code=mock&state=mock")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == "mock"
    assert body["expires_in"] == 900
    # Set-Cookie 검증
    cookies = resp.cookies
    assert cookies.get("domainrag_access") == "mock-access-token"
    assert cookies.get("domainrag_refresh") == "mock-refresh-token"


def test_refresh_mock_returns_new_cookies():
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/refresh",
            json={"tenant_id": "security"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.cookies.get("domainrag_access") == "mock-access-token-refreshed"


def test_logout_mock_clears_cookies():
    with TestClient(app) as client:
        # 먼저 callback으로 쿠키 set
        client.get("/api/auth/callback?code=mock&state=mock")
        resp = client.post("/api/auth/logout", json={"tenant_id": "security"})
    assert resp.status_code == 204
    # delete_cookie는 max_age=0이거나 Expires 과거 — 빈 문자열로 set됨
    # TestClient.cookies는 set이 되어 있을 수 있으니 raw header로 확인
    set_cookie_headers = resp.headers.get("set-cookie", "")
    # access/refresh 둘 다 명시 삭제 헤더가 포함되어 있어야 한다
    assert "domainrag_access" in set_cookie_headers
    assert "domainrag_refresh" in set_cookie_headers


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
        auth_mode="authfusion",
        issuer="https://sso.test",
        jwks_uri="https://sso.test/jwks",
        token_endpoint="https://sso.test/oauth2/token",
        revoke_endpoint="https://sso.test/oauth2/revoke",
        authorize_endpoint="https://sso.test/oauth2/authorize",
        scopes=["openid", "profile"],
        state_ttl_seconds=600,
        app_base_url="http://localhost:3000",
        redirect_path="/auth/callback",
        jwt_algorithm="RS256",
        jwks_cache_ttl_seconds=3600,
        platform_admin_role="PLATFORM_ADMIN",
        client_tenant_map={"client-security": "security"},
        service_accounts={},
        mock_default_user=MockUserConfig(
            user_id="x",
            tenant_id="security",
            roles=["USER"],
            clearance="internal",
            department=None,
            domain_groups=[],
            preferred_username=None,
            email=None,
        ),
        mock_forbid_in_production=True,
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
    assert body["tenant_id"] == "security"
    assert body["expires_in"] == 900
    # token client가 정확한 verifier로 호출되어야 한다
    assert len(token_client.calls.exchanges) == 1
    call = token_client.calls.exchanges[0]
    assert call["code"] == "real-code"
    assert call["client_id"] == "client-security"
    assert call["redirect_uri"] == "http://localhost:3000/auth/callback"
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
            tenant_id="security",
            code_verifier="v",
            redirect_uri="http://localhost:3000/auth/callback",
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
            "/api/auth/refresh", json={"tenant_id": "security"}
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
        resp = client.post("/api/auth/logout", json={"tenant_id": "security"})
    assert resp.status_code == 204
    # access + refresh 두 번 revoke
    assert len(token_client.calls.revokes) == 2
    type_hints = {c["token_type_hint"] for c in token_client.calls.revokes}
    assert type_hints == {"access_token", "refresh_token"}
