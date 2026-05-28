"""
AuthAdapter — JWT 검증 + UserContext 빌드 (ADR-008 §7, ADR-018).

본 모듈은 ADR-002 Protocol/Adapter 패턴. 운영 구현체 1종:
  - AuthFusionAdapter (ADR-018): JWKS RS256 + user_tenant_membership 보강

MockAuthAdapter는 ADR-018 §9에 따라 `backend/tests/_fixtures/mock_auth.py`로 격리됨
(2026-05-18 옵션 C). 운영 코드는 mock을 import하지 않는다.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserContext:
    """ADR-018 §5 — JWT 검증 후 빌드되는 사용자 컨텍스트."""

    user_id: str  # AuthFusion sub
    domain_id: str  # DomainRAG tenant (client_id 매핑)
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
    def is_auditor(self) -> bool:
        """ADR-022 §3 — 전역 read-only 감사 역할 (모든 도메인 조회, 쓰기 불가)."""
        return "AUDITOR" in self.roles

    @property
    def is_service_account(self) -> bool:
        return "SERVICE" in self.roles


class AuthAdapter(Protocol):
    """ADR-008 §7 Protocol stub의 구체 인터페이스."""

    async def verify_and_extract(
        self, bearer_token: str, expected_domain_id: str
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
        expected_domain_id: str,
        error: str,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._ledger is None:
            return
        try:
            await self._ledger.publish_auth_failure(
                domain_id=expected_domain_id,
                actor=actor,
                reason=error,
                details=dict(details or {}),
            )
        except Exception:  # noqa: BLE001 — swallow
            pass

    async def _publish_tenant_mismatch(
        self,
        *,
        expected_domain_id: str,
        token_tenant: str | None,
        actor: str | None,
    ) -> None:
        if self._ledger is None:
            return
        try:
            await self._ledger.publish_tenant_mismatch(
                domain_id=expected_domain_id,
                actor=actor,
                expected_tenant=expected_domain_id,
                token_tenant=token_tenant,
            )
        except Exception:  # noqa: BLE001
            pass

    async def verify_and_extract(
        self, bearer_token: str, expected_domain_id: str
    ) -> UserContext:
        token = (bearer_token or "").strip()
        if not token:
            await self._publish_auth_failure(
                expected_domain_id=expected_domain_id, error="missing_bearer_token"
            )
            raise HTTPException(status_code=401, detail={"error": "missing_bearer_token"})

        # 1) header에서 kid 추출
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            await self._publish_auth_failure(
                expected_domain_id=expected_domain_id,
                error="invalid_token_header",
                details={"reason": str(exc)},
            )
            raise HTTPException(
                status_code=401, detail={"error": "invalid_token_header", "reason": str(exc)}
            ) from exc
        kid = header.get("kid")
        if not kid:
            await self._publish_auth_failure(
                expected_domain_id=expected_domain_id, error="missing_kid"
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
                expected_domain_id=expected_domain_id,
                error="invalid_token",
                details={"reason": str(exc)},
            )
            raise HTTPException(
                status_code=401, detail={"error": "invalid_token", "reason": str(exc)}
            ) from exc

        client_id = claims.get("client_id") or claims.get("azp")
        if not client_id:
            await self._publish_auth_failure(
                expected_domain_id=expected_domain_id,
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
            # service account: domain_id 매핑 + clearance/role 확정
            if sa is not None:
                token_domain_id = sa.domain_id
                merged_roles = list(dict.fromkeys([*roles, *sa.roles]))
                clearance = sa.clearance
            else:
                # SERVICE role인데 service_accounts 매핑 미등록 — 보수적으로 fallback
                token_domain_id = self.auth_config.client_id_to_domain_id(
                    client_id, expected_domain_id
                )
                merged_roles = roles
                clearance = "internal"

            # service account의 domain_id="platform"은 모든 tenant 호출 허용
            if token_domain_id != "platform" and token_domain_id != expected_domain_id:
                await self._publish_tenant_mismatch(
                    expected_domain_id=expected_domain_id,
                    token_tenant=token_domain_id,
                    actor=claims.get("sub", client_id),
                )
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "tenant_mismatch",
                        "token_domain_id": token_domain_id,
                        "expected_domain_id": expected_domain_id,
                    },
                )

            return UserContext(
                user_id=claims.get("sub", client_id),
                domain_id=expected_domain_id,
                roles=merged_roles,
                clearance=clearance,
                department=None,
                domain_groups=[],
                preferred_username=claims.get("preferred_username"),
                email=claims.get("email"),
                raw_claims=claims,
            )

        # 일반 user: client_id → domain_id 매핑 (single-client multi-tenant 시
        # expected_domain_id가 map[client_id] list에 있으면 expected를 신뢰).
        # expected_domain_id == "__platform__" sentinel은 platform admin 경로 —
        # tenant mismatch 검증 skip (role 검증은 require_platform_admin이 담당).
        token_domain_id = self.auth_config.client_id_to_domain_id(
            client_id, expected_domain_id
        )
        if expected_domain_id == "__platform__":
            return UserContext(
                user_id=claims.get("sub", client_id),
                domain_id=token_domain_id,
                roles=roles,
                clearance=claims.get("clearance", "internal"),
                department=claims.get("department"),
                domain_groups=list(claims.get("domain_groups") or []),
                preferred_username=claims.get("preferred_username"),
                email=claims.get("email"),
                raw_claims=claims,
            )
        if token_domain_id != expected_domain_id:
            logger.warning(
                "verify_and_extract tenant_mismatch — client_id=%s expected=%s "
                "token_tenant=%s map=%s",
                client_id,
                expected_domain_id,
                token_domain_id,
                self.auth_config.client_tenant_map.get(client_id),
            )
            await self._publish_tenant_mismatch(
                expected_domain_id=expected_domain_id,
                token_tenant=token_domain_id,
                actor=claims.get("sub", client_id),
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "tenant_mismatch",
                    "token_domain_id": token_domain_id,
                    "expected_domain_id": expected_domain_id,
                },
            )

        # 5) email_verified 검증 (AuthFusion OIDC_REQUIRE_VERIFIED_EMAIL 표준).
        # 운영 AuthFusion이 email_verified claim을 현재 발급하지 않으므로 claim
        # 부재는 통과시킨다. claim이 있으면 True여야 한다. AuthFusion이 발급
        # 시작하면 require_verified_email를 strict로 다시 켤 수 있다.
        if (
            self.auth_config.require_verified_email
            and "email_verified" in claims
            and claims.get("email_verified") is not True
        ):
            await self._publish_auth_failure(
                expected_domain_id=expected_domain_id,
                error="email_not_verified",
                actor=claims.get("sub"),
            )
            raise HTTPException(
                status_code=403,
                detail={"error": "email_not_verified"},
            )

        # 6) user_tenant_membership 조회로 clearance/department/domain_groups 보강.
        #    전역 역할(admin/auditor/platform_admin)은 membership 없이 통과 (ADR-022 §3).
        sub = claims.get("sub")
        if not sub:
            await self._publish_auth_failure(
                expected_domain_id=expected_domain_id, error="missing_sub_claim",
            )
            raise HTTPException(status_code=401, detail={"error": "missing_sub_claim"})

        membership = await self._load_membership(sub, expected_domain_id)
        if membership is None:
            # 전역 역할은 모든 도메인 접근 (ADR-022 §3): admin/platform_admin=관리,
            # auditor=read-only. 기본 clearance=secret(전체 문서 가시). membership row가
            # 존재하면 그 값을 우선한다(더 구체적).
            is_global = bool({"ADMIN", "AUDITOR", "PLATFORM_ADMIN"} & set(roles))
            if is_global:
                membership = {
                    "clearance": "secret",
                    "department": None,
                    "domain_groups": [],
                }
            else:
                # 일반 user. open 도메인이면 JIT membership 생성(ADR-022 §4), 아니면 403.
                enrollment = await self._load_tenant_enrollment(expected_domain_id)
                if enrollment == "open":
                    membership = await self._create_jit_membership(
                        sub, expected_domain_id
                    )
                else:
                    await self._publish_auth_failure(
                        expected_domain_id=expected_domain_id,
                        error="no_tenant_membership",
                        actor=sub,
                    )
                    # assigned 도메인 + membership 미등록 = 접근 권한 없음
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "error": "no_tenant_membership",
                            "domain_id": expected_domain_id,
                        },
                    )

        # tenant 별 추가 admin role
        extra_admin_roles = self.auth_config.extra_admin_roles(expected_domain_id)
        if extra_admin_roles and "ADMIN" not in roles:
            for r in extra_admin_roles:
                if r in roles:
                    roles = [*roles, "ADMIN"]
                    break

        return UserContext(
            user_id=sub,
            domain_id=expected_domain_id,
            roles=roles,
            clearance=membership["clearance"],
            department=membership.get("department"),
            domain_groups=list(membership.get("domain_groups") or []),
            preferred_username=claims.get("preferred_username"),
            email=claims.get("email"),
            raw_claims=claims,
        )

    async def _load_membership(
        self, user_id: str, domain_id: str
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
                    UserTenantMembership.domain_id == domain_id,
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

    async def _load_tenant_enrollment(self, domain_id: str) -> str | None:
        """ADR-022 §4 — 도메인의 enrollment_policy 조회 (open|assigned). 없으면 None."""
        if self.session_factory is None:
            return None
        from sqlalchemy import text

        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT enrollment_policy FROM tenants "
                        "WHERE domain_id = :tid AND status = 'active'"
                    ),
                    {"tid": domain_id},
                )
            ).first()
        return row[0] if row else None

    async def _create_jit_membership(
        self, user_id: str, domain_id: str
    ) -> dict[str, Any]:
        """ADR-022 §4 — open 도메인 첫 진입 시 기본 membership(USER/internal) 자동 생성.

        모든 접근이 단일 membership 경로로 흐르게 해 감사·revoke 일관성을 유지한다.
        persist 실패는 best-effort로 swallow하고 이번 요청은 기본값으로 통과시킨다.
        """
        default = {
            "clearance": "internal",
            "department": None,
            "domain_groups": [],
        }
        if self.session_factory is None:
            return default
        from sqlalchemy import text

        try:
            async with self.session_factory() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO user_tenant_membership
                            (user_id, domain_id, clearance, domain_groups, is_active)
                        VALUES (:uid, :tid, 'internal', '[]'::jsonb, TRUE)
                        ON CONFLICT (user_id, domain_id) DO UPDATE SET is_active = TRUE
                        """
                    ),
                    {"uid": user_id, "tid": domain_id},
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — persist 실패해도 open 도메인 접근은 허용
            logger.warning(
                "JIT membership persist failed (user=%s tenant=%s): %s",
                user_id, domain_id, exc,
            )
        return default


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


async def get_user_context_no_tenant(
    request: Request,
    adapter: AuthAdapter = Depends(get_auth_adapter),
) -> UserContext:
    """platform 경로 (`/api/platform/admin/*`)용 — tenant path param 없음.

    token 검증은 동일 (Authorization or domainrag_access cookie). tenant mismatch
    검증은 skip. role 검증은 require_platform_admin가 담당.
    """
    token = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        token = (request.cookies.get("domainrag_access") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail={"error": "missing_bearer_token"})
    return await adapter.verify_and_extract(token, "__platform__")


async def get_user_context(
    request: Request,
    domain_id: str,
    adapter: AuthAdapter = Depends(get_auth_adapter),
) -> UserContext:
    """모든 `/api/{domain_id}/...` endpoint의 dependency.

    Token source 우선순위 (ADR-018 §6):
      1. Authorization: Bearer ... 헤더 (service-to-service / 명시적 SDK)
      2. httpOnly cookie `domainrag_access` (브라우저 + frontend SPA)

    둘 다 없으면 401. 검증된 token의 domain_id가 URL path domain_id와
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

    return await adapter.verify_and_extract(token, domain_id)
