"""ChatLogReader — admin이 chat_logs를 조회하는 단일 진입점 Protocol.

ADR-017 §8 + ADR-019 §2 정합:
  - list_by_tenant: tenant scope 필터·페이징 (최신순)
  - get: tenant_id + request_id로 단건 조회 (모든 컬럼 노출)

본 모듈은 SQLAlchemy 의존 없는 Protocol과 InMemory variant만 정의. 운영 구현
(Postgres + RLS)은 backend/app/repositories/chat_log_reader.py.

상세 응답 schema는 ADR-019 §2 chat_logs 컬럼 전체:
  retrieved_chunks / citations / citation_types / verifier_metrics / routing_decision /
  classifier_decision / model_failure_chain / inference_judge_results / conflict_groups /
  input_pii_found / output_pii_masked / pii_storage_policy / latency_ms / confidence /
  fallback_reason / unsupported_ratio / ui_mode / llm_model / embedding_model /
  reranker_model / prompt_version / question / rewritten_query / answer / created_at.

질문 keyword 필터는 question / rewritten_query / answer ILIKE 매칭.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class ChatLogRecord:
    """chat_logs row 1건 — ADR-019 §2 컬럼 매핑."""

    id: str
    tenant_id: str
    request_id: str
    user_id: str | None
    conversation_id: str | None
    question: str | None
    rewritten_query: str | None
    answer: str | None
    created_at: datetime | None

    retrieved_chunks: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    citation_types: list[str] = field(default_factory=list)
    verifier_metrics: dict[str, Any] = field(default_factory=dict)
    routing_decision: dict[str, Any] = field(default_factory=dict)
    classifier_decision: dict[str, Any] = field(default_factory=dict)
    model_failure_chain: list[dict[str, Any]] = field(default_factory=list)
    inference_judge_results: list[dict[str, Any]] = field(default_factory=list)
    conflict_groups: list[Any] = field(default_factory=list)
    input_pii_found: list[dict[str, Any]] = field(default_factory=list)
    output_pii_masked: list[dict[str, Any]] = field(default_factory=list)

    llm_model: str | None = None
    embedding_model: str | None = None
    reranker_model: str | None = None
    prompt_version: str | None = None
    latency_ms: int | None = None
    ui_mode: str | None = None
    confidence: float | None = None
    fallback_reason: str | None = None
    unsupported_ratio: float | None = None
    pii_storage_policy: str | None = None


@dataclass
class ChatLogListFilters:
    """list_by_tenant 필터 — endpoint Query param과 1:1 매핑.

    None은 미지정. fallback_only=True면 fallback_reason IS NOT NULL.
    keyword는 question / rewritten_query / answer 부분 일치(ILIKE %k%).
    citation_type은 citation_types JSONB 배열 안에 해당 문자열이 있는지.
    """

    user_id: str | None = None
    conversation_id: str | None = None
    from_date: datetime | None = None  # created_at >= from_date
    to_date: datetime | None = None  # created_at < to_date
    fallback_only: bool = False
    ui_mode: str | None = None
    keyword: str | None = None
    citation_type: str | None = None
    min_confidence: float | None = None
    max_confidence: float | None = None


@dataclass
class ChatLogListResult:
    items: list[ChatLogRecord]
    total: int
    page: int
    page_size: int


class ChatLogReader(Protocol):
    async def list_by_tenant(
        self,
        *,
        tenant_id: str,
        filters: ChatLogListFilters,
        page: int = 1,
        page_size: int = 20,
    ) -> ChatLogListResult: ...

    async def get(
        self, *, tenant_id: str, request_id: str
    ) -> ChatLogRecord | None: ...


# --------------------------------------------------------------------------- #
# InMemory variant — dev / tests, InMemoryChatLogWriter.records를 공유
# --------------------------------------------------------------------------- #


class InMemoryChatLogReader:
    """InMemoryChatLogWriter.records 위에 동작하는 reader.

    Args:
        writer: 기존 InMemoryChatLogWriter — records 리스트 공유.
    """

    def __init__(self, *, writer) -> None:
        self._writer = writer

    async def list_by_tenant(
        self,
        *,
        tenant_id: str,
        filters: ChatLogListFilters,
        page: int = 1,
        page_size: int = 20,
    ) -> ChatLogListResult:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))

        rows = [r for r in self._writer.records if r.tenant_id == tenant_id]
        rows = [r for r in rows if _row_matches(r, filters)]
        # InMemoryChatLogWriter는 append 순서가 시간순 — 최신순으로 reverse
        rows = list(reversed(rows))
        total = len(rows)
        start = (page - 1) * page_size
        items = [
            _payload_to_record(payload, index=total - 1 - i)
            for i, payload in enumerate(rows[start : start + page_size], start=start)
        ]
        return ChatLogListResult(
            items=items, total=total, page=page, page_size=page_size
        )

    async def get(
        self, *, tenant_id: str, request_id: str
    ) -> ChatLogRecord | None:
        for r in self._writer.records:
            if r.tenant_id == tenant_id and r.request_id == request_id:
                return _payload_to_record(r, index=None)
        return None


def _row_matches(payload, f: ChatLogListFilters) -> bool:
    if f.user_id is not None and payload.user_id != f.user_id:
        return False
    if f.conversation_id is not None and payload.conversation_id != f.conversation_id:
        return False
    if f.fallback_only and not payload.fallback_reason:
        return False
    if f.ui_mode is not None and payload.ui_mode != f.ui_mode:
        return False
    if f.citation_type is not None and f.citation_type not in (payload.citation_types or []):
        return False
    if f.min_confidence is not None and (payload.confidence or 0.0) < f.min_confidence:
        return False
    if f.max_confidence is not None and (payload.confidence or 0.0) > f.max_confidence:
        return False
    if f.keyword:
        needle = f.keyword.lower()
        haystack = " ".join(
            x
            for x in (payload.question, payload.rewritten_query, payload.answer)
            if x is not None
        ).lower()
        if needle not in haystack:
            return False
    # InMemory에서는 created_at이 없으므로 from/to_date 필터는 skip — 운영에서만 의미.
    # 호출자가 InMemory 환경에서 날짜 필터를 사용하면 결과는 그대로 통과한다.
    return True


def _payload_to_record(payload, *, index: int | None) -> ChatLogRecord:
    """InMemory payload → ChatLogRecord. id는 stable한 dummy (index 기반)."""
    record_id = f"inmem-{index:08d}" if index is not None else f"inmem-{payload.request_id}"
    return ChatLogRecord(
        id=record_id,
        tenant_id=payload.tenant_id,
        request_id=payload.request_id,
        user_id=payload.user_id,
        conversation_id=payload.conversation_id,
        question=payload.question,
        rewritten_query=payload.rewritten_query,
        answer=payload.answer,
        created_at=None,
        retrieved_chunks=list(payload.retrieved_chunks),
        citations=list(payload.citations),
        citation_types=list(payload.citation_types),
        verifier_metrics=dict(payload.verifier_metrics),
        routing_decision=dict(payload.routing_decision),
        classifier_decision=dict(payload.classifier_decision),
        model_failure_chain=list(payload.model_failure_chain),
        inference_judge_results=list(payload.inference_judge_results),
        conflict_groups=list(payload.conflict_groups),
        input_pii_found=list(payload.input_pii_found),
        output_pii_masked=list(payload.output_pii_masked),
        llm_model=payload.llm_model,
        embedding_model=payload.embedding_model,
        reranker_model=payload.reranker_model,
        prompt_version=payload.prompt_version,
        latency_ms=payload.latency_ms,
        ui_mode=payload.ui_mode,
        confidence=payload.confidence,
        fallback_reason=payload.fallback_reason,
        unsupported_ratio=payload.unsupported_ratio,
        pii_storage_policy=payload.pii_storage_policy,
    )
