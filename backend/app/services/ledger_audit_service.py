"""LedgerAuditService — ADR-020 §8 4종(+α) 보안 이벤트 publish helper.

caller는 publish_* 메서드를 호출. enable=false면 no-op 반환. Ledger 가용성 실패는
client가 swallow하므로 본 service도 raise하지 않는다.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.ledger_client import LedgerClient, LedgerEvent, now_iso

logger = logging.getLogger(__name__)


_DEFAULT_EVENTS = frozenset(
    {
        "auth_failure",
        "tenant_mismatch",
        "hard_delete",
        "platform_admin_action",
        "config_change_breaking",
        "pii_high_severity_block",
    }
)


class LedgerAuditService:
    """ADR-020 §8 publish facade.

    Args:
        client: LedgerClient (Noop 또는 Httpx)
        enable: configs/platform/audit.yaml.authfusion_ledger.enable
        enabled_events: configs의 events list (subset of _DEFAULT_EVENTS). 빈 set이면 default
            전체 활성. enable=false면 의미 없음.
    """

    def __init__(
        self,
        *,
        client: LedgerClient,
        enable: bool = True,
        enabled_events: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._client = client
        self._enable = enable
        self._enabled_events = frozenset(enabled_events or _DEFAULT_EVENTS)

    @property
    def enabled(self) -> bool:
        return self._enable

    async def _publish(
        self,
        *,
        event_type: str,
        domain_id: str,
        actor: str | None,
        reason: str | None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        if not self._enable or event_type not in self._enabled_events:
            return False
        return await self._client.publish(
            LedgerEvent(
                event_type=event_type,
                domain_id=domain_id,
                actor=actor,
                reason=reason,
                details=dict(details or {}),
                timestamp=now_iso(),
            )
        )

    # ---------------------------------------------------------------- #
    # 4종 + 확장 이벤트 helper
    # ---------------------------------------------------------------- #

    async def publish_auth_failure(
        self,
        *,
        domain_id: str,
        actor: str | None,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        return await self._publish(
            event_type="auth_failure",
            domain_id=domain_id, actor=actor, reason=reason, details=details,
        )

    async def publish_tenant_mismatch(
        self,
        *,
        domain_id: str,
        actor: str | None,
        expected_tenant: str | None,
        token_tenant: str | None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        merged = dict(details or {})
        merged.setdefault("expected_tenant", expected_tenant)
        merged.setdefault("token_tenant", token_tenant)
        return await self._publish(
            event_type="tenant_mismatch",
            domain_id=domain_id,
            actor=actor,
            reason="path domain_id mismatch with token claim",
            details=merged,
        )

    async def publish_hard_delete(
        self,
        *,
        domain_id: str,
        actor: str,
        reason: str,
        doc_id: str,
        version: str | None,
        removed_chunks: int,
        affected_chat_logs: int,
        chat_logs_action: str,
    ) -> bool:
        return await self._publish(
            event_type="hard_delete",
            domain_id=domain_id, actor=actor, reason=reason,
            details={
                "doc_id": doc_id,
                "version": version,
                "removed_chunks": removed_chunks,
                "affected_chat_logs": affected_chat_logs,
                "chat_logs_action": chat_logs_action,
            },
        )

    async def publish_platform_admin_action(
        self,
        *,
        domain_id: str,
        actor: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        merged = dict(details or {})
        merged.setdefault("action", action)
        return await self._publish(
            event_type="platform_admin_action",
            domain_id=domain_id, actor=actor, reason=action, details=merged,
        )

    async def publish_config_change_breaking(
        self,
        *,
        domain_id: str,
        actor: str,
        category: str,
        path: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        merged = dict(details or {})
        merged.update({"category": category, "path": path})
        return await self._publish(
            event_type="config_change_breaking",
            domain_id=domain_id, actor=actor,
            reason=f"breaking config change: {category}.{path}",
            details=merged,
        )

    async def publish_pii_high_severity_block(
        self,
        *,
        domain_id: str,
        actor: str | None,
        categories: list[str],
        details: dict[str, Any] | None = None,
    ) -> bool:
        merged = dict(details or {})
        merged["blocked_categories"] = list(categories)
        return await self._publish(
            event_type="pii_high_severity_block",
            domain_id=domain_id, actor=actor,
            reason="high severity PII detected in user input",
            details=merged,
        )
