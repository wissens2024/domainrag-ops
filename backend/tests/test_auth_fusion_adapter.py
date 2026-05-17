"""AuthFusionAdapter — JWT 검증 + tenant_mismatch + service account + membership 보강."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwk as jose_jwk
from jose import jwt as jose_jwt

from app.core.auth_adapter import AuthFusionAdapter
from app.core.auth_config import AuthConfigLoader
from app.core.config import get_settings
from tests._fixtures.mock_auth import MockAuthAdapter


# ---------------------------------------------------------------------------
# RSA + JWKS 픽스처
# ---------------------------------------------------------------------------


@dataclass
class _KeyMaterial:
    kid: str
    private_pem: str
    jwk_dict: dict


def _generate_rsa_key(kid: str = "test-kid-1") -> _KeyMaterial:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    public_jwk = jose_jwk.construct(public_pem, algorithm="RS256").to_dict()
    public_jwk["kid"] = kid
    public_jwk["alg"] = "RS256"
    public_jwk["use"] = "sig"
    return _KeyMaterial(kid=kid, private_pem=private_pem, jwk_dict=public_jwk)


class _FakeJWKSClient:
    """JWKSClient 인터페이스만 흉내 — get_key(kid)."""

    def __init__(self, keys: dict[str, dict]):
        self._keys = keys

    async def get_key(self, kid: str, *, force_refresh: bool = False) -> dict:
        from app.core.jwks_client import JWKSFetchError

        key = self._keys.get(kid)
        if key is None:
            raise JWKSFetchError(f"unknown kid: {kid}")
        return key


def _issue_token(km: _KeyMaterial, claims: dict) -> str:
    return jose_jwt.encode(
        claims,
        km.private_pem,
        algorithm="RS256",
        headers={"kid": km.kid},
    )


# ---------------------------------------------------------------------------
# 가짜 DB session — user_tenant_membership 조회 mock
# ---------------------------------------------------------------------------


class _MembershipRow:
    def __init__(self, clearance: str, department: str | None, domain_groups: list[str]):
        self.clearance = clearance
        self.department = department
        self.domain_groups = domain_groups


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, store: dict[tuple[str, str], _MembershipRow]):
        self._store = store
        self._captured_user: str | None = None
        self._captured_tenant: str | None = None

    async def execute(self, stmt):
        # 단순화 — 마지막 호출의 user_id/tenant_id를 stmt.compile()에서 추출하지 않고,
        # set_keys로 직접 검색을 강제. (테스트마다 한 번씩 호출이므로 store에서 임의 매칭)
        # 더 정확히는 stmt를 sqlalchemy의 _whereclause에서 파싱해야 하지만, 통합 테스트에선 충분.
        # store가 비어있으면 None.
        if not self._store:
            return _FakeResult(None)
        # 한 항목만 있다고 가정 (테스트당 하나씩 wire)
        row = next(iter(self._store.values()))
        return _FakeResult(row)


def _make_session_factory(store: dict[tuple[str, str], _MembershipRow] | None):
    @asynccontextmanager
    async def _cm():
        yield _FakeSession(store or {})

    return _cm


# ---------------------------------------------------------------------------
# 공통 fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_caches():
    AuthConfigLoader.reset()
    yield
    AuthConfigLoader.reset()


@pytest.fixture
def km() -> _KeyMaterial:
    return _generate_rsa_key("test-kid-1")


@pytest.fixture
def auth_config():
    return AuthConfigLoader.load(get_settings())


def _make_adapter(km: _KeyMaterial, store=None, ledger_audit=None) -> AuthFusionAdapter:
    cfg = AuthConfigLoader.load(get_settings())
    return AuthFusionAdapter(
        settings=get_settings(),
        auth_config=cfg,
        jwks_client=_FakeJWKSClient({km.kid: km.jwk_dict}),
        session_factory=_make_session_factory(store),
        ledger_audit=ledger_audit,
    )


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# 일반 user 시나리오
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_user_with_membership_returns_context(km, auth_config):
    store = {
        ("user-001", "security"): _MembershipRow(
            clearance="confidential",
            department="security",
            domain_groups=["group:security", "group:it-admin"],
        )
    }
    adapter = _make_adapter(km, store=store)
    token = _issue_token(
        km,
        {
            "iss": auth_config.issuer,
            "sub": "user-001",
            "client_id": "client-security",
            "roles": ["USER", "ADMIN"],
            "preferred_username": "alice",
            "email": "alice@example.com",
            "exp": _now() + 600,
            "iat": _now(),
        },
    )
    ctx = await adapter.verify_and_extract(token, "security")
    assert ctx.user_id == "user-001"
    assert ctx.tenant_id == "security"
    assert ctx.clearance == "confidential"
    assert ctx.department == "security"
    assert "group:security" in ctx.domain_groups
    assert ctx.is_admin is True
    assert ctx.is_platform_admin is False
    assert ctx.is_service_account is False
    assert ctx.preferred_username == "alice"


@pytest.mark.asyncio
async def test_tenant_mismatch_403(km, auth_config):
    adapter = _make_adapter(km, store={})
    token = _issue_token(
        km,
        {
            "iss": auth_config.issuer,
            "sub": "user-001",
            "client_id": "client-security",  # token = security
            "roles": ["USER"],
            "exp": _now() + 600,
        },
    )
    with pytest.raises(HTTPException) as ei:
        await adapter.verify_and_extract(token, "legal")  # path = legal
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "tenant_mismatch"


@pytest.mark.asyncio
async def test_no_membership_403(km, auth_config):
    """tenant 일치하지만 user_tenant_membership에 row 없음 → 403."""
    adapter = _make_adapter(km, store={})  # 빈 store
    token = _issue_token(
        km,
        {
            "iss": auth_config.issuer,
            "sub": "user-001",
            "client_id": "client-security",
            "roles": ["USER"],
            "exp": _now() + 600,
        },
    )
    with pytest.raises(HTTPException) as ei:
        await adapter.verify_and_extract(token, "security")
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "no_tenant_membership"


@pytest.mark.asyncio
async def test_expired_token_raises_401(km, auth_config):
    adapter = _make_adapter(km, store={})
    token = _issue_token(
        km,
        {
            "iss": auth_config.issuer,
            "sub": "user-001",
            "client_id": "client-security",
            "roles": ["USER"],
            "exp": _now() - 60,  # 이미 만료
        },
    )
    with pytest.raises(HTTPException) as ei:
        await adapter.verify_and_extract(token, "security")
    assert ei.value.status_code == 401
    assert ei.value.detail["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_invalid_issuer_raises_401(km):
    adapter = _make_adapter(km, store={})
    token = _issue_token(
        km,
        {
            "iss": "https://not-our-sso.example",
            "sub": "user-001",
            "client_id": "client-security",
            "roles": ["USER"],
            "exp": _now() + 600,
        },
    )
    with pytest.raises(HTTPException) as ei:
        await adapter.verify_and_extract(token, "security")
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_signature_mismatch_raises_401(km, auth_config):
    """다른 키로 서명한 토큰을 같은 kid로 보내면 검증 실패."""
    other = _generate_rsa_key("test-kid-1")  # 같은 kid, 다른 키
    adapter = _make_adapter(km, store={})
    token = _issue_token(
        other,
        {
            "iss": auth_config.issuer,
            "sub": "user-001",
            "client_id": "client-security",
            "roles": ["USER"],
            "exp": _now() + 600,
        },
    )
    with pytest.raises(HTTPException) as ei:
        await adapter.verify_and_extract(token, "security")
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_kid_raises_401(km, auth_config):
    """header에 kid 없는 토큰."""
    adapter = _make_adapter(km, store={})
    # kid 없이 직접 발급
    token = jose_jwt.encode(
        {
            "iss": auth_config.issuer,
            "sub": "user-001",
            "client_id": "client-security",
            "roles": ["USER"],
            "exp": _now() + 600,
        },
        km.private_pem,
        algorithm="RS256",
    )
    with pytest.raises(HTTPException) as ei:
        await adapter.verify_and_extract(token, "security")
    assert ei.value.status_code == 401
    assert ei.value.detail["error"] == "missing_kid"


# ---------------------------------------------------------------------------
# Service account 시나리오
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_account_indexer_can_call_any_tenant(km, auth_config):
    """tenant_id='platform' service account는 모든 tenant 호출 허용."""
    adapter = _make_adapter(km, store=None)  # DB 호출 없어야 함
    token = _issue_token(
        km,
        {
            "iss": auth_config.issuer,
            "sub": "service-domainrag-indexer",
            "client_id": "service-domainrag-indexer",
            "roles": ["SERVICE", "INDEXER"],
            "exp": _now() + 600,
        },
    )
    ctx = await adapter.verify_and_extract(token, "security")
    assert ctx.tenant_id == "security"
    assert ctx.is_service_account is True
    assert ctx.clearance == "secret"  # service_accounts.indexer.clearance
    assert "INDEXER" in ctx.roles


# ---------------------------------------------------------------------------
# Platform admin 시나리오
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_admin_role_recognized(km, auth_config):
    store = {
        ("user-pa", "security"): _MembershipRow(
            clearance="secret", department="ops", domain_groups=[]
        )
    }
    adapter = _make_adapter(km, store=store)
    token = _issue_token(
        km,
        {
            "iss": auth_config.issuer,
            "sub": "user-pa",
            "client_id": "client-security",
            "roles": ["USER", "PLATFORM_ADMIN"],
            "exp": _now() + 600,
        },
    )
    ctx = await adapter.verify_and_extract(token, "security")
    assert ctx.is_platform_admin is True
    assert ctx.is_admin is True


# ---------------------------------------------------------------------------
# Mock adapter 시나리오 (test fixture로 격리됨 — ADR-018 §9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_adapter_returns_default_user():
    adapter = MockAuthAdapter()
    ctx = await adapter.verify_and_extract("", "security")
    assert ctx.tenant_id == "security"
    assert "ADMIN" in ctx.roles
    assert ctx.clearance == "confidential"


@pytest.mark.asyncio
async def test_mock_adapter_uses_path_tenant():
    """mock은 default tenant 외에도 path tenant를 그대로 인정."""
    adapter = MockAuthAdapter()
    ctx = await adapter.verify_and_extract("", "legal")
    assert ctx.tenant_id == "legal"


def test_mock_adapter_isolated_from_production_code():
    """ADR-018 §9 — MockAuthAdapter는 운영 코드(app/)에 import 흔적 없어야 한다."""
    import app.core.auth_adapter as mod

    assert not hasattr(mod, "MockAuthAdapter")
    # `app.core.auth_config`에서도 MockUserConfig 제거 확인
    import app.core.auth_config as cfg_mod
    assert not hasattr(cfg_mod, "MockUserConfig")


# ---------------------------------------------------------------------------
# ADR-020 §8 — Ledger auth_failure / tenant_mismatch publish 검증
# ---------------------------------------------------------------------------


from app.services.ledger_audit_service import LedgerAuditService  # noqa: E402
from app.services.ledger_client import NoopLedgerClient  # noqa: E402


def _ledger():
    client = NoopLedgerClient()
    return LedgerAuditService(client=client, enable=True), client


async def test_missing_bearer_token_publishes_auth_failure(km):
    ledger, noop = _ledger()
    adapter = _make_adapter(km, ledger_audit=ledger)
    with pytest.raises(Exception):  # noqa: B017
        await adapter.verify_and_extract("", "security")
    events = [e for e in noop.published if e.event_type == "auth_failure"]
    assert len(events) == 1
    assert events[0].reason == "missing_bearer_token"
    assert events[0].tenant_id == "security"


async def test_missing_kid_publishes_auth_failure(km, auth_config):
    """kid 없는 JWT — verify_and_extract가 auth_failure publish 후 401."""
    ledger, noop = _ledger()
    adapter = _make_adapter(km, ledger_audit=ledger)
    # jose는 별도 headers 지정 안 하면 alg만 들어가고 kid 누락
    token = jose_jwt.encode(
        {"iss": auth_config.issuer, "sub": "u1", "exp": _now() + 600},
        km.private_pem,
        algorithm="RS256",
    )
    with pytest.raises(Exception):  # noqa: B017
        await adapter.verify_and_extract(token, "security")
    assert any(
        e.event_type == "auth_failure" and e.reason == "missing_kid"
        for e in noop.published
    )


async def test_invalid_token_publishes_auth_failure(km, auth_config):
    """잘못된 issuer로 발급된 토큰 — JWTError → auth_failure publish."""
    ledger, noop = _ledger()
    adapter = _make_adapter(km, ledger_audit=ledger)
    token = _issue_token(
        km,
        {
            "iss": "https://wrong-issuer.example",
            "sub": "u1",
            "client_id": "client-security",
            "exp": _now() + 600,
        },
    )
    with pytest.raises(Exception):  # noqa: B017
        await adapter.verify_and_extract(token, "security")
    events = [
        e for e in noop.published
        if e.event_type == "auth_failure" and e.reason == "invalid_token"
    ]
    assert len(events) == 1


async def test_no_tenant_membership_publishes_auth_failure(km, auth_config):
    ledger, noop = _ledger()
    # membership store 비어있음 → no_tenant_membership 403
    adapter = _make_adapter(km, store={}, ledger_audit=ledger)
    token = _issue_token(
        km,
        {
            "iss": auth_config.issuer,
            "sub": "user-without-membership",
            "client_id": "client-security",
            "roles": ["USER"],
            "exp": _now() + 600,
        },
    )
    with pytest.raises(Exception):  # noqa: B017
        await adapter.verify_and_extract(token, "security")
    events = [
        e for e in noop.published
        if e.event_type == "auth_failure" and e.reason == "no_tenant_membership"
    ]
    assert len(events) == 1
    assert events[0].actor == "user-without-membership"


async def test_tenant_mismatch_in_adapter_publishes_tenant_mismatch(km, auth_config):
    """client_id가 legal인 토큰으로 security path 호출 — tenant_mismatch publish."""
    ledger, noop = _ledger()
    adapter = _make_adapter(km, ledger_audit=ledger)
    token = _issue_token(
        km,
        {
            "iss": auth_config.issuer,
            "sub": "u1",
            "client_id": "client-legal",  # legal tenant
            "roles": ["USER"],
            "exp": _now() + 600,
        },
    )
    with pytest.raises(Exception):  # noqa: B017
        await adapter.verify_and_extract(token, "security")
    events = [e for e in noop.published if e.event_type == "tenant_mismatch"]
    assert len(events) == 1
    e = events[0]
    assert e.tenant_id == "security"
    assert e.details["token_tenant"] == "legal"
    assert e.details["expected_tenant"] == "security"
