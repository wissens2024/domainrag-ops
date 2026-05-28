"""MembershipService — user_tenant_membership CRUD (ADR-018 §4, ADR-022 §3·§4).

도메인 인가의 단일 저장소. RLS 미적용 테이블(cross-tenant 매핑)이므로 운영 구현은
admin engine(BYPASSRLS)을 사용한다.

용도:
  - list_for_user: 로그인 사용자가 접근 가능한 도메인(=내 membership) — /api/auth/me/domains
  - list_for_tenant: 한 도메인에 배정된 사용자 목록 — admin 도메인 관리 화면
  - assign / revoke: 특수(assigned) 도메인에 사용자 배정·해제 — admin
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


def _row_to_dict(
    *,
    user_id: str,
    domain_id: str,
    clearance: str,
    department: str | None,
    domain_groups: list[str] | None,
    is_active: bool,
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "domain_id": domain_id,
        "clearance": clearance,
        "department": department,
        "domain_groups": list(domain_groups or []),
        "is_active": is_active,
    }


class MembershipService(Protocol):
    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]: ...
    async def list_for_tenant(self, domain_id: str) -> list[dict[str, Any]]: ...
    async def assign(
        self,
        *,
        user_id: str,
        domain_id: str,
        clearance: str = "internal",
        department: str | None = None,
        domain_groups: list[str] | None = None,
    ) -> dict[str, Any]: ...
    async def revoke(self, *, user_id: str, domain_id: str) -> bool: ...


class PostgresMembershipService:
    """운영 구현 — admin session(BYPASSRLS)."""

    def __init__(self, *, admin_session_factory) -> None:
        self._sf = admin_session_factory

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import text

        async with self._sf() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT domain_id, clearance, department, domain_groups
                          FROM user_tenant_membership
                         WHERE user_id = :uid AND is_active = TRUE
                         ORDER BY domain_id
                        """
                    ),
                    {"uid": user_id},
                )
            ).all()
        return [
            _row_to_dict(
                user_id=user_id, domain_id=r[0], clearance=r[1],
                department=r[2], domain_groups=r[3], is_active=True,
            )
            for r in rows
        ]

    async def list_for_tenant(self, domain_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import text

        async with self._sf() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT user_id, clearance, department, domain_groups, is_active
                          FROM user_tenant_membership
                         WHERE domain_id = :tid
                         ORDER BY user_id
                        """
                    ),
                    {"tid": domain_id},
                )
            ).all()
        return [
            _row_to_dict(
                user_id=r[0], domain_id=domain_id, clearance=r[1],
                department=r[2], domain_groups=r[3], is_active=bool(r[4]),
            )
            for r in rows
        ]

    async def assign(
        self,
        *,
        user_id: str,
        domain_id: str,
        clearance: str = "internal",
        department: str | None = None,
        domain_groups: list[str] | None = None,
    ) -> dict[str, Any]:
        from sqlalchemy import text
        import json as _json

        async with self._sf() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO user_tenant_membership
                        (user_id, domain_id, clearance, department, domain_groups, is_active)
                    VALUES (:uid, :tid, :clr, :dept, CAST(:groups AS JSONB), TRUE)
                    ON CONFLICT (user_id, domain_id) DO UPDATE SET
                        clearance = EXCLUDED.clearance,
                        department = EXCLUDED.department,
                        domain_groups = EXCLUDED.domain_groups,
                        is_active = TRUE,
                        updated_at = NOW()
                    """
                ),
                {
                    "uid": user_id,
                    "tid": domain_id,
                    "clr": clearance,
                    "dept": department,
                    "groups": _json.dumps(list(domain_groups or [])),
                },
            )
            await session.commit()
        return _row_to_dict(
            user_id=user_id, domain_id=domain_id, clearance=clearance,
            department=department, domain_groups=domain_groups, is_active=True,
        )

    async def revoke(self, *, user_id: str, domain_id: str) -> bool:
        from sqlalchemy import text

        async with self._sf() as session:
            res = await session.execute(
                text(
                    """
                    UPDATE user_tenant_membership
                       SET is_active = FALSE, updated_at = NOW()
                     WHERE user_id = :uid AND domain_id = :tid AND is_active = TRUE
                    """
                ),
                {"uid": user_id, "tid": domain_id},
            )
            await session.commit()
        return (res.rowcount or 0) > 0


class InMemoryMembershipService:
    """테스트/inmemory backend — 프로세스 내 dict."""

    def __init__(self) -> None:
        # (user_id, domain_id) -> dict
        self._store: dict[tuple[str, str], dict[str, Any]] = {}

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return [
            dict(v)
            for (u, _t), v in sorted(self._store.items())
            if u == user_id and v["is_active"]
        ]

    async def list_for_tenant(self, domain_id: str) -> list[dict[str, Any]]:
        return [
            dict(v)
            for (_u, t), v in sorted(self._store.items())
            if t == domain_id
        ]

    async def assign(
        self,
        *,
        user_id: str,
        domain_id: str,
        clearance: str = "internal",
        department: str | None = None,
        domain_groups: list[str] | None = None,
    ) -> dict[str, Any]:
        rec = _row_to_dict(
            user_id=user_id, domain_id=domain_id, clearance=clearance,
            department=department, domain_groups=domain_groups, is_active=True,
        )
        self._store[(user_id, domain_id)] = rec
        return dict(rec)

    async def revoke(self, *, user_id: str, domain_id: str) -> bool:
        rec = self._store.get((user_id, domain_id))
        if rec is None or not rec["is_active"]:
            return False
        rec["is_active"] = False
        return True
