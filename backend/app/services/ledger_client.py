"""AuthFusionLedgerClient — ADR-020 §8 보안 이벤트 publish 클라이언트.

Protocol + httpx 운영 구현 + Noop 구현(enable=false 또는 endpoint 미설정 시).
Ledger 장애는 DomainRAG 응답을 막지 않는다 — 실패 시 log + skip(swallow).

운영 의미:
  - hash chain 무결성 보장 (FAU_STG.1)
  - 사고 조사 시 단일 ledger로 cross-system 감사 가능
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LedgerEvent:
    event_type: str  # auth_failure | tenant_mismatch | hard_delete | platform_admin_action | ...
    tenant_id: str
    actor: str | None
    reason: str | None
    details: dict[str, Any]
    timestamp: str  # ISO8601 with offset

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "source_system": "domainrag",
            "tenant_id": self.tenant_id,
            "actor": self.actor,
            "reason": self.reason,
            "details": self.details,
            "timestamp": self.timestamp,
        }


class LedgerClient(Protocol):
    async def publish(self, event: LedgerEvent) -> bool:
        """publish 결과를 bool로 반환 — True=성공, False=실패(swallow된 케이스).

        본 메서드는 raise 하지 않는다 — Ledger 가용성 의존이 DomainRAG 응답을 차단하지
        않게 caller에서 단순히 bool로 활용.
        """
        ...


class NoopLedgerClient:
    """ledger.enable=false 또는 endpoint 미설정 시 no-op."""

    def __init__(self) -> None:
        self.published: list[LedgerEvent] = []  # 테스트 가시성

    async def publish(self, event: LedgerEvent) -> bool:
        # 운영에서는 호출 자체가 안 일어남 (LedgerAuditService가 enable check). 단순 기록만.
        self.published.append(event)
        return True


class HttpxLedgerClient:
    """AuthFusion Ledger (port 8089) HTTP 클라이언트.

    POST `{endpoint}/api/v1/events` + Bearer api_key. 실패 시 1회 retry. 모든 예외는
    swallow + log — caller는 raise하지 않는다.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None = None,
        timeout_seconds: float = 3.0,
        max_retries: int = 1,
    ) -> None:
        if not endpoint:
            raise ValueError("ledger endpoint required")
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)

    async def publish(self, event: LedgerEvent) -> bool:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._endpoint}/api/v1/events"
        payload = event.to_payload()

        attempts = self._max_retries + 1
        last_err: Exception | None = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code < 400:
                    return True
                last_err = RuntimeError(f"ledger HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            # 마지막 attempt 아니면 retry
        logger.warning(
            "ledger publish failed: event_type=%s tenant=%s err=%s (swallow)",
            event.event_type,
            event.tenant_id,
            last_err,
        )
        return False


def now_iso() -> str:
    """ADR-020 §8 timestamp — ISO8601 with offset (Asia/Seoul는 운영 환경 결정).

    UTC를 그대로 ISO8601로 반환. 후속 분석에서 timezone 변환은 ledger 측 책임.
    """
    return datetime.now(timezone.utc).isoformat()
