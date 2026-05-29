"""인증 API — `/api/auth/*` (ADR-017 §2, ADR-018 §2·§6).

흐름:
  1. Frontend → GET /api/auth/authorize/{domain_id} — backend가 PKCE pair 생성 + state 저장 +
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
from app.core.auth_adapter import UserContext, get_user_context_no_tenant
from app.deps import (
    get_authfusion_token_client,
    get_membership_service,
    get_oauth_state_store,
    get_tenant_lifecycle_service,
)

router = APIRouter()


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


_ACCESS_COOKIE = "domainrag_access"
_REFRESH_COOKIE = "domainrag_refresh"
_ID_TOKEN_COOKIE = "domainrag_id_token"  # OIDC RP-Initiated Logout id_token_hint용


def _set_cookies(
    response: Response,
    *,
    token: TokenResponse,
    is_production: bool,
) -> None:
    """access/refresh를 httpOnly Secure 쿠키로 set. local dev는 Secure 끔.

    세션 길이(ADR-018 §6): access 쿠키는 access 토큰 TTL(짧음)을 그대로 따르되,
    refresh/id 쿠키는 AuthFusion이 주는 *실제 refresh 토큰 수명*(`refresh_expires_in`)을
    max-age로 쓴다. 미제공 시 expires_in*4 fallback. access 쿠키가 만료돼도 refresh 쿠키가
    살아있으면 middleware가 통과시키고 api interceptor가 조용히 갱신한다(잦은 재로그인 방지).
    """
    common = {
        "httponly": True,
        "secure": is_production,
        "samesite": "lax",
        "path": "/",
    }
    # refresh/id 쿠키 수명 — AuthFusion refresh_expires_in 우선, 없으면 expires_in*4.
    refresh_max_age = token.refresh_expires_in or (
        token.expires_in * 4 if token.expires_in else None
    )
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
            max_age=refresh_max_age,
            **common,
        )
    if token.id_token:
        # logout 시 IdP end_session의 id_token_hint로 사용 (ADR-022).
        response.set_cookie(
            _ID_TOKEN_COOKIE,
            token.id_token,
            max_age=refresh_max_age,
            **common,
        )


def _clear_cookies(response: Response, *, is_production: bool) -> None:
    for name in (_ACCESS_COOKIE, _REFRESH_COOKIE, _ID_TOKEN_COOKIE):
        response.delete_cookie(name, path="/", secure=is_production, httponly=True)


def _redirect_uri(auth_cfg: AuthConfig) -> str:
    base = auth_cfg.app_base_url.rstrip("/")
    return f"{base}{auth_cfg.redirect_path}"


def _resolve_client_id(auth_cfg: AuthConfig, domain_id: str) -> str:
    ov = auth_cfg.tenant_overrides.get(domain_id) or {}
    cid = ov.get("client_id")
    if not cid:
        # client_tenant_map 역방향 탐색 (mapped는 list[str])
        for client_id, mapped in auth_cfg.client_tenant_map.items():
            if domain_id in mapped:
                return client_id
        raise HTTPException(
            status_code=404,
            detail={"error": "client_id_not_configured", "domain_id": domain_id},
        )
    return str(cid)


# ----------------------------------------------------------------------------
# /authorize/{domain_id} — Frontend가 SSO redirect를 시작하기 전 호출
# ----------------------------------------------------------------------------


@router.get("/authorize/{domain_id}")
async def build_authorize_url(
    domain_id: str,
    redirect: bool = Query(default=False),
    prompt: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
    state_store: InMemoryOAuthStateStore = Depends(get_oauth_state_store),
):
    """ADR-018 §2 — PKCE pair 생성 + state 저장 + authorize URL 반환.

    기본은 JSON({authorize_url, state, domain_id}) — frontend SPA가 직접
    navigation을 다룰 때. `?redirect=1`이면 그 URL로 302 redirect — Next.js
    middleware나 일반 브라우저 navigation이 그대로 사용 가능.

    `?prompt=login`이면 OIDC prompt 파라미터를 IdP authorize에 전달 — 활성 SSO
    세션이 있어도 재인증(로그인 폼)을 강제한다(계정 전환용, ADR-018).
    """
    auth_cfg = AuthConfigLoader.load(settings)

    if not auth_cfg.authorize_endpoint:
        raise HTTPException(
            status_code=500, detail={"error": "authorize_endpoint_not_configured"}
        )

    # domain_id 화이트리스트 — auth configs (configs/tenants/<tid>/auth.yaml) 또는
    # platform map에 등록된 경우만 허용. 임의 문자열로 SSO 흐름 트리거 방지.
    is_known_tenant = (
        domain_id in auth_cfg.tenant_overrides
        or any(domain_id in mapped for mapped in auth_cfg.client_tenant_map.values())
    )
    if not is_known_tenant:
        raise HTTPException(
            status_code=404,
            detail={"error": "tenant_not_registered", "domain_id": domain_id},
        )

    client_id = _resolve_client_id(auth_cfg, domain_id)
    verifier, challenge = generate_pkce_pair()
    state = generate_state()
    redirect_uri = _redirect_uri(auth_cfg)

    state_store.put(
        OAuthStateEntry(
            state=state,
            domain_id=domain_id,
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
    # OIDC prompt (login|none|consent|select_account) — 계정 전환 시 prompt=login.
    if prompt in {"login", "none", "consent", "select_account"}:
        params["prompt"] = prompt
    authorize_url = f"{auth_cfg.authorize_endpoint}?{urlencode(params)}"
    if redirect:
        return RedirectResponse(url=authorize_url, status_code=302)
    return {
        "authorize_url": authorize_url,
        "state": state,
        "domain_id": domain_id,
    }


# ----------------------------------------------------------------------------
# /callback — AuthFusion이 code/state를 가지고 frontend로 redirect 후 frontend가 호출
# ----------------------------------------------------------------------------


class CallbackResponse(BaseModel):
    domain_id: str
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
        domain_id=entry.domain_id,
        expires_in=token.expires_in,
        token_type=token.token_type,
        scope=token.scope,
    )


# ----------------------------------------------------------------------------
# /me — 현재 로그인 사용자 (cross-tenant, RBAC 메뉴 결정용)
# ----------------------------------------------------------------------------


@router.get("/me")
async def auth_me(user: UserContext = Depends(get_user_context_no_tenant)):
    """현재 로그인 사용자 (ADR-016 §3 Y9 — frontend RBAC 결정 단일 진입점).

    path tenant 무관. PLATFORM_ADMIN/ADMIN/USER role을 frontend가 알아야 메뉴
    필터링 가능. /api/{tid}/me는 동일 정보를 tenant scope에서 제공하지만
    frontend는 본 endpoint 하나로 단일화.
    """
    return {
        "user_id": user.user_id,
        "domain_id": user.domain_id,
        "roles": user.roles,
        "is_admin": user.is_admin,
        "is_auditor": user.is_auditor,
        "is_platform_admin": user.is_platform_admin,
        "clearance": user.clearance,
        "department": user.department,
        "domain_groups": user.domain_groups,
        "preferred_username": user.preferred_username,
        "email": user.email,
    }


@router.get("/me/domains")
async def auth_me_domains(
    user: UserContext = Depends(get_user_context_no_tenant),
    tenant_service=Depends(get_tenant_lifecycle_service),
    membership_service=Depends(get_membership_service),
):
    """ADR-022 §7 — 내가 접근 가능한 도메인 목록 (frontend 도메인 switcher).

    - admin/auditor/platform_admin: 모든 active 도메인 (전역 역할).
    - 일반 user: 내 membership 도메인 + enrollment_policy=open 도메인.

    각 항목의 `access`는 어떻게 접근권을 얻었는지: global(전역역할) | member(배정) | open(개방).
    """
    active = await tenant_service.list_(status="active", limit=500, offset=0)
    is_global = user.is_admin or user.is_auditor or user.is_platform_admin

    if is_global:
        items = [
            {
                "domain_id": t.domain_id,
                "display_name": t.display_name,
                "enrollment_policy": t.enrollment_policy,
                "access": "global",
            }
            for t in active
        ]
        return {"items": items, "is_global": True}

    memberships = await membership_service.list_for_user(user.user_id)
    member_ids = {m["domain_id"] for m in memberships}
    items = []
    for t in active:
        if t.domain_id in member_ids:
            access = "member"
        elif t.enrollment_policy == "open":
            access = "open"
        else:
            continue
        items.append(
            {
                "domain_id": t.domain_id,
                "display_name": t.display_name,
                "enrollment_policy": t.enrollment_policy,
                "access": access,
            }
        )
    return {"items": items, "is_global": False}


# ----------------------------------------------------------------------------
# /account/* — AuthFusion self-service API proxy (spec: docs/integration/
# authfusion-self-service-v1.md, 11 endpoints under /api/v1/me/*)
# ----------------------------------------------------------------------------

_ACCOUNT_ALLOWED_PATHS = {
    ("GET", ""),
    ("GET", "summary"),
    ("GET", "applications"),
    ("GET", "sessions"),
    ("GET", "mfa/status"),
    ("POST", "mfa/setup"),
    ("POST", "mfa/verify-setup"),
    ("POST", "mfa/disable"),
    ("POST", "mfa/recovery-codes/regenerate"),
    ("POST", "change-password"),
}
# DELETE /sessions/{sessionId} — UUID 패턴이라 별도 매칭
import re as _re

_SESSION_DELETE_RE = _re.compile(r"^sessions/[0-9a-fA-F-]{8,}$")


def _account_path_allowed(method: str, suffix: str) -> bool:
    if (method, suffix) in _ACCOUNT_ALLOWED_PATHS:
        return True
    if method == "DELETE" and _SESSION_DELETE_RE.match(suffix):
        return True
    return False


@router.api_route(
    "/account",
    methods=["GET"],
    include_in_schema=False,
)
@router.api_route(
    "/account/{path:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
async def authfusion_self_service_proxy(
    request: Request,
    path: str = "",
    settings: Settings = Depends(get_settings),
):
    """AuthFusion `/api/v1/me/*` 11 endpoints의 thin proxy.

    cookie의 access_token을 Bearer header로 forward + AuthFusion 응답을 그대로
    client에 전달 (status·body·content-type 보존). spec drift 시 docs/integration/
    authfusion-self-service-v1.md 비교.
    """
    import httpx

    suffix = path.strip("/")
    if not _account_path_allowed(request.method, suffix):
        raise HTTPException(
            status_code=404,
            detail={"error": "endpoint_not_allowed", "path": path, "method": request.method},
        )

    access = request.cookies.get(_ACCESS_COOKIE)
    if not access:
        raise HTTPException(status_code=401, detail={"error": "missing_token"})

    base = (settings.authfusion_issuer or "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=500, detail={"error": "authfusion_issuer_not_configured"}
        )

    upstream = f"{base}/api/v1/me/{suffix}" if suffix else f"{base}/api/v1/me"
    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            res = await client.request(
                request.method,
                upstream,
                content=body,
                headers={
                    "Authorization": f"Bearer {access}",
                    "Content-Type": request.headers.get(
                        "Content-Type", "application/json"
                    ),
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail={"error": "idp_unreachable", "reason": str(exc)},
            ) from exc

    from fastapi.responses import Response as _Resp

    return _Resp(
        content=res.content,
        status_code=res.status_code,
        media_type=res.headers.get("content-type", "application/json"),
    )


# ----------------------------------------------------------------------------
# /refresh
# ----------------------------------------------------------------------------


class RefreshRequest(BaseModel):
    # 명시적 refresh_token 또는 cookie 사용. body 우선.
    refresh_token: str | None = None
    domain_id: str  # client_id 매핑에 필요


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

    client_id = _resolve_client_id(auth_cfg, req.domain_id)
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
        "domain_id": req.domain_id,
        "expires_in": token.expires_in,
        "token_type": token.token_type,
    }


# ----------------------------------------------------------------------------
# /logout
# ----------------------------------------------------------------------------


class LogoutRequest(BaseModel):
    domain_id: str


@router.post("/logout")
async def logout(
    req: LogoutRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    token_client: AuthFusionTokenClient | None = Depends(get_authfusion_token_client),
):
    """ADR-018 §6 + ADR-022 — token revoke + 쿠키 삭제 + IdP 세션 종료 URL 반환.

    SP 쿠키만 지우면 AuthFusion SSO 세션이 살아있어 다음 /console 진입 시 *재로그인 없이*
    silent 재인증된다(로그아웃이 안 되는 것처럼 보임). 따라서 OIDC RP-Initiated Logout
    (end_session_endpoint)으로 IdP 세션까지 종료시킨다. frontend는 반환된 logout_url로
    브라우저를 이동시킨다. end_session 미설정 시 logout_url=None → frontend는 landing으로.
    """
    auth_cfg = AuthConfigLoader.load(settings)
    is_production = settings.env == "production"

    access = request.cookies.get(_ACCESS_COOKIE)
    refresh = request.cookies.get(_REFRESH_COOKIE)
    client_id = _resolve_client_id(auth_cfg, req.domain_id)

    if token_client is not None:
        if access:
            await token_client.revoke(
                token=access, client_id=client_id, token_type_hint="access_token"
            )
        if refresh:
            await token_client.revoke(
                token=refresh, client_id=client_id, token_type_hint="refresh_token"
            )

    _clear_cookies(response, is_production=is_production)

    logout_url: str | None = None
    if auth_cfg.end_session_endpoint:
        params = {"client_id": client_id}
        # AuthFusion은 id_token_hint를 요구 (OIDC RP-Initiated Logout §2).
        id_token = request.cookies.get(_ID_TOKEN_COOKIE)
        if id_token:
            params["id_token_hint"] = id_token
        # post_logout_redirect_uri는 AuthFusion client에 사전 등록된 경우에만 첨부.
        # 미등록 시 AuthFusion이 "사전 등록되지 않음" 에러로 logout 자체를 중단하므로,
        # 등록 전에는 생략한다(IdP가 자체 logged-out 페이지 표시). 등록 후 켜려면
        # configs/platform/auth.yaml의 post_logout_redirect_uri_registered=true.
        if auth_cfg.post_logout_redirect_registered:
            params["post_logout_redirect_uri"] = f"{auth_cfg.app_base_url.rstrip('/')}/"
        logout_url = f"{auth_cfg.end_session_endpoint}?{urlencode(params)}"
    return {"logout_url": logout_url}
