"""AuthConfig — configs/platform/auth.yaml + configs/tenants/<tid>/auth.yaml 합성 (ADR-018).

본 모듈은 application-level config (Settings에 의존하는 env vars)와 분리.
auth.yaml의 `*_env` 키는 Settings에서 해당 env 값을 읽어 치환한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

import yaml

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class ServiceAccountConfig:
    """ADR-018 §7 — system call용 service account."""

    client_id: str
    tenant_id: str
    roles: list[str]
    clearance: str = "internal"


@dataclass(frozen=True)
class MockUserConfig:
    """auth.yaml mock.default_user."""

    user_id: str
    tenant_id: str
    roles: list[str]
    clearance: str
    department: str | None
    domain_groups: list[str]
    preferred_username: str | None
    email: str | None


@dataclass(frozen=True)
class AuthConfig:
    """런타임 auth config (불변)."""

    auth_mode: str  # "mock" | "authfusion"

    # AuthFusion (env에서 해석된 값)
    issuer: str
    jwks_uri: str
    token_endpoint: str | None
    revoke_endpoint: str | None
    authorize_endpoint: str | None
    scopes: list[str]
    state_ttl_seconds: int
    app_base_url: str
    redirect_path: str
    jwt_algorithm: str
    jwks_cache_ttl_seconds: int
    platform_admin_role: str

    client_tenant_map: dict[str, str]
    service_accounts: dict[str, ServiceAccountConfig]

    # Mock
    mock_default_user: MockUserConfig | None
    mock_forbid_in_production: bool

    # Tenant overlays (tenant_id → tenant 자체의 auth.yaml 내용)
    tenant_overrides: dict[str, dict] = field(default_factory=dict)

    def client_id_to_tenant_id(self, client_id: str) -> str:
        """ADR-018 §1 — mapping table 우선, fallback prefix 제거."""
        if client_id in self.client_tenant_map:
            return self.client_tenant_map[client_id]
        return client_id.removeprefix("client-")

    def extra_admin_roles(self, tenant_id: str) -> list[str]:
        """tenant.auth.yaml.extra_admin_roles."""
        ov = self.tenant_overrides.get(tenant_id, {})
        roles = ov.get("extra_admin_roles") or []
        return list(roles)


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

        auth_mode = _resolve_env_string(raw.get("auth_mode", "mock"), settings) or settings.auth_mode

        af = raw.get("authfusion") or {}
        issuer = _resolve_env_ref(af.get("issuer_env"), settings) or settings.authfusion_issuer
        jwks_uri = _resolve_env_ref(af.get("jwks_uri_env"), settings) or settings.authfusion_jwks_uri
        token_endpoint = _resolve_env_ref(af.get("token_endpoint_env"), settings)
        revoke_endpoint = _resolve_env_ref(af.get("revoke_endpoint_env"), settings)
        authorize_endpoint = _resolve_env_ref(
            af.get("authorize_endpoint_env"), settings
        )
        scopes = list(af.get("scopes") or ["openid", "profile", "email"])
        state_ttl_seconds = int(af.get("state_ttl_seconds", 600))

        callback_cfg = raw.get("oauth2_callback") or {}
        redirect_path = str(callback_cfg.get("redirect_path", "/auth/callback"))
        app_base_url = _resolve_env_string(
            callback_cfg.get("app_base_url"), settings
        ) or _settings_attr(settings, "APP_BASE_URL") or "http://localhost:3000"

        client_tenant_map: dict[str, str] = dict(af.get("client_tenant_map") or {})
        service_accounts: dict[str, ServiceAccountConfig] = {}
        for cid, body in (af.get("service_accounts") or {}).items():
            service_accounts[cid] = ServiceAccountConfig(
                client_id=cid,
                tenant_id=body.get("tenant_id", "platform"),
                roles=list(body.get("roles") or []),
                clearance=body.get("clearance", "internal"),
            )

        mock = raw.get("mock") or {}
        du = mock.get("default_user") or {}
        mock_default_user = (
            MockUserConfig(
                user_id=du.get("user_id", "dev-user-001"),
                tenant_id=du.get("tenant_id", "security"),
                roles=list(du.get("roles") or []),
                clearance=du.get("clearance", "internal"),
                department=du.get("department"),
                domain_groups=list(du.get("domain_groups") or []),
                preferred_username=du.get("preferred_username"),
                email=du.get("email"),
            )
            if du
            else None
        )

        # tenant overrides — 모든 tenants/<id>/auth.yaml을 미리 읽어 둠
        tenant_overrides: dict[str, dict] = {}
        tenants_dir = config_dir / "tenants"
        if tenants_dir.exists():
            for tdir in tenants_dir.iterdir():
                tyaml = tdir / "auth.yaml"
                if tyaml.is_file():
                    tenant_overrides[tdir.name] = _read_yaml(tyaml)
                    # tenant auth.yaml의 client_id가 platform map에 없으면 자동 등록
                    cid = tenant_overrides[tdir.name].get("client_id")
                    if cid and cid not in client_tenant_map:
                        client_tenant_map[cid] = tdir.name

        cfg = AuthConfig(
            auth_mode=auth_mode,
            issuer=issuer,
            jwks_uri=jwks_uri,
            token_endpoint=token_endpoint,
            revoke_endpoint=revoke_endpoint,
            authorize_endpoint=authorize_endpoint,
            scopes=scopes,
            state_ttl_seconds=state_ttl_seconds,
            app_base_url=app_base_url,
            redirect_path=redirect_path,
            jwt_algorithm=af.get("jwt_algorithm", "RS256"),
            jwks_cache_ttl_seconds=int(af.get("jwks_cache_ttl_seconds", 3600)),
            platform_admin_role=af.get("platform_admin_role", "PLATFORM_ADMIN"),
            client_tenant_map=client_tenant_map,
            service_accounts=service_accounts,
            mock_default_user=mock_default_user,
            mock_forbid_in_production=bool(mock.get("forbid_in_production", True)),
            tenant_overrides=tenant_overrides,
        )

        with cls._lock:
            cls._cache = cfg
        return cfg

    @classmethod
    def reset(cls) -> None:
        """테스트용 cache invalidate."""
        with cls._lock:
            cls._cache = None


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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
