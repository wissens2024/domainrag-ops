"""TenantGuard — URL path domain_id ↔ UserContext.domain_id 검증 통일.

ADR-008 §2 격리 3중 방어의 두 번째 layer (JWT claim·URL path mirror — mismatch 시 403).
tenant_admin / tenant_chat / tenant_me 모든 endpoint가 같은 함수를 호출하도록 통일하고,
mismatch 발생 시 ADR-020 §8 LedgerAuditService.publish_tenant_mismatch도 한 군데서 처리.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from app.core.auth_adapter import UserContext

logger = logging.getLogger(__name__)


async def ensure_tenant_match(
    domain_id: str,
    user: UserContext,
    *,
    ledger: Any | None = None,
) -> None:
    """ADR-008 §2 — URL path domain_id가 token의 domain_id와 다르면 403.

    platform_admin role은 모든 tenant 호출 허용 (cross-tenant 운영 메뉴).
    mismatch 발생 시 ledger가 주입되어 있으면 publish_tenant_mismatch.
    """
    if user.is_platform_admin:
        return
    if user.domain_id == domain_id:
        return

    logger.warning(
        "ensure_tenant_match 403 — path_tenant=%s user_tenant=%s user_id=%s roles=%s",
        domain_id,
        user.domain_id,
        user.user_id,
        user.roles,
    )

    # ledger publish (실패는 swallow)
    if ledger is not None:
        try:
            await ledger.publish_tenant_mismatch(
                domain_id=domain_id,
                actor=user.user_id,
                expected_tenant=domain_id,
                token_tenant=user.domain_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ledger publish_tenant_mismatch failed: %s", exc)

    raise HTTPException(
        status_code=403,
        detail={
            "error": "tenant_mismatch",
            "expected_domain_id": domain_id,
            "token_domain_id": user.domain_id,
        },
    )
