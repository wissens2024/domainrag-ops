"""PostgresChatLogWriter — chat_logs INSERT (RLS + month partitioning).

ADR-019 §2 partitioning + ADR-008 RLS + ADR-013 §10 컬럼 매핑.

흐름:
  1. tenant context SET LOCAL (RLS)
  2. payload.conversation_id None이면 conversations row INSERT (auto-create)
  3. chat_logs row INSERT
  4. commit
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.services.chat_log_writer import ChatLogPayload

from app.core.rls import set_tenant_context

logger = structlog.get_logger(__name__)


def _json(value: Any) -> str:
    """JSONB 컬럼에 안전하게 직렬화. None/빈값은 'null'/'[]'/'{}' 그대로."""
    return json.dumps(value, ensure_ascii=False, default=str)


class PostgresChatLogWriter:
    """SQLAlchemy AsyncSession 기반 chat_logs writer.

    Args:
        session_factory: async_sessionmaker (app DB engine — RLS 적용)
    """

    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def write(self, payload: ChatLogPayload) -> str:
        async with self._session_factory() as session:
            await set_tenant_context(session, payload.tenant_id)

            conversation_id = payload.conversation_id
            if conversation_id is None:
                # legacy fallback — caller가 conversation_id를 미리 발급 안 한 경우.
                # Postgres가 gen_random_uuid()로 발급. 현재 rag_service/streaming은
                # 항상 upfront 발급하므로 이 분기는 직접 ChatLogWriter를 호출하는
                # 비정상 경로(테스트·운영 스크립트)에서만 동작한다.
                row = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO conversations (tenant_id, user_id, title)
                            VALUES (:tenant_id, :user_id, :title)
                            RETURNING id
                            """
                        ),
                        {
                            "tenant_id": payload.tenant_id,
                            "user_id": payload.user_id,
                            "title": (payload.question or "")[:80],
                        },
                    )
                ).first()
                conversation_id = str(row[0])
            else:
                # caller(rag_service/streaming)가 발급한 conversation_id — Conversation
                # API(ADR-017 §4) lookup 정합. 동일 conversation_id로 후속 turn이 오면
                # ON CONFLICT DO NOTHING으로 멱등 동작.
                await session.execute(
                    text(
                        """
                        INSERT INTO conversations (id, tenant_id, user_id, title)
                        VALUES (CAST(:cid AS UUID), :tenant_id, :user_id, :title)
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "cid": conversation_id,
                        "tenant_id": payload.tenant_id,
                        "user_id": payload.user_id,
                        "title": (payload.question or "")[:80],
                    },
                )

            await session.execute(
                text(
                    """
                    INSERT INTO chat_logs (
                        tenant_id, request_id, user_id, conversation_id,
                        question, rewritten_query, answer,
                        retrieved_chunks, citations, citation_types,
                        llm_model, embedding_model, reranker_model, prompt_version,
                        latency_ms, ui_mode, confidence, fallback_reason,
                        unsupported_ratio, verifier_metrics, routing_decision,
                        classifier_decision, model_failure_chain,
                        inference_judge_results, conflict_groups,
                        input_pii_found, output_pii_masked, pii_storage_policy
                    )
                    VALUES (
                        :tenant_id, :request_id, :user_id, :conversation_id,
                        :question, :rewritten_query, :answer,
                        CAST(:retrieved_chunks AS JSONB),
                        CAST(:citations AS JSONB),
                        CAST(:citation_types AS JSONB),
                        :llm_model, :embedding_model, :reranker_model, :prompt_version,
                        :latency_ms, :ui_mode, :confidence, :fallback_reason,
                        :unsupported_ratio,
                        CAST(:verifier_metrics AS JSONB),
                        CAST(:routing_decision AS JSONB),
                        CAST(:classifier_decision AS JSONB),
                        CAST(:model_failure_chain AS JSONB),
                        CAST(:inference_judge_results AS JSONB),
                        CAST(:conflict_groups AS JSONB),
                        CAST(:input_pii_found AS JSONB),
                        CAST(:output_pii_masked AS JSONB),
                        :pii_storage_policy
                    )
                    """
                ),
                {
                    "tenant_id": payload.tenant_id,
                    "request_id": payload.request_id,
                    "user_id": payload.user_id,
                    "conversation_id": conversation_id,
                    "question": payload.question,
                    "rewritten_query": payload.rewritten_query,
                    "answer": payload.answer,
                    "retrieved_chunks": _json(payload.retrieved_chunks),
                    "citations": _json(payload.citations),
                    "citation_types": _json(payload.citation_types),
                    "llm_model": payload.llm_model,
                    "embedding_model": payload.embedding_model,
                    "reranker_model": payload.reranker_model,
                    "prompt_version": payload.prompt_version,
                    "latency_ms": payload.latency_ms,
                    "ui_mode": payload.ui_mode,
                    "confidence": payload.confidence,
                    "fallback_reason": payload.fallback_reason,
                    "unsupported_ratio": payload.unsupported_ratio,
                    "verifier_metrics": _json(payload.verifier_metrics),
                    "routing_decision": _json(payload.routing_decision),
                    "classifier_decision": _json(payload.classifier_decision),
                    "model_failure_chain": _json(payload.model_failure_chain),
                    "inference_judge_results": _json(payload.inference_judge_results),
                    "conflict_groups": _json(payload.conflict_groups),
                    "input_pii_found": _json(payload.input_pii_found),
                    "output_pii_masked": _json(payload.output_pii_masked),
                    "pii_storage_policy": payload.pii_storage_policy,
                },
            )
            await session.commit()
            return str(conversation_id)


class _BestEffortChatLogWriter:
    """write 실패를 swallow하고 로그만 남기는 wrapper.

    chat_logs 저장 실패가 사용자 응답 자체를 fail시키면 운영 risk(인프라 단절 시
    전체 chat 차단)가 너무 큼. ADR-022 후보 — 신뢰성·관측성 별도 ADR로 정식화.
    """

    def __init__(self, *, inner: "PostgresChatLogWriter") -> None:
        self._inner = inner

    async def write(self, payload: ChatLogPayload) -> str:
        try:
            return await self._inner.write(payload)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "chat_log_write_failed",
                tenant_id=payload.tenant_id,
                request_id=payload.request_id,
                exc_type=type(e).__name__,
                exc_msg=str(e),
            )
            # caller에 conversation_id를 return해야 하므로 임시 uuid 발급
            return payload.conversation_id or f"unsaved-{uuid.uuid4().hex[:12]}"


def build_postgres_chat_log_writer(
    *,
    session_factory: async_sessionmaker,
    swallow_errors: bool = True,
):
    writer = PostgresChatLogWriter(session_factory=session_factory)
    if swallow_errors:
        return _BestEffortChatLogWriter(inner=writer)
    return writer
