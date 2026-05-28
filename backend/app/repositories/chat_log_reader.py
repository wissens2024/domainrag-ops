"""PostgresChatLogReader — chat_logs read (ADR-017 §8 + ADR-019 §2).

흐름:
  1. set_tenant_context (RLS) — tenant scope 자동 제한
  2. dynamic WHERE 구성 (필터 None은 skip)
  3. COUNT + SELECT 두 번 (페이징 total 산출)
  4. JSONB 컬럼은 sqlalchemy가 dict/list로 자동 디코드 (asyncpg driver) — 그대로 전달

ADR-019 §2 partitioning은 created_at 기반 RANGE — from_date/to_date 필터는
PostgreSQL이 자동으로 partition pruning한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.chat_log_reader import (
    ChatLogListFilters,
    ChatLogListResult,
    ChatLogRecord,
)

from app.core.rls import set_tenant_context


_SELECT_COLUMNS = """
    id, domain_id, request_id, user_id, conversation_id,
    question, rewritten_query, answer,
    retrieved_chunks, citations, citation_types,
    verifier_metrics, routing_decision, classifier_decision,
    model_failure_chain, inference_judge_results, conflict_groups,
    input_pii_found, output_pii_masked, pii_storage_policy,
    llm_model, embedding_model, reranker_model, prompt_version,
    latency_ms, ui_mode, confidence, fallback_reason, unsupported_ratio,
    created_at
"""


class PostgresChatLogReader:
    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def list_by_tenant(
        self,
        *,
        domain_id: str,
        filters: ChatLogListFilters,
        page: int = 1,
        page_size: int = 20,
    ) -> ChatLogListResult:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        offset = (page - 1) * page_size

        where, params = _build_where(domain_id, filters)
        params.update({"limit": page_size, "offset": offset})

        async with self._sf() as session:
            await set_tenant_context(session, domain_id)

            total_row = (
                await session.execute(
                    text(f"SELECT COUNT(*) FROM chat_logs WHERE {where}"),
                    {k: v for k, v in params.items() if k not in {"limit", "offset"}},
                )
            ).first()
            total = int(total_row[0]) if total_row else 0

            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT {_SELECT_COLUMNS}
                          FROM chat_logs
                         WHERE {where}
                         ORDER BY created_at DESC
                         LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
            ).all()

        items = [_row_to_record(r) for r in rows]
        return ChatLogListResult(
            items=items, total=total, page=page, page_size=page_size
        )

    async def get(
        self, *, domain_id: str, request_id: str
    ) -> ChatLogRecord | None:
        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT {_SELECT_COLUMNS}
                          FROM chat_logs
                         WHERE domain_id = :domain_id AND request_id = :request_id
                         ORDER BY created_at DESC
                         LIMIT 1
                        """
                    ),
                    {"domain_id": domain_id, "request_id": request_id},
                )
            ).first()
        return _row_to_record(row) if row else None


def _build_where(
    domain_id: str, f: ChatLogListFilters
) -> tuple[str, dict[str, Any]]:
    clauses = ["domain_id = :domain_id"]
    params: dict[str, Any] = {"domain_id": domain_id}
    if f.user_id is not None:
        clauses.append("user_id = :user_id")
        params["user_id"] = f.user_id
    if f.conversation_id is not None:
        clauses.append("conversation_id = CAST(:conversation_id AS UUID)")
        params["conversation_id"] = f.conversation_id
    if f.from_date is not None:
        clauses.append("created_at >= :from_date")
        params["from_date"] = f.from_date
    if f.to_date is not None:
        clauses.append("created_at < :to_date")
        params["to_date"] = f.to_date
    if f.fallback_only:
        clauses.append("fallback_reason IS NOT NULL")
    if f.ui_mode is not None:
        clauses.append("ui_mode = :ui_mode")
        params["ui_mode"] = f.ui_mode
    if f.citation_type is not None:
        clauses.append("citation_types @> CAST(:citation_type_json AS JSONB)")
        # `citation_types`는 JSONB 배열 — `[<value>]`이 부분집합인지 검사
        import json as _json

        params["citation_type_json"] = _json.dumps([f.citation_type])
    if f.min_confidence is not None:
        clauses.append("confidence >= :min_confidence")
        params["min_confidence"] = f.min_confidence
    if f.max_confidence is not None:
        clauses.append("confidence <= :max_confidence")
        params["max_confidence"] = f.max_confidence
    if f.keyword:
        clauses.append(
            "(question ILIKE :kw OR rewritten_query ILIKE :kw OR answer ILIKE :kw)"
        )
        params["kw"] = f"%{f.keyword}%"
    return " AND ".join(clauses), params


def _row_to_record(r) -> ChatLogRecord:
    return ChatLogRecord(
        id=str(r[0]),
        domain_id=str(r[1]),
        request_id=str(r[2]),
        user_id=r[3],
        conversation_id=str(r[4]) if r[4] is not None else None,
        question=r[5],
        rewritten_query=r[6],
        answer=r[7],
        retrieved_chunks=list(r[8] or []),
        citations=list(r[9] or []),
        citation_types=list(r[10] or []),
        verifier_metrics=dict(r[11] or {}),
        routing_decision=dict(r[12] or {}),
        classifier_decision=dict(r[13] or {}),
        model_failure_chain=list(r[14] or []),
        inference_judge_results=list(r[15] or []),
        conflict_groups=list(r[16] or []),
        input_pii_found=list(r[17] or []),
        output_pii_masked=list(r[18] or []),
        pii_storage_policy=r[19],
        llm_model=r[20],
        embedding_model=r[21],
        reranker_model=r[22],
        prompt_version=r[23],
        latency_ms=r[24],
        ui_mode=r[25],
        confidence=r[26],
        fallback_reason=r[27],
        unsupported_ratio=r[28],
        created_at=_as_datetime(r[29]),
    )


def _as_datetime(value) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else None
