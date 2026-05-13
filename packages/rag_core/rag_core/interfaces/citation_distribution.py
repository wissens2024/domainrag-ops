"""CitationDistributionAnalytics — ADR-017 §9 시간 버킷별 citation_type 분포.

Dashboard의 citation_type_distribution이 today 단일 스냅샷이라면 본 서비스는
임의 date range 위에서 day/hour 버킷의 시계열을 노출한다.

InMemory variant는 created_at이 없으므로 모든 record를 단일 'all' 버킷에 집계.
Postgres는 date_trunc(:granularity, created_at) GROUP BY로 시계열 산출.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol


@dataclass
class CitationDistributionBucket:
    bucket: str  # ISO8601 또는 'all' (InMemory)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class CitationDistributionResult:
    granularity: str  # 'day' | 'hour' | 'all'
    buckets: list[CitationDistributionBucket]
    total_messages: int
    from_date: datetime | None = None
    to_date: datetime | None = None


class CitationDistributionAnalytics(Protocol):
    async def distribution(
        self,
        *,
        tenant_id: str,
        from_date: datetime | None,
        to_date: datetime | None,
        group_by: Literal["day", "hour"],
    ) -> CitationDistributionResult: ...


# --------------------------------------------------------------------------- #
# InMemory variant
# --------------------------------------------------------------------------- #


class InMemoryCitationDistributionAnalytics:
    """InMemoryChatLogWriter.records 위에서 동작.

    InMemory는 created_at 없음 → 모든 record를 단일 'all' 버킷에 집계. tenant_id
    필터링은 적용.
    """

    def __init__(self, *, writer) -> None:
        self._writer = writer

    async def distribution(
        self,
        *,
        tenant_id: str,
        from_date: datetime | None,
        to_date: datetime | None,
        group_by: Literal["day", "hour"],
    ) -> CitationDistributionResult:
        records = [r for r in self._writer.records if r.tenant_id == tenant_id]
        counts: dict[str, int] = {}
        for r in records:
            for ct in set(r.citation_types or []):
                counts[ct] = counts.get(ct, 0) + 1
        bucket = CitationDistributionBucket(bucket="all", counts=counts)
        return CitationDistributionResult(
            granularity="all",
            buckets=[bucket] if records else [],
            total_messages=len(records),
            from_date=from_date,
            to_date=to_date,
        )
