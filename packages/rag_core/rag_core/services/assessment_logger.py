"""AssessmentLogger — ADR-014 §10 assessment_logs INSERT Protocol.

3 mode (extract/generate/hybrid) + criteria + result_summary + validator_summary +
similarity_results + latency_ms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class AssessmentLogPayload:
    tenant_id: str
    request_id: str
    action: str  # 'extract' | 'generate' | 'hybrid'
    actor: str | None = None
    criteria: dict[str, Any] = field(default_factory=dict)
    result_summary: dict[str, Any] = field(default_factory=dict)
    validator_summary: dict[str, Any] = field(default_factory=dict)
    similarity_results: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int | None = None
    created_at: datetime | None = None


class AssessmentLogger(Protocol):
    async def write(self, payload: AssessmentLogPayload) -> None: ...


class InMemoryAssessmentLogger:
    def __init__(self) -> None:
        self.records: list[AssessmentLogPayload] = []

    async def write(self, payload: AssessmentLogPayload) -> None:
        if payload.created_at is None:
            payload.created_at = datetime.utcnow()
        self.records.append(payload)
