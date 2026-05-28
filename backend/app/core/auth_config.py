"""AuthConfig — configs/platform/auth.yaml + configs/tenants/<tid>/auth.yaml 합성 (ADR-018).

본 모듈은 application-level config (Settings에 의존하는 env vars)와 분리.
auth.yaml의 `*_env` 키는 Settings에서 해당 env 값을 읽어 치환한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class ServiceAccountConfig:
    """ADR-018 §7 — system call용 service account."""

    client_id: str
    domain_id: str
    roles: list[str]
    clearance: str = "internal"


@dataclass(frozen=True)
class AuthConfig:
    """런타임 auth config (불변)."""

    auth_mode: str  # "mock" | "oidc"

    # AuthFusion (discovery 또는 env로 해석된 값)
    issuer: str
    jwks_uri: str
    token_endpoint: str | None
    revoke_endpoint: str | None
    authorize_endpoint: str | None
    userinfo_endpoint: str | None  # AuthFusion OIDC discovery에서 채워짐
    end_session_endpoint: str | None  # OIDC RP-initiated logout (ADR-022) — IdP 세션 종료
    discovery_url: str | None  # 미설정 시 None (env override fallback)
    scopes: list[str]
    state_ttl_seconds: int
    app_base_url: str
    redirect_path: str
    jwt_algorithm: str
    jwks_cache_ttl_seconds: int
    platform_admin_role: str

    # OIDC CONFIDENTIAL client credentials.
    # AuthFusion 등록 응답의 clientId(UUID) + clientSecret(평문 1회).
    client_id: str | None
    client_secret: str | None

    # email_verified=true 강제 여부 (AuthFusion 표준 동작)
    require_verified_email: bool

    # service-scoped role prefix. AuthFusion RP_ONBOARDING.md ADR-0019:
    # <rp-slug>-admin / <rp-slug>-auditor / <rp-slug>-user 로만 매칭, 전역 ADMIN 금지.
    rp_slug: str  # e.g. "domainrag-ops"

    # 1:N 매핑 — 한 client_id가 여러 tenant를 서비스하는 single-client multi-tenant
    # 운영 패턴 지원. 로딩 시 단일 string value도 [string]으로 normalize.
    client_tenant_map: dict[str, list[str]]
    service_accounts: dict[str, ServiceAccountConfig]

    # Tenant overlays (domain_id → tenant 자체의 auth.yaml 내용)
    tenant_overrides: dict[str, dict] = field(default_factory=dict)

    # ADR-0019 strict mode를 풀어 legacy USER/ADMIN/AUDITOR claim도 매칭 (dev/test).
    # 운영(authfusion 모드 + 다중 RP)에선 false로 강제 권장 — 다른 RP의 admin이 들어와도 차단.
    accept_legacy_global_roles: bool = True

    def client_id_to_domain_id(
        self, client_id: str, expected_domain_id: str | None = None
    ) -> str:
        """ADR-018 §1 + single-client multi-tenant 보강.

        - client_id가 map에 있고 expected_domain_id가 list에 포함되면 expected 반환
          (single-client 공유 시 path의 tenant 의도를 신뢰).
        - expected_domain_id가 None이거나 list에 없으면 list[0] 반환 — caller가 비교해
          mismatch를 판단.
        - map에 없으면 legacy `client-<tenant>` prefix 제거 fallback (dev/test).
        """
        mapped = self.client_tenant_map.get(client_id)
        if mapped:
            if expected_domain_id and expected_domain_id in mapped:
                return expected_domain_id
            return mapped[0]
        return client_id.removeprefix("client-")

    def extra_admin_roles(self, domain_id: str) -> list[str]:
        """tenant.auth.yaml.extra_admin_roles."""
        ov = self.tenant_overrides.get(domain_id, {})
        roles = ov.get("extra_admin_roles") or []
        return list(roles)

    def map_oidc_role(self, role: str) -> str | None:
        """RP_ONBOARDING.md ADR-0019 — prefix-only 매칭.

        AuthFusion `<rp-slug>-admin/auditor/user` → DomainRAG `ADMIN/AUDITOR/USER`.
        전역 `ADMIN` claim은 매칭하지 않는다 (다른 RP의 admin이 DomainRAG admin이 되는 사고 방지).
        PLATFORM_ADMIN은 전역 role이라 예외 (AuthFusion platform_admin_role과 일치 시).

        `accept_legacy_global_roles=True`이면 USER/ADMIN/AUDITOR도 그대로 통과 (dev/test).
        """
        upper = role.upper()
        lower = role.lower()
        slug = self.rp_slug.lower()
        if lower == self.platform_admin_role.lower():
            return "PLATFORM_ADMIN"
        if slug:
            if lower == f"{slug}-admin":
                return "ADMIN"
            if lower == f"{slug}-auditor":
                return "AUDITOR"
            if lower == f"{slug}-user":
                return "USER"
        if self.accept_legacy_global_roles and upper in {"USER", "ADMIN", "AUDITOR"}:
            return upper
        return None


class AuthConfigLoader:
    """auth.yaml 합성 + 캐싱."""

    _cache: AuthConfig | None = None
    _lock = Lock()

    @classmethod
    def load(cls, settings: Settings | None = None) -> AuthConfig:
        with cls._lock:
            if cls._cache is not None:
                return cls._cache

        settings = settings or get_settings()
        config_dir: Path = settings.config_dir.resolve()
        platform_yaml = config_dir / "platform" / "auth.yaml"
        if not platform_yaml.exists():
            raise FileNotFoundError(f"auth.yaml not found: {platform_yaml}")

        raw = _read_yaml(platform_yaml)

        auth_mode = _resolve_env_string(raw.get("auth_mode", "oidc"), settings) or settings.auth_mode

        af = raw.get("oidc") or {}
        issuer = _resolve_env_ref(af.get("issuer_env"), settings) or settings.authfusion_issuer
        # Discovery — explicit env > yaml > issuer 합성. discovery는 lazy fetch이므로 None 허용.
        discovery_url = (
            getattr(settings, "authfusion_discovery_url", None)
            or _resolve_env_ref(af.get("discovery_url_env"), settings)
            or (f"{issuer.rstrip('/')}/.well-known/openid-configuration" if issuer else None)
        )
        # OIDC discovery로 endpoint 일괄 fetch — 실패해도 env override fallback.
        # 운영(authfusion 모드)에서만 시도. mock·dev는 issuer 미존재로 skip.
        discovered: dict[str, Any] = {}
        if auth_mode == "oidc" and discovery_url:
            discovered = _fetch_discovery(discovery_url)

        jwks_uri = (
            _resolve_env_ref(af.get("jwks_uri_env"), settings)
            or getattr(settings, "authfusion_jwks_uri", None)
            or discovered.get("jwks_uri")
            or f"{issuer.rstrip('/')}/.well-known/jwks.json"
        )
        token_endpoint = (
            _resolve_env_ref(af.get("token_endpoint_env"), settings)
            or getattr(settings, "authfusion_token_endpoint", None)
            or discovered.get("token_endpoint")
        )
        revoke_endpoint = (
            _resolve_env_ref(af.get("revoke_endpoint_env"), settings)
            or getattr(settings, "authfusion_revoke_endpoint", None)
            or discovered.get("revocation_endpoint")
        )
        authorize_endpoint = (
            _resolve_env_ref(af.get("authorize_endpoint_env"), settings)
            or getattr(settings, "authfusion_authorize_endpoint", None)
            or discovered.get("authorization_endpoint")
        )
        userinfo_endpoint = (
            _resolve_env_ref(af.get("userinfo_endpoint_env"), settings)
            or discovered.get("userinfo_endpoint")
        )
        end_session_endpoint = (
            _resolve_env_ref(af.get("end_session_endpoint_env"), settings)
            or getattr(settings, "authfusion_end_session_endpoint", None)
            or discovered.get("end_session_endpoint")
        )
        scopes = list(af.get("scopes") or ["openid", "profile", "email"])
        state_ttl_seconds = int(af.get("state_ttl_seconds", 600))

        # OIDC client credentials — KeyHub ref 또는 env 직접 주입.
        # AuthFusion guide §3 — RP `.env`의 OIDC_CLIENT_ID/SECRET.
        client_id = (
            _resolve_env_ref(af.get("client_id_env"), settings)
            or getattr(settings, "authfusion_client_id", None)
        )
        client_secret = (
            _resolve_env_ref(af.get("client_secret_env"), settings)
            or getattr(settings, "authfusion_client_secret", None)
        )

        require_verified_email = bool(
            af.get(
                "require_verified_email",
                getattr(settings, "oidc_require_verified_email", True),
            )
        )
        rp_slug = str(af.get("rp_slug", "domainrag-ops"))
        accept_legacy = bool(af.get("accept_legacy_global_roles", True))

        callback_cfg = raw.get("oauth2_callback") or {}
        redirect_path = str(callback_cfg.get("redirect_path", "/auth/callback"))
        app_base_url = _resolve_env_string(
            callback_cfg.get("app_base_url"), settings
        ) or _settings_attr(settings, "APP_BASE_URL") or "http://localhost:3010"

        # client_tenant_map normalize — yaml은 string도 list도 받아들이지만
        # 내부 표현은 항상 list (single-client multi-tenant 지원).
        client_tenant_map: dict[str, list[str]] = {}
        for cid, mapped in (af.get("client_tenant_map") or {}).items():
            if isinstance(mapped, list):
                client_tenant_map[cid] = [str(t) for t in mapped if t]
            elif mapped:
                client_tenant_map[cid] = [str(mapped)]

        service_accounts: dict[str, ServiceAccountConfig] = {}
        for cid, body in (af.get("service_accounts") or {}).items():
            service_accounts[cid] = ServiceAccountConfig(
                client_id=cid,
                domain_id=body.get("domain_id", "platform"),
                roles=list(body.get("roles") or []),
                clearance=body.get("clearance", "internal"),
            )

        # tenant overrides — 모든 tenants/<id>/auth.yaml을 미리 읽어 둠.
        # 같은 client_id를 여러 tenant가 공유하면 list에 append (1:N).
        tenant_overrides: dict[str, dict] = {}
        tenants_dir = config_dir / "tenants"
        if tenants_dir.exists():
            for tdir in tenants_dir.iterdir():
                tyaml = tdir / "auth.yaml"
                if tyaml.is_file():
                    tenant_overrides[tdir.name] = _read_yaml(tyaml)
                    cid = tenant_overrides[tdir.name].get("client_id")
                    if cid:
                        bucket = client_tenant_map.setdefault(cid, [])
                        if tdir.name not in bucket:
                            bucket.append(tdir.name)

        cfg = AuthConfig(
            auth_mode=auth_mode,
            issuer=issuer,
            jwks_uri=jwks_uri,
            token_endpoint=token_endpoint,
            revoke_endpoint=revoke_endpoint,
            authorize_endpoint=authorize_endpoint,
            userinfo_endpoint=userinfo_endpoint,
            end_session_endpoint=end_session_endpoint,
            discovery_url=discovery_url,
            scopes=scopes,
            state_ttl_seconds=state_ttl_seconds,
            app_base_url=app_base_url,
            redirect_path=redirect_path,
            jwt_algorithm=af.get("jwt_algorithm", "RS256"),
            jwks_cache_ttl_seconds=int(af.get("jwks_cache_ttl_seconds", 3600)),
            platform_admin_role=af.get("platform_admin_role", "PLATFORM_ADMIN"),
            client_id=client_id,
            client_secret=client_secret,
            require_verified_email=require_verified_email,
            rp_slug=rp_slug,
            accept_legacy_global_roles=accept_legacy,
            client_tenant_map=client_tenant_map,
            service_accounts=service_accounts,
            tenant_overrides=tenant_overrides,
        )

        with cls._lock:
            cls._cache = cfg
        return cfg

    @classmethod
    def reset(cls) -> None:
        """테스트용 cache invalidate. discovery cache까지 함께 비운다."""
        with cls._lock:
            cls._cache = None
        _discovery_cache.clear()


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_discovery_cache: dict[str, dict] = {}


def _fetch_discovery(url: str) -> dict:
    """OIDC discovery — process-life cache (config은 process 동안 immutable).

    실패는 swallow + 빈 dict. caller가 env override fallback으로 복구.
    """
    if url in _discovery_cache:
        return _discovery_cache[url]
    try:
        import httpx

        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url)
        resp.raise_for_status()
        data = resp.json() or {}
        if isinstance(data, dict):
            _discovery_cache[url] = data
            return data
    except Exception:  # noqa: BLE001 — discovery 실패는 fail-fast 아님
        pass
    return {}


def _resolve_env_string(value: object, settings: Settings) -> str | None:
    """`${VAR}` literal을 settings 또는 OS env로 치환. 그 외는 그대로 반환."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.startswith("${") and s.endswith("}"):
        var = s[2:-1]
        # default 형식 ${VAR:-fallback}
        if ":-" in var:
            var, fallback = var.split(":-", 1)
        else:
            fallback = ""
        return _settings_attr(settings, var) or fallback or None
    return s


def _resolve_env_ref(env_var: str | None, settings: Settings) -> str | None:
    """`*_env: AUTHFUSION_ISSUER` 식의 참조 → Settings 속성 또는 OS env."""
    if not env_var:
        return None
    return _settings_attr(settings, env_var)


def _settings_attr(settings: Settings, env_var: str) -> str | None:
    """env_var (대문자 OS env name)로 Settings 속성을 찾는다.

    Settings 필드의 alias가 env_var와 일치하면 해당 값 반환.
    못 찾으면 OS env에서 직접 lookup (Settings에 없는 변수도 지원).
    """
    import os

    target = env_var.lower()
    fields = type(settings).model_fields
    for fname, finfo in fields.items():
        alias = finfo.alias or fname
        if alias.upper() == env_var or fname.lower() == target:
            val = getattr(settings, fname, None)
            if val:
                return str(val)
    return os.environ.get(env_var)
