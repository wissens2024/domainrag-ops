"""PiiStorageApprovalService — ADR-020 §4 platform_admin 승인 기반 plain 보관 정책.

활성 승인(status='active')이 있어야 PIIService가 configured plain 정책을 실제로 적용한다.
부분 유니크 인덱스(uq_pii_storage_approvals_active)가 race를 차단하므로 본 서비스는
승인 시 status='active' INSERT를 시도하고 충돌이면 PiiApprovalConflictError raise.

audit: approve/revoke마다 tenant_lifecycle_logs에 platform_admin_action 이벤트를 기록한다.

본 서비스는 admin engine(BYPASSRLS)을 사용한다 — pii_storage_approvals는 platform_admin이
cross-tenant로 관리하는 메타이며 RLS 미적용 (ADR-019 §2 RLS 미적용 테이블 분류).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


async def _publish_ledger(
    ledger,
    *,
    tenant_id: str,
    actor: str,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """ADR-020 §8 — platform_admin_action publish. ledger=None이거나 실패면 swallow."""
    if ledger is None:
        return
    try:
        await ledger.publish_platform_admin_action(
            tenant_id=tenant_id, actor=actor, action=action, details=details,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ledger publish (%s) failed for tenant=%s: %s (swallow)",
            action, tenant_id, exc,
        )

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker


@dataclass(frozen=True)
class PiiApprovalRecord:
    """API 응답 / 내부 lookup 결과."""

    approval_id: str
    tenant_id: str
    policy: str
    reason: str
    approved_by: str
    valid_from: datetime | None
    valid_until: datetime | None
    status: str
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revoke_reason: str | None = None


class PiiApprovalConflictError(Exception):
    """이미 active 승인이 존재 — 재승인 전에 revoke 필요."""

    def __init__(self, existing_id: str):
        super().__init__(f"active approval already exists: {existing_id}")
        self.existing_id = existing_id


class PiiApprovalNotFoundError(Exception):
    """revoke 요청 시 active 승인이 없음."""


class PiiStorageApprovalService:
    """pii_storage_approvals CRUD + tenant_lifecycle_logs audit (ADR-020 §4·§8).

    Args:
        admin_session_factory: domainrag_platform_admin role의 async_sessionmaker
            (RLS 미적용). approve/revoke/조회 모두 본 session으로 처리.
        ledger_audit: 선택. ADR-020 §8 — approve/revoke 후 publish_platform_admin_action.
    """

    def __init__(
        self,
        *,
        admin_session_factory: async_sessionmaker,
        ledger_audit=None,
    ) -> None:
        self._sf = admin_session_factory
        self._ledger = ledger_audit

    async def approve_plain(
        self,
        *,
        tenant_id: str,
        reason: str,
        approved_by: str,
        valid_until: datetime | None = None,
    ) -> PiiApprovalRecord:
        async with self._sf() as session:
            try:
                row = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO pii_storage_approvals (
                                tenant_id, policy, reason, approved_by, valid_until
                            )
                            VALUES (:tenant_id, 'plain', :reason, :approved_by, :valid_until)
                            RETURNING id, valid_from, valid_until, status
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "reason": reason,
                            "approved_by": approved_by,
                            "valid_until": valid_until,
                        },
                    )
                ).first()
            except IntegrityError as exc:
                await session.rollback()
                existing = await self._find_active_id(tenant_id)
                if existing:
                    raise PiiApprovalConflictError(existing) from exc
                raise

            approval_id = str(row[0])
            valid_from = row[1]
            valid_until_out = row[2]
            status_out = row[3]

            await self._audit(
                session,
                tenant_id=tenant_id,
                action="pii_storage_plain_approved",
                actor=approved_by,
                reason=reason,
                details={
                    "approval_id": approval_id,
                    "valid_until": valid_until_out.isoformat() if valid_until_out else None,
                },
            )
            await session.commit()

            record = PiiApprovalRecord(
                approval_id=approval_id,
                tenant_id=tenant_id,
                policy="plain",
                reason=reason,
                approved_by=approved_by,
                valid_from=valid_from,
                valid_until=valid_until_out,
                status=status_out,
            )
            await _publish_ledger(
                self._ledger,
                tenant_id=tenant_id, actor=approved_by,
                action="pii_storage_plain_approved",
                details={
                    "approval_id": approval_id,
                    "reason": reason,
                    "valid_until": valid_until_out.isoformat() if valid_until_out else None,
                },
            )
            return record

    async def revoke_active(
        self,
        *,
        tenant_id: str,
        revoked_by: str,
        revoke_reason: str,
    ) -> PiiApprovalRecord:
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE pii_storage_approvals
                           SET status = 'revoked',
                               revoked_at = NOW(),
                               revoked_by = :revoked_by,
                               revoke_reason = :revoke_reason
                         WHERE tenant_id = :tenant_id
                           AND status = 'active'
                        RETURNING id, policy, reason, approved_by, valid_from, valid_until,
                                  revoked_at
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "revoked_by": revoked_by,
                        "revoke_reason": revoke_reason,
                    },
                )
            ).first()
            if row is None:
                raise PiiApprovalNotFoundError(
                    f"no active approval for tenant {tenant_id}"
                )

            approval_id = str(row[0])
            await self._audit(
                session,
                tenant_id=tenant_id,
                action="pii_storage_plain_revoked",
                actor=revoked_by,
                reason=revoke_reason,
                details={"approval_id": approval_id},
            )
            await session.commit()

            await _publish_ledger(
                self._ledger,
                tenant_id=tenant_id, actor=revoked_by,
                action="pii_storage_plain_revoked",
                details={"approval_id": approval_id, "reason": revoke_reason},
            )
            return PiiApprovalRecord(
                approval_id=approval_id,
                tenant_id=tenant_id,
                policy=row[1],
                reason=row[2],
                approved_by=row[3],
                valid_from=row[4],
                valid_until=row[5],
                status="revoked",
                revoked_at=row[6],
                revoked_by=revoked_by,
                revoke_reason=revoke_reason,
            )

    async def is_plain_approved(self, tenant_id: str) -> bool:
        """chat 요청 시 PIIService 평가에 사용되는 hot lookup.

        active 승인 row가 존재하고 valid_until이 NULL 또는 NOW() 이후면 True.
        """
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT 1 FROM pii_storage_approvals
                         WHERE tenant_id = :tenant_id
                           AND status = 'active'
                           AND (valid_until IS NULL OR valid_until > NOW())
                         LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).first()
            return row is not None

    async def list_by_tenant(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PiiApprovalRecord]:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT id, policy, reason, approved_by, valid_from, valid_until,
                               status, revoked_at, revoked_by, revoke_reason
                          FROM pii_storage_approvals
                         WHERE tenant_id = :tenant_id
                         ORDER BY created_at DESC
                         LIMIT :limit OFFSET :offset
                        """
                    ),
                    {"tenant_id": tenant_id, "limit": limit, "offset": offset},
                )
            ).all()
            return [
                PiiApprovalRecord(
                    approval_id=str(r[0]),
                    tenant_id=tenant_id,
                    policy=r[1],
                    reason=r[2],
                    approved_by=r[3],
                    valid_from=r[4],
                    valid_until=r[5],
                    status=r[6],
                    revoked_at=r[7],
                    revoked_by=r[8],
                    revoke_reason=r[9],
                )
                for r in rows
            ]

    async def _find_active_id(self, tenant_id: str) -> str | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id FROM pii_storage_approvals
                         WHERE tenant_id = :tenant_id AND status = 'active'
                         ORDER BY created_at DESC LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).first()
            return str(row[0]) if row else None

    @staticmethod
    async def _audit(
        session,
        *,
        tenant_id: str,
        action: str,
        actor: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """tenant_lifecycle_logs에 platform_admin_action 흐름을 단일 row로 기록."""
        await session.execute(
            text(
                """
                INSERT INTO tenant_lifecycle_logs (
                    tenant_id, action, from_state, to_state, actor, reason, details
                )
                VALUES (
                    :tenant_id, :action, NULL, NULL, :actor, :reason,
                    CAST(:details AS JSONB)
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "action": action,
                "actor": actor,
                "reason": reason,
                "details": json.dumps(details or {}, ensure_ascii=False, default=str),
            },
        )


# ---------------------------------------------------------------------------
# InMemory variant — dev/tests용. DB 의존 없이 동일한 surface 제공.
# ---------------------------------------------------------------------------


class InMemoryPiiStorageApprovalService:
    """Postgres PiiStorageApprovalService의 in-process 변형 (테스트·inmemory backend)."""

    def __init__(self, *, ledger_audit=None) -> None:
        # tenant_id → PiiApprovalRecord (active 또는 가장 최근)
        self._active: dict[str, PiiApprovalRecord] = {}
        self._history: dict[str, list[PiiApprovalRecord]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self._ledger = ledger_audit

    async def approve_plain(
        self,
        *,
        tenant_id: str,
        reason: str,
        approved_by: str,
        valid_until: datetime | None = None,
    ) -> PiiApprovalRecord:
        if tenant_id in self._active:
            raise PiiApprovalConflictError(self._active[tenant_id].approval_id)
        import uuid as _uuid

        record = PiiApprovalRecord(
            approval_id=_uuid.uuid4().hex,
            tenant_id=tenant_id,
            policy="plain",
            reason=reason,
            approved_by=approved_by,
            valid_from=datetime.utcnow(),
            valid_until=valid_until,
            status="active",
        )
        self._active[tenant_id] = record
        self._history.setdefault(tenant_id, []).append(record)
        self.audit_events.append(
            {
                "tenant_id": tenant_id,
                "action": "pii_storage_plain_approved",
                "actor": approved_by,
                "reason": reason,
                "details": {"approval_id": record.approval_id},
            }
        )
        await _publish_ledger(
            self._ledger,
            tenant_id=tenant_id, actor=approved_by,
            action="pii_storage_plain_approved",
            details={"approval_id": record.approval_id, "reason": reason},
        )
        return record

    async def revoke_active(
        self,
        *,
        tenant_id: str,
        revoked_by: str,
        revoke_reason: str,
    ) -> PiiApprovalRecord:
        existing = self._active.pop(tenant_id, None)
        if existing is None:
            raise PiiApprovalNotFoundError(
                f"no active approval for tenant {tenant_id}"
            )
        revoked = PiiApprovalRecord(
            approval_id=existing.approval_id,
            tenant_id=tenant_id,
            policy=existing.policy,
            reason=existing.reason,
            approved_by=existing.approved_by,
            valid_from=existing.valid_from,
            valid_until=existing.valid_until,
            status="revoked",
            revoked_at=datetime.utcnow(),
            revoked_by=revoked_by,
            revoke_reason=revoke_reason,
        )
        # history의 마지막 record를 revoked로 교체
        history = self._history.get(tenant_id, [])
        if history and history[-1].approval_id == existing.approval_id:
            history[-1] = revoked
        self.audit_events.append(
            {
                "tenant_id": tenant_id,
                "action": "pii_storage_plain_revoked",
                "actor": revoked_by,
                "reason": revoke_reason,
                "details": {"approval_id": existing.approval_id},
            }
        )
        await _publish_ledger(
            self._ledger,
            tenant_id=tenant_id, actor=revoked_by,
            action="pii_storage_plain_revoked",
            details={"approval_id": existing.approval_id, "reason": revoke_reason},
        )
        return revoked

    async def is_plain_approved(self, tenant_id: str) -> bool:
        record = self._active.get(tenant_id)
        if record is None:
            return False
        if record.valid_until is not None and record.valid_until <= datetime.utcnow():
            return False
        return True

    async def list_by_tenant(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PiiApprovalRecord]:
        history = list(reversed(self._history.get(tenant_id, [])))
        return history[offset : offset + limit]
