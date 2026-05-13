"""ChatLogEraser — right-to-erasure (ADR-020 §10) + ADR-012 hard delete 정합 Protocol.

본인 chat_logs에 대해 `mask_only`(컬럼 단위 NULL/마스킹) 또는 `hard_delete`(row DELETE)를
수행한다. tenant_id RLS context는 구현체가 책임진다. rag_core는 인터페이스만 정의하고,
운영 구현은 backend의 PostgresChatLogEraser (SQLAlchemy + RLS).

mask_only 컬럼 정책 (ADR-020 §10):
  - question / rewritten_query / answer → NULL
  - retrieved_chunks / citations / input_pii_found / output_pii_masked → '[]'
  - pii_storage_policy → 'erased' (운영자가 식별 가능하도록 별도 sentinel)
  - 그 외 운영 지표(latency_ms / routing_decision / verifier_metrics / model_failure_chain
    / classifier_decision / inference_judge_results / conflict_groups / ui_mode /
    confidence / fallback_reason / unsupported_ratio)는 보존
  - user_id → NULL (다른 user의 logs와 식별 분리)

hard_delete:
  - chat_logs row 자체 DELETE.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ErasureMode(str, Enum):
    MASK_ONLY = "mask_only"
    HARD_DELETE = "hard_delete"


@dataclass
class ErasureResult:
    """erase_my_logs 결과 — endpoint 응답 + audit 기록에 모두 사용."""

    tenant_id: str
    user_id: str
    mode: ErasureMode
    affected_rows: int
    reason: str


class ChatLogEraser(Protocol):
    async def erase_my_logs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        mode: ErasureMode,
        reason: str,
    ) -> ErasureResult: ...


class InMemoryChatLogEraser:
    """InMemoryChatLogWriter.records를 대상으로 동작하는 테스트·dev 구현체.

    Args:
        writer: 기존 InMemoryChatLogWriter — records 리스트를 공유해 실시간 erase 효과 검증.
    """

    def __init__(self, *, writer) -> None:
        self._writer = writer

    async def erase_my_logs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        mode: ErasureMode,
        reason: str,
    ) -> ErasureResult:
        affected = 0
        new_records = []
        for rec in self._writer.records:
            if rec.tenant_id == tenant_id and rec.user_id == user_id:
                if mode == ErasureMode.HARD_DELETE:
                    affected += 1
                    continue
                # mask_only — 컬럼 단위 마스킹
                rec.question = None  # type: ignore[assignment]
                rec.rewritten_query = None
                rec.answer = None  # type: ignore[assignment]
                rec.retrieved_chunks = []
                rec.citations = []
                rec.input_pii_found = []
                rec.output_pii_masked = []
                rec.pii_storage_policy = "erased"
                rec.user_id = None
                affected += 1
            new_records.append(rec)
        self._writer.records = new_records
        return ErasureResult(
            tenant_id=tenant_id,
            user_id=user_id,
            mode=mode,
            affected_rows=affected,
            reason=reason,
        )
