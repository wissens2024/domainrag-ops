"""인증 API — `/api/auth/*` (ADR-017 §2, ADR-018 §2·§6).

흐름:
  1. Frontend → GET /api/auth/authorize/{tenant_id} — backend가 PKCE pair 생성 + state 저장 +
     authorize URL 반환. Frontend가 그 URL로 redirect.
  2. AuthFusion → GET /api/auth/callback?code&state — backend가 state로 code_verifier
     복원 → token endpoint 호출 → httpOnly Secure 쿠키 set.
  3. POST /api/auth/refresh — refresh_token 쿠키 사용 → 새 access/refresh 쿠키 갱신.
  4. POST /api/auth/logout — access/refresh 쿠키의 token revoke + 쿠키 삭제.

ADR-018 §9 — auth_mode는 `authfusion` 단일 (mock은 backend/tests/_fixtures로 격리).
운영 backend는 mock 분기 코드를 포함하지 않는다.
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.auth_config import AuthConfig, AuthConfigLoader
from app.core.authfusion_token_client import (
    AuthFusionTokenClient,
    TokenExchangeError,
    TokenResponse,
)
from app.core.config import Settings, get_settings
from app.core.oauth_state_store import (
    InMemoryOAuthStateStore,
    OAuthStateEntry,
    generate_pkce_pair,
    generate_state,
)
from app.deps import (
    get_authfusion_token_client,
    get_oauth_state_store,
)

router = APIRouter()


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


_ACCESS_COOKIE = "domainrag_access"
_REFRESH_COOKIE = "domainrag_refresh"


def _set_cookies(
    response: Response,
    *,
    token: TokenResponse,
    is_production: bool,
) -> None:
    """access/refresh를 httpOnly Secure 쿠키로 set. local dev는 Secure 끔."""
    common = {
        "httponly": True,
        "secure": is_production,
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(
        _ACCESS_COOKIE,
        token.access_token,
        max_age=token.expires_in or None,
        **common,
    )
    if token.refresh_token:
        response.set_cookie(
            _REFRESH_COOKIE,
            token.refresh_token,
            max_age=token.expires_in * 4 if token.expires_in else None,
            **common,
        )


def _clear_cookies(response: Response, *, is_production: bool) -> None:
    for name in (_ACCESS_COOKIE, _REFRESH_COOKIE):
        response.delete_cookie(name, path="/", secure=is_production, httponly=True)


def _redirect_uri(auth_cfg: AuthConfig) -> str:
    base = auth_cfg.app_base_url.rstrip("/")
    return f"{base}{auth_cfg.redirect_path}"


def _resolve_client_id(auth_cfg: AuthConfig, tenant_id: str) -> str:
    ov = auth_cfg.tenant_overrides.get(tenant_id) or {}
    cid = ov.get("client_id")
    if not cid:
        # client_tenant_map 역방향 탐색
        for client_id, mapped in auth_cfg.client_tenant_map.items():
            if mapped == tenant_id:
                return client_id
        raise HTTPException(
            status_code=404,
            detail={"error": "client_id_not_configured", "tenant_id": tenant_id},
        )
    return str(cid)


# ----------------------------------------------------------------------------
# /authorize/{tenant_id} — Frontend가 SSO redirect를 시작하기 전 호출
# ----------------------------------------------------------------------------


@router.get("/authorize/{tenant_id}")
async def build_authorize_url(
    tenant_id: str,
    redirect: bool = Query(default=False),
    settings: Settings = Depends(get_settings),
    state_store: InMemoryOAuthStateStore = Depends(get_oauth_state_store),
):
    """ADR-018 §2 — PKCE pair 생성 + state 저장 + authorize URL 반환.

    기본은 JSON({authorize_url, state, tenant_id}) — frontend SPA가 직접
    navigation을 다룰 때. `?redirect=1`이면 그 URL로 302 redirect — Next.js
    middleware나 일반 브라우저 navigation이 그대로 사용 가능.
    """
    auth_cfg = AuthConfigLoader.load(settings)

    if not auth_cfg.authorize_endpoint:
        raise HTTPException(
            status_code=500, detail={"error": "authorize_endpoint_not_configured"}
        )

    client_id = _resolve_client_id(auth_cfg, tenant_id)
    verifier, challenge = generate_pkce_pair()
    state = generate_state()
    redirect_uri = _redirect_uri(auth_cfg)

    state_store.put(
        OAuthStateEntry(
            state=state,
            tenant_id=tenant_id,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
            expires_at=time.time() + auth_cfg.state_ttl_seconds,
        )
    )

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(auth_cfg.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"{auth_cfg.authorize_endpoint}?{urlencode(params)}"
    if redirect:
        return RedirectResponse(url=authorize_url, status_code=302)
    return {
        "authorize_url": authorize_url,
        "state": state,
        "tenant_id": tenant_id,
    }


# ----------------------------------------------------------------------------
# /callback — AuthFusion이 code/state를 가지고 frontend로 redirect 후 frontend가 호출
# ----------------------------------------------------------------------------


class CallbackResponse(BaseModel):
    tenant_id: str
    expires_in: int
    token_type: str
    scope: str | None = None


@router.get("/callback", response_model=CallbackResponse)
async def oauth_callback(
    code: str,
    state: str,
    response: Response,
    settings: Settings = Depends(get_settings),
    state_store: InMemoryOAuthStateStore = Depends(get_oauth_state_store),
    token_client: AuthFusionTokenClient | None = Depends(get_authfusion_token_client),
):
    """OAuth2 callback (ADR-018 §2 step 5). state 검증 + token 교환 + 쿠키 set."""
    auth_cfg = AuthConfigLoader.load(settings)
    is_production = settings.env == "production"

    entry = state_store.pop(state)
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_or_expired_state"},
        )
    if token_client is None:
        raise HTTPException(
            status_code=500, detail={"error": "token_client_not_configured"}
        )

    try:
        token = await token_client.exchange_code(
            code=code,
            code_verifier=entry.code_verifier,
            client_id=entry.client_id,
            redirect_uri=entry.redirect_uri,
        )
    except TokenExchangeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "token_exchange_failed", "upstream": exc.payload},
        ) from exc

    _set_cookies(response, token=token, is_production=is_production)
    return CallbackResponse(
        tenant_id=entry.tenant_id,
        expires_in=token.expires_in,
        token_type=token.token_type,
        scope=token.scope,
    )


# ----------------------------------------------------------------------------
# /refresh
# ----------------------------------------------------------------------------


class RefreshRequest(BaseModel):
    # 명시적 refresh_token 또는 cookie 사용. body 우선.
    refresh_token: str | None = None
    tenant_id: str  # client_id 매핑에 필요


@router.post("/refresh")
async def refresh_token_endpoint(
    req: RefreshRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    token_client: AuthFusionTokenClient | None = Depends(get_authfusion_token_client),
):
    """ADR-018 §6 — refresh_token grant. body가 우선, 없으면 쿠키."""
    auth_cfg = AuthConfigLoader.load(settings)
    is_production = settings.env == "production"

    refresh = req.refresh_token or request.cookies.get(_REFRESH_COOKIE)
    if not refresh:
        raise HTTPException(
            status_code=400, detail={"error": "missing_refresh_token"}
        )
    if token_client is None:
        raise HTTPException(
            status_code=500, detail={"error": "token_client_not_configured"}
        )

    client_id = _resolve_client_id(auth_cfg, req.tenant_id)
    try:
        token = await token_client.refresh(
            refresh_token=refresh, client_id=client_id
        )
    except TokenExchangeError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": "refresh_failed", "upstream": exc.payload},
        ) from exc

    _set_cookies(response, token=token, is_production=is_production)
    return {
        "tenant_id": req.tenant_id,
        "expires_in": token.expires_in,
        "token_type": token.token_type,
    }


# ----------------------------------------------------------------------------
# /logout
# ----------------------------------------------------------------------------


class LogoutRequest(BaseModel):
    tenant_id: str


@router.post("/logout", status_code=204)
async def logout(
    req: LogoutRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    token_client: AuthFusionTokenClient | None = Depends(get_authfusion_token_client),
):
    """ADR-018 §6 — token revoke + 쿠키 삭제. revoke 실패는 swallow (RFC 7009)."""
    auth_cfg = AuthConfigLoader.load(settings)
    is_production = settings.env == "production"

    access = request.cookies.get(_ACCESS_COOKIE)
    refresh = request.cookies.get(_REFRESH_COOKIE)

    if token_client is not None:
        client_id = _resolve_client_id(auth_cfg, req.tenant_id)
        if access:
            await token_client.revoke(
                token=access, client_id=client_id, token_type_hint="access_token"
            )
        if refresh:
            await token_client.revoke(
                token=refresh, client_id=client_id, token_type_hint="refresh_token"
            )

    _clear_cookies(response, is_production=is_production)
    response.status_code = 204
    return None
