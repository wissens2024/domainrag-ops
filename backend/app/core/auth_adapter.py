"""
AuthAdapter — JWT 검증 + UserContext 빌드 (ADR-008 §7, ADR-018).

본 모듈은 ADR-002 Protocol/Adapter 패턴. 운영 구현체 1종:
  - AuthFusionAdapter (ADR-018): JWKS RS256 + user_tenant_membership 보강

MockAuthAdapter는 ADR-018 §9에 따라 `backend/tests/_fixtures/mock_auth.py`로 격리됨
(2026-05-18 옵션 C). 운영 코드는 mock을 import하지 않는다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_config import AuthConfig, AuthConfigLoader
from app.core.config import Settings, get_settings
from app.core.jwks_client import JWKSClient, JWKSFetchError


@dataclass(frozen=True)
class UserContext:
    """ADR-018 §5 — JWT 검증 후 빌드되는 사용자 컨텍스트."""

    user_id: str  # AuthFusion sub
    tenant_id: str  # DomainRAG tenant (client_id 매핑)
    roles: list[str] = field(default_factory=list)
    clearance: str = "internal"  # ADR-004
    department: str | None = None
    domain_groups: list[str] = field(default_factory=list)
    preferred_username: str | None = None
    email: str | None = None
    raw_claims: dict = field(default_factory=dict)

    @property
    def is_platform_admin(self) -> bool:
        return "PLATFORM_ADMIN" in self.roles

    @property
    def is_admin(self) -> bool:
        return "ADMIN" in self.roles or self.is_platform_admin

    @property
    def is_service_account(self) -> bool:
        return "SERVICE" in self.roles


class AuthAdapter(Protocol):
    """ADR-008 §7 Protocol stub의 구체 인터페이스."""

    async def verify_and_extract(
        self, bearer_token: str, expected_tenant_id: str
    ) -> UserContext: ...


# ---------------------------------------------------------------------------
# AuthFusion (운영) — 유일한 운영 AuthAdapter 구현
# ---------------------------------------------------------------------------


# adapter 생성 시 주입되는 session factory.
# 호출하면 `async with` 가능한 객체를 반환 — yield 시 AsyncSession.
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class AuthFusionAdapter:
    """ADR-018 §3 — 운영 구현. JWKS RS256 + user_tenant_membership 보강."""

    def __init__(
        self,
        settings: Settings,
        auth_config: AuthConfig | None = None,
        jwks_client: JWKSClient | None = None,
        session_factory: SessionFactory | None = None,
        clock: Callable[[], float] = time.time,
        ledger_audit=None,
    ):
        self.settings = settings
        self.auth_config = auth_config or AuthConfigLoader.load(settings)
        self.jwks_client = jwks_client or JWKSClient(
            jwks_uri=self.auth_config.jwks_uri,
            cache_ttl_seconds=self.auth_config.jwks_cache_ttl_seconds,
        )
        self.session_factory = session_factory
        self.clock = clock
        # ADR-020 §8 — auth_failure / tenant_mismatch publish. 실패는 swallow.
        self._ledger = ledger_audit

    async def _publish_auth_failure(
        self,
        *,
        expected_tenant_id: str,
        error: str,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._ledger is None:
            return
        try:
            await self._ledger.publish_auth_failure(
                tenant_id=expected_tenant_id,
                actor=actor,
                reason=error,
                details=dict(details or {}),
            )
        except Exception:  # noqa: BLE001 — swallow
            pass

    async def _publish_tenant_mismatch(
        self,
        *,
        expected_tenant_id: str,
        token_tenant: str | None,
        actor: str | None,
    ) -> None:
        if self._ledger is None:
            return
        try:
            await self._ledger.publish_tenant_mismatch(
                tenant_id=expected_tenant_id,
                actor=actor,
                expected_tenant=expected_tenant_id,
                token_tenant=token_tenant,
            )
        except Exception:  # noqa: BLE001
            pass

    async def verify_and_extract(
        self, bearer_token: str, expected_tenant_id: str
    ) -> UserContext:
        token = (bearer_token or "").strip()
        if not token:
            await self._publish_auth_failure(
                expected_tenant_id=expected_tenant_id, error="missing_bearer_token"
            )
            raise HTTPException(status_code=401, detail={"error": "missing_bearer_token"})

        # 1) header에서 kid 추출
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            await self._publish_auth_failure(
                expected_tenant_id=expected_tenant_id,
                error="invalid_token_header",
                details={"reason": str(exc)},
            )
            raise HTTPException(
                status_code=401, detail={"error": "invalid_token_header", "reason": str(exc)}
            ) from exc
        kid = header.get("kid")
        if not kid:
            await self._publish_auth_failure(
                expected_tenant_id=expected_tenant_id, error="missing_kid"
            )
            raise HTTPException(status_code=401, detail={"error": "missing_kid"})

        # 2) JWKS에서 key 조회 (jwks_unavailable은 infra 문제 — auth_failure 아님, publish skip)
        try:
            jwk = await self.jwks_client.get_key(kid)
        except JWKSFetchError as exc:
            raise HTTPException(
                status_code=503, detail={"error": "jwks_unavailable", "reason": str(exc)}
            ) from exc

        # 3) RS256 서명 검증 + iss/exp 검증
        try:
            claims = jwt.decode(
                token,
                jwk,
                algorithms=[self.auth_config.jwt_algorithm],
                issuer=self.auth_config.issuer,
                options={
                    "verify_aud": False,  # AuthFusion이 audience 미발급일 수 있음
                    "verify_iss": True,
                    "verify_exp": True,
                    "verify_signature": True,
                },
            )
        except JWTError as exc:
            await self._publish_auth_failure(
                expected_tenant_id=expected_tenant_id,
                error="invalid_token",
                details={"reason": str(exc)},
            )
            raise HTTPException(
                status_code=401, detail={"error": "invalid_token", "reason": str(exc)}
            ) from exc

        client_id = claims.get("client_id") or claims.get("azp")
        if not client_id:
            await self._publish_auth_failure(
                expected_tenant_id=expected_tenant_id,
                error="missing_client_id_claim",
                actor=claims.get("sub"),
            )
            raise HTTPException(
                status_code=401, detail={"error": "missing_client_id_claim"}
            )

        raw_roles = list(claims.get("roles") or [])
        # AuthFusion RP_ONBOARDING ADR-0019 — service-scoped role prefix-only 매칭.
        # `<rp-slug>-admin/auditor/user`만 ADMIN/AUDITOR/USER로 매핑, 전역 ADMIN 제외.
        # PLATFORM_ADMIN은 platform_admin_role과 일치 시 통과.
        roles: list[str] = []
        for r in raw_roles:
            mapped = self.auth_config.map_oidc_role(r)
            if mapped is not None and mapped not in roles:
                roles.append(mapped)
        # service account 분기를 위해 raw_roles의 SERVICE도 보존
        if "SERVICE" in raw_roles and "SERVICE" not in roles:
            roles.append("SERVICE")

        # 4) Service account vs 일반 user
        sa = self.auth_config.service_accounts.get(client_id)
        if sa is not None or "SERVICE" in roles:
            # service account: tenant_id 매핑 + clearance/role 확정
            if sa is not None:
                token_tenant_id = sa.tenant_id
                merged_roles = list(dict.fromkeys([*roles, *sa.roles]))
                clearance = sa.clearance
            else:
                # SERVICE role인데 service_accounts 매핑 미등록 — 보수적으로 fallback
                token_tenant_id = self.auth_config.client_id_to_tenant_id(client_id)
                merged_roles = roles
                clearance = "internal"

            # service account의 tenant_id="platform"은 모든 tenant 호출 허용
            if token_tenant_id != "platform" and token_tenant_id != expected_tenant_id:
                await self._publish_tenant_mismatch(
                    expected_tenant_id=expected_tenant_id,
                    token_tenant=token_tenant_id,
                    actor=claims.get("sub", client_id),
                )
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "tenant_mismatch",
                        "token_tenant_id": token_tenant_id,
                        "expected_tenant_id": expected_tenant_id,
                    },
                )

            return UserContext(
                user_id=claims.get("sub", client_id),
                tenant_id=expected_tenant_id,
                roles=merged_roles,
                clearance=clearance,
                department=None,
                domain_groups=[],
                preferred_username=claims.get("preferred_username"),
                email=claims.get("email"),
                raw_claims=claims,
            )

        # 일반 user: client_id → tenant_id 매핑
        token_tenant_id = self.auth_config.client_id_to_tenant_id(client_id)
        if token_tenant_id != expected_tenant_id:
            await self._publish_tenant_mismatch(
                expected_tenant_id=expected_tenant_id,
                token_tenant=token_tenant_id,
                actor=claims.get("sub", client_id),
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "tenant_mismatch",
                    "token_tenant_id": token_tenant_id,
                    "expected_tenant_id": expected_tenant_id,
                },
            )

        # 5) email_verified 검증 (AuthFusion OIDC_REQUIRE_VERIFIED_EMAIL 표준).
        # claim에 email_verified가 없으면 통과 — IdP가 email scope 미발급한 client만.
        if (
            self.auth_config.require_verified_email
            and "email_verified" in claims
            and claims.get("email_verified") is not True
        ):
            await self._publish_auth_failure(
                expected_tenant_id=expected_tenant_id,
                error="email_not_verified",
                actor=claims.get("sub"),
            )
            raise HTTPException(
                status_code=403,
                detail={"error": "email_not_verified"},
            )

        # 6) user_tenant_membership 조회로 clearance/department/domain_groups 보강
        sub = claims.get("sub")
        if not sub:
            await self._publish_auth_failure(
                expected_tenant_id=expected_tenant_id, error="missing_sub_claim",
            )
            raise HTTPException(status_code=401, detail={"error": "missing_sub_claim"})

        membership = await self._load_membership(sub, expected_tenant_id)
        if membership is None:
            await self._publish_auth_failure(
                expected_tenant_id=expected_tenant_id,
                error="no_tenant_membership",
                actor=sub,
            )
            # 멤버십 미등록 = 해당 tenant 접근 권한 없음
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "no_tenant_membership",
                    "tenant_id": expected_tenant_id,
                },
            )

        # tenant 별 추가 admin role
        extra_admin_roles = self.auth_config.extra_admin_roles(expected_tenant_id)
        if extra_admin_roles and "ADMIN" not in roles:
            for r in extra_admin_roles:
                if r in roles:
                    roles = [*roles, "ADMIN"]
                    break

        return UserContext(
            user_id=sub,
            tenant_id=expected_tenant_id,
            roles=roles,
            clearance=membership["clearance"],
            department=membership.get("department"),
            domain_groups=list(membership.get("domain_groups") or []),
            preferred_username=claims.get("preferred_username"),
            email=claims.get("email"),
            raw_claims=claims,
        )

    async def _load_membership(
        self, user_id: str, tenant_id: str
    ) -> dict[str, Any] | None:
        if self.session_factory is None:
            # session 미주입 — DB 조회 불가. 운영에서는 사용 불가.
            raise HTTPException(
                status_code=500,
                detail={"error": "auth_db_session_unavailable"},
            )
        from app.models.user_tenant_membership import UserTenantMembership

        async with self.session_factory() as session:
            stmt = (
                select(UserTenantMembership)
                .where(
                    UserTenantMembership.user_id == user_id,
                    UserTenantMembership.tenant_id == tenant_id,
                    UserTenantMembership.is_active.is_(True),
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return {
                "clearance": row.clearance,
                "department": row.department,
                "domain_groups": row.domain_groups or [],
            }


# ---------------------------------------------------------------------------
# FastAPI dependency wiring
# ---------------------------------------------------------------------------


_authfusion_singleton: AuthFusionAdapter | None = None


def _build_authfusion_adapter(settings: Settings) -> AuthFusionAdapter:
    """admin DB session(BYPASSRLS — user_tenant_membership은 RLS 미적용)으로 wire.

    ADR-020 §8 — auth_failure / tenant_mismatch publish를 위해 LedgerAuditService도 주입.
    """
    from app.core.db import AdminSessionLocal
    from app.deps import get_ledger_audit_service

    def _session_cm() -> AbstractAsyncContextManager[AsyncSession]:
        return AdminSessionLocal()

    return AuthFusionAdapter(
        settings=settings,
        session_factory=_session_cm,
        ledger_audit=get_ledger_audit_service(settings),
    )


def get_auth_adapter(settings: Settings = Depends(get_settings)) -> AuthAdapter:
    """FastAPI dependency — 운영은 AuthFusionAdapter 단일 구현.

    테스트는 `app.dependency_overrides[get_auth_adapter] = lambda: MockAuthAdapter(...)`로
    가로채야 한다 (ADR-018 §9, conftest.py 참고).
    """
    global _authfusion_singleton
    if _authfusion_singleton is None:
        _authfusion_singleton = _build_authfusion_adapter(settings)
    return _authfusion_singleton


def reset_auth_adapter() -> None:
    """테스트용 — singleton + AuthConfig cache 둘 다 reset."""
    global _authfusion_singleton
    _authfusion_singleton = None
    AuthConfigLoader.reset()


async def get_user_context(
    request: Request,
    tenant_id: str,
    adapter: AuthAdapter = Depends(get_auth_adapter),
) -> UserContext:
    """모든 `/api/{tenant_id}/...` endpoint의 dependency.

    Token source 우선순위 (ADR-018 §6):
      1. Authorization: Bearer ... 헤더 (service-to-service / 명시적 SDK)
      2. httpOnly cookie `domainrag_access` (브라우저 + frontend SPA)

    둘 다 없으면 401. 검증된 token의 tenant_id가 URL path tenant_id와
    mismatch면 403 (ADR-008 격리 3중 방어).
    """
    token = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        token = (request.cookies.get("domainrag_access") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail={"error": "missing_bearer_token"})

    return await adapter.verify_and_extract(token, tenant_id)
