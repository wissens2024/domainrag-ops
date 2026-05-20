"""
Chat API — `/api/{tenant_id}/chat`, `/chat/stream` (ADR-017 §3, ADR-013).
"""

import json
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import iso_kst
from app.core.auth_adapter import UserContext, get_user_context
from app.core.config import get_settings
from app.core.db import get_tenant_session
from app.core.tenant_guard import ensure_tenant_match
from app.deps import (
    get_chat_log_reader,
    get_conversation_repository,
    get_feedback_writer,
    get_ledger_audit_service,
    get_rag_service,
)
from app.services.rag_service import RAGService

router = APIRouter()


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    user_id: str | None = None  # 인증된 user_id 사용 권장 (요청 body는 무시)
    question: str
    ui_mode_request: Literal["structured", "streaming", None] = None


@router.post("/chat")
async def chat(
    tenant_id: str,
    req: ChatRequest,
    user: UserContext = Depends(get_user_context),
    rag: RAGService = Depends(get_rag_service),
):
    """Sync chat (chat_structured) — citation 포함 응답 (ADR-010·017 §3.1).

    chat_structured slice (ADR-013 §9 부분집합)를 호출. success / fallback 두 갈래.

    NOTE: chat_logs 저장(save_chat_log 노드)·verifier 결선(verify_tier1/2/3)·
    judge_inference·mask_response_pii는 본 슬라이스에 포함되지 않는다. 이후 같은
    deps 패턴으로 추가된다.
    """
    await ensure_tenant_match(
        tenant_id, user, ledger=get_ledger_audit_service(get_settings())
    )
    return await rag.chat_structured(
        tenant_id=tenant_id,
        user=user,
        question=req.question,
        conversation_id=req.conversation_id,
    )


@router.post("/chat/stream")
async def chat_stream(
    tenant_id: str,
    req: ChatRequest,
    user: UserContext = Depends(get_user_context),
    rag: RAGService = Depends(get_rag_service),
):
    """SSE streaming chat (chat_streaming) — citation 비활성 (ADR-013, ADR-017 §3.2).

    응답 형식 (text/event-stream):
        event: token
        data: {"text": "..."}

        event: complete  (또는 fallback / error)
        data: {"message_id": "...", "metadata": {...}}
    """
    await ensure_tenant_match(
        tenant_id, user, ledger=get_ledger_audit_service(get_settings())
    )
    if not rag.streaming_enabled:
        raise HTTPException(
            status_code=501,
            detail={"error": "streaming_not_configured"},
        )

    iterator = await rag.chat_streaming(
        tenant_id=tenant_id,
        user=user,
        question=req.question,
        conversation_id=req.conversation_id,
    )

    async def _sse() -> AsyncIterator[bytes]:
        async for evt in iterator:
            payload = json.dumps(evt.data, ensure_ascii=False)
            yield f"event: {evt.event}\ndata: {payload}\n\n".encode("utf-8")

    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ----------------------------------------------------------------------------
# Conversation API (ADR-017 §4)
# ----------------------------------------------------------------------------


@router.get("/conversations")
async def list_conversations(
    tenant_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(get_user_context),
    repo=Depends(get_conversation_repository),
):
    """ADR-017 §4 — 본인 대화 목록 (updated_at 최신순)."""
    await ensure_tenant_match(
        tenant_id, user, ledger=get_ledger_audit_service(get_settings())
    )
    result = await repo.list_by_user(
        tenant_id=tenant_id, user_id=user.user_id,
        page=page, page_size=page_size,
    )
    return {
        "items": [_conversation_to_dict(c) for c in result.items],
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
    }


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    tenant_id: str,
    conversation_id: str,
    user: UserContext = Depends(get_user_context),
    repo=Depends(get_conversation_repository),
    chat_log_reader=Depends(get_chat_log_reader),
):
    """ADR-017 §4 — 대화 상세 + 메시지. 메시지는 chat_logs(audit truth)에서 derive.

    각 chat_logs row가 1턴(user question + assistant answer + citations)을
    구성. answer가 fallback인 경우에도 동일 schema로 반환.
    """
    await ensure_tenant_match(
        tenant_id, user, ledger=get_ledger_audit_service(get_settings())
    )
    record = await repo.get(
        tenant_id=tenant_id, user_id=user.user_id, conversation_id=conversation_id,
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "conversation_not_found", "conversation_id": conversation_id},
        )
    messages = await _list_messages(
        tenant_id=tenant_id, conversation_id=conversation_id,
        chat_log_reader=chat_log_reader,
    )
    return {
        **_conversation_to_dict(record),
        "messages": messages,
    }


class ConversationPatchRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(
    tenant_id: str,
    conversation_id: str,
    req: ConversationPatchRequest,
    user: UserContext = Depends(get_user_context),
    repo=Depends(get_conversation_repository),
):
    """ADR-017 §4 — 본인 대화 제목 수정."""
    await ensure_tenant_match(
        tenant_id, user, ledger=get_ledger_audit_service(get_settings())
    )
    updated = await repo.update_title(
        tenant_id=tenant_id, user_id=user.user_id,
        conversation_id=conversation_id, title=req.title,
    )
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "conversation_not_found", "conversation_id": conversation_id},
        )
    return _conversation_to_dict(updated)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    tenant_id: str,
    conversation_id: str,
    user: UserContext = Depends(get_user_context),
    repo=Depends(get_conversation_repository),
):
    """ADR-017 §4 — 본인 대화 삭제. chat_logs는 보존(audit truth, ADR-020 §10 별도)."""
    await ensure_tenant_match(
        tenant_id, user, ledger=get_ledger_audit_service(get_settings())
    )
    affected = await repo.delete(
        tenant_id=tenant_id, user_id=user.user_id, conversation_id=conversation_id,
    )
    if affected == 0:
        raise HTTPException(
            status_code=404,
            detail={"error": "conversation_not_found", "conversation_id": conversation_id},
        )
    return None


def _conversation_to_dict(record) -> dict:
    return {
        "conversation_id": record.id,
        "tenant_id": record.tenant_id,
        "user_id": record.user_id,
        "title": record.title,
        "message_count": record.message_count,
        "created_at": iso_kst(record.created_at),
        "updated_at": iso_kst(record.updated_at),
    }


async def _list_messages(
    *, tenant_id: str, conversation_id: str, chat_log_reader
) -> list[dict]:
    """chat_logs를 conversation_id 필터로 가져와 user/assistant 메시지 쌍으로 변환."""
    from rag_core.interfaces.chat_log_reader import ChatLogListFilters

    # 단일 conversation 내 chat_logs는 통상 ~수십건. 100건 페이지로 한 번에.
    result = await chat_log_reader.list_by_tenant(
        tenant_id=tenant_id,
        filters=ChatLogListFilters(conversation_id=conversation_id),
        page=1, page_size=100,
    )
    messages: list[dict] = []
    # list는 최신순 — 메시지는 오래된 순으로 노출
    for record in reversed(result.items):
        if record.question:
            messages.append({
                "role": "user",
                "content": record.question,
                "message_id": record.request_id,
                "created_at": iso_kst(record.created_at),
            })
        if record.answer is not None:
            messages.append({
                "role": "assistant",
                "content": record.answer,
                "message_id": record.request_id,
                "citations": record.citations,
                "citation_types": record.citation_types,
                "fallback_reason": record.fallback_reason,
                "created_at": iso_kst(record.created_at),
            })
    return messages


class FeedbackRequest(BaseModel):
    message_id: str
    feedback: Literal["good", "bad"]
    comment: str | None = None


@router.post("/feedback", status_code=204)
async def feedback(
    tenant_id: str,
    req: FeedbackRequest,
    user: UserContext = Depends(get_user_context),
    writer=Depends(get_feedback_writer),
):
    """ADR-017 §5 — chat_logs.feedback 갱신. 본인 message만 허용.

    user_id가 다른 message_id를 지정해도 UPDATE 0 → 404 message_not_found 반환
    (존재 여부 노출 차단). service_account은 본 endpoint를 호출할 일이 없지만 동일
    rule로 자동 차단된다.
    """
    await ensure_tenant_match(
        tenant_id, user, ledger=get_ledger_audit_service(get_settings())
    )
    result = await writer.record(
        tenant_id=tenant_id,
        message_id=req.message_id,
        user_id=user.user_id,
        feedback=req.feedback,
        comment=req.comment,
    )
    if result.affected == 0:
        raise HTTPException(
            status_code=404,
            detail={"error": "message_not_found", "message_id": req.message_id},
        )
    return None
