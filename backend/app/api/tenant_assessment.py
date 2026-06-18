"""Assessment API — ADR-014 + ADR-017 §17.

User endpoints (`/api/{domain_id}/assessment/*`):
  - POST /extract
  - POST /generate
  - POST /hybrid

Admin endpoints (`/api/{domain_id}/admin/assessment/*`):
  - GET   /items
  - POST  /items
  - GET   /items/{item_id}
  - PATCH /items/{item_id}
  - POST  /items/{item_id}/approve
  - GET   /review-queue
  - GET   /analytics
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.timezone import iso_kst
from app.core.auth_adapter import UserContext, get_user_context
from app.core.config import get_settings
from app.core.tenant_guard import ensure_tenant_match
from app.deps import (
    get_assessment_extract_service,
    get_assessment_generate_service,
    get_assessment_hybrid_service,
    get_assessment_item_repository,
    get_assessment_logger,
    get_ledger_audit_service,
)

router = APIRouter()
admin_router = APIRouter()


def require_admin(user: UserContext = Depends(get_user_context)) -> UserContext:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail={"error": "insufficient_role"})
    return user


async def _tenant_guard(domain_id: str, user: UserContext) -> None:
    await ensure_tenant_match(
        domain_id, user, ledger=get_ledger_audit_service(get_settings())
    )


def _item_to_dict(rec) -> dict[str, Any]:
    return {
        "item_id": rec.item_id,
        "domain_id": rec.domain_id,
        "subject": rec.subject,
        "chapter": rec.chapter,
        "difficulty": rec.difficulty,
        "question_type": rec.question_type,
        "question_text": rec.question_text,
        "choices": rec.choices,
        "answer": rec.answer,
        "explanation": rec.explanation,
        "tags": rec.tags,
        "assets": rec.assets,
        "figure_dependent": rec.figure_dependent,
        "quality_status": rec.quality_status,
        "quality_score": rec.quality_score,
        "validator_results": rec.validator_results,
        "used_count": rec.used_count,
        "last_used_at": iso_kst(rec.last_used_at),
        "source": rec.source,
        "reference_item_ids": rec.reference_item_ids,
        "created_at": iso_kst(rec.created_at),
        "updated_at": iso_kst(rec.updated_at),
    }


# --------------------------------------------------------------------------- #
# User endpoints — 3 modes
# --------------------------------------------------------------------------- #


class ExtractRequest(BaseModel):
    subject: str | None = None
    chapter: str | None = None
    difficulty: str | None = None
    difficulty_distribution: dict[str, int] | None = None
    quality_status: list[str] = Field(default_factory=lambda: ["approved"])
    exclude_recent_days: int | None = None
    tags_any: list[str] = Field(default_factory=list)
    count: int | None = None  # 추출 개수 상한 (None이면 service default)


@router.post("/extract")
async def extract_items(
    domain_id: str,
    req: ExtractRequest,
    user: UserContext = Depends(get_user_context),
    service=Depends(get_assessment_extract_service),
    logger=Depends(get_assessment_logger),
):
    """ADR-014 §3 Mode 1 — 기존 item pool에서 조건 매칭 추출."""
    await _tenant_guard(domain_id, user)
    from rag_core.interfaces.assessment_item_repository import ExtractCriteria
    from rag_core.services.assessment_logger import AssessmentLogPayload

    request_id = uuid.uuid4().hex
    start = time.perf_counter()
    result = await service.extract(
        domain_id=domain_id,
        criteria=ExtractCriteria(
            subject=req.subject,
            chapter=req.chapter,
            difficulty=req.difficulty,
            difficulty_distribution=req.difficulty_distribution,
            quality_status=req.quality_status,
            exclude_recent_days=req.exclude_recent_days,
            tags_any=req.tags_any,
        ),
    )
    # count 상한 — service가 difficulty_distribution sum 외 기본 N에 의존하므로
    # 명시 count는 후처리로 cap (ExtractCriteria signature 변경 회피).
    if req.count is not None and req.count >= 0:
        items_out = list(result.items)[: req.count]
        extracted_count = min(result.extracted_count, req.count)
    else:
        items_out = list(result.items)
        extracted_count = result.extracted_count
    latency_ms = int((time.perf_counter() - start) * 1000)
    await logger.write(
        AssessmentLogPayload(
            domain_id=domain_id,
            request_id=request_id,
            action="extract",
            actor=user.user_id,
            criteria=req.model_dump(exclude_none=True),
            result_summary={
                "extracted_count": extracted_count,
                "insufficient_pool": result.insufficient_pool,
            },
            latency_ms=latency_ms,
        )
    )
    return {
        "request_id": request_id,
        "domain_id": domain_id,
        "mode": "extract",
        "items": [_item_to_dict(r) for r in items_out],
        "extracted_count": extracted_count,
        "citations": result.citations,
        "insufficient_pool": result.insufficient_pool,
        "latency_ms": latency_ms,
    }


class GenerateRequest(BaseModel):
    subject: str
    chapter: str | None = None
    difficulty: str = "medium"
    count: int = Field(default=5, ge=1, le=20)
    question_type: str = "multiple_choice"


@router.post("/generate")
async def generate_items(
    domain_id: str,
    req: GenerateRequest,
    user: UserContext = Depends(get_user_context),
    service=Depends(get_assessment_generate_service),
    logger=Depends(get_assessment_logger),
):
    """ADR-014 §3 Mode 2 — LLM 신규 생성 + similarity + validator."""
    await _tenant_guard(domain_id, user)
    from rag_core.services.assessment_generate import GenerateCriteria
    from rag_core.services.assessment_logger import AssessmentLogPayload

    request_id = uuid.uuid4().hex
    start = time.perf_counter()
    result = await service.generate(
        domain_id=domain_id,
        criteria=GenerateCriteria(
            subject=req.subject,
            chapter=req.chapter,
            difficulty=req.difficulty,
            count=req.count,
            question_type=req.question_type,
        ),
        actor=user.user_id,
    )
    latency_ms = int((time.perf_counter() - start) * 1000)
    await logger.write(
        AssessmentLogPayload(
            domain_id=domain_id,
            request_id=request_id,
            action="generate",
            actor=user.user_id,
            criteria=req.model_dump(exclude_none=True),
            result_summary={
                "generated_count": result.generated_count,
                "rejected_duplicates": result.rejected_duplicates,
                "retries_used": result.retries_used,
            },
            validator_summary=result.validator_summary,
            similarity_results=result.similarity_results,
            latency_ms=latency_ms,
        )
    )
    return {
        "request_id": request_id,
        "domain_id": domain_id,
        "mode": "generate",
        "items": [_item_to_dict(r) for r in result.items],
        "generated_count": result.generated_count,
        "citations": result.citations,
        "validator_summary": result.validator_summary,
        "similarity_results": result.similarity_results,
        "rejected_duplicates": result.rejected_duplicates,
        "retries_used": result.retries_used,
        "latency_ms": latency_ms,
    }


class HybridRequest(BaseModel):
    extract: ExtractRequest
    generate: GenerateRequest


@router.post("/hybrid")
async def hybrid_items(
    domain_id: str,
    req: HybridRequest,
    user: UserContext = Depends(get_user_context),
    service=Depends(get_assessment_hybrid_service),
    logger=Depends(get_assessment_logger),
):
    """ADR-014 §3 Mode 3 — extract + generate."""
    await _tenant_guard(domain_id, user)
    from rag_core.interfaces.assessment_item_repository import ExtractCriteria
    from rag_core.services.assessment_generate import GenerateCriteria
    from rag_core.services.assessment_logger import AssessmentLogPayload

    request_id = uuid.uuid4().hex
    start = time.perf_counter()
    result = await service.run(
        domain_id=domain_id,
        extract_criteria=ExtractCriteria(
            subject=req.extract.subject,
            chapter=req.extract.chapter,
            difficulty=req.extract.difficulty,
            difficulty_distribution=req.extract.difficulty_distribution,
            quality_status=req.extract.quality_status,
            exclude_recent_days=req.extract.exclude_recent_days,
            tags_any=req.extract.tags_any,
        ),
        generate_criteria=GenerateCriteria(
            subject=req.generate.subject,
            chapter=req.generate.chapter,
            difficulty=req.generate.difficulty,
            count=req.generate.count,
            question_type=req.generate.question_type,
        ),
        actor=user.user_id,
    )
    latency_ms = int((time.perf_counter() - start) * 1000)
    await logger.write(
        AssessmentLogPayload(
            domain_id=domain_id,
            request_id=request_id,
            action="hybrid",
            actor=user.user_id,
            criteria={"extract": req.extract.model_dump(exclude_none=True),
                       "generate": req.generate.model_dump(exclude_none=True)},
            result_summary={
                "extracted_count": result.extracted_count,
                "generated_count": result.generated_count,
                "rejected_duplicates": result.rejected_duplicates,
                "insufficient_pool": result.insufficient_pool,
            },
            validator_summary=result.validator_summary,
            similarity_results=result.similarity_results,
            latency_ms=latency_ms,
        )
    )
    return {
        "request_id": request_id,
        "domain_id": domain_id,
        "mode": "hybrid",
        "items": [_item_to_dict(r) for r in result.items],
        "extracted_count": result.extracted_count,
        "generated_count": result.generated_count,
        "citations": result.citations,
        "insufficient_pool": result.insufficient_pool,
        "rejected_duplicates": result.rejected_duplicates,
        "validator_summary": result.validator_summary,
        "similarity_results": result.similarity_results,
        "latency_ms": latency_ms,
    }


# --------------------------------------------------------------------------- #
# Admin endpoints
# --------------------------------------------------------------------------- #


class ItemUpsertRequest(BaseModel):
    item_id: str | None = None
    subject: str
    chapter: str | None = None
    difficulty: str = "medium"
    question_type: str = "multiple_choice"
    question_text: str = Field(..., min_length=1)
    choices: list[Any] = Field(default_factory=list)
    answer: str = Field(..., min_length=1)
    explanation: str | None = None
    tags: list[str] = Field(default_factory=list)
    assets: list[dict[str, Any]] = Field(default_factory=list)  # ADR-025 이미지 자산
    figure_dependent: bool = False
    quality_status: Literal["draft", "reviewed", "approved", "retired"] = "draft"


@admin_router.get("/items")
async def list_items(
    domain_id: str,
    keyword: str | None = Query(None),
    subject: str | None = Query(None),
    chapter: str | None = Query(None),
    difficulty: str | None = Query(None),
    quality_status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(require_admin),
    repo=Depends(get_assessment_item_repository),
):
    await _tenant_guard(domain_id, user)
    items, total = await repo.list_by_tenant(
        domain_id=domain_id, keyword=keyword,
        subject=subject, chapter=chapter, difficulty=difficulty,
        quality_status=quality_status,
        page=page, page_size=page_size,
    )
    return {
        "items": [_item_to_dict(r) for r in items],
        "total": total, "page": page, "page_size": page_size,
    }


@admin_router.post("/items", status_code=201)
async def create_item(
    domain_id: str,
    req: ItemUpsertRequest,
    user: UserContext = Depends(require_admin),
    repo=Depends(get_assessment_item_repository),
):
    await _tenant_guard(domain_id, user)
    from rag_core.interfaces.assessment_item_repository import (
        AssessmentItemConflictError,
        AssessmentItemRecord,
    )

    item_id = req.item_id or f"Q-{uuid.uuid4().hex[:12]}"
    record = AssessmentItemRecord(
        item_id=item_id,
        domain_id=domain_id,
        subject=req.subject, chapter=req.chapter,
        difficulty=req.difficulty, question_type=req.question_type,
        question_text=req.question_text, choices=req.choices,
        answer=req.answer, explanation=req.explanation,
        tags=req.tags, quality_status=req.quality_status,
        assets=req.assets, figure_dependent=req.figure_dependent,
        source="imported",
    )
    try:
        # 명시 item_id 충돌은 409로 변환
        existing = await repo.get(domain_id=domain_id, item_id=item_id)
        if existing is not None and req.item_id is not None:
            raise HTTPException(
                status_code=409,
                detail={"error": "item_id_exists", "item_id": item_id},
            )
        created = await repo.upsert(record)
    except AssessmentItemConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "item_id_exists", "item_id": exc.item_id},
        ) from exc
    return _item_to_dict(created)


@admin_router.get("/items/{item_id}")
async def get_item(
    domain_id: str,
    item_id: str,
    user: UserContext = Depends(require_admin),
    repo=Depends(get_assessment_item_repository),
):
    await _tenant_guard(domain_id, user)
    record = await repo.get(domain_id=domain_id, item_id=item_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail={"error": "item_not_found", "item_id": item_id},
        )
    return _item_to_dict(record)


class ItemPatchRequest(BaseModel):
    subject: str | None = None
    chapter: str | None = None
    difficulty: str | None = None
    question_text: str | None = None
    choices: list[Any] | None = None
    answer: str | None = None
    explanation: str | None = None
    tags: list[str] | None = None
    assets: list[dict[str, Any]] | None = None
    figure_dependent: bool | None = None
    quality_status: (
        Literal["draft", "reviewed", "approved", "retired"] | None
    ) = None


@admin_router.patch("/items/{item_id}")
async def patch_item(
    domain_id: str,
    item_id: str,
    req: ItemPatchRequest,
    user: UserContext = Depends(require_admin),
    repo=Depends(get_assessment_item_repository),
):
    await _tenant_guard(domain_id, user)
    existing = await repo.get(domain_id=domain_id, item_id=item_id)
    if existing is None:
        raise HTTPException(
            status_code=404, detail={"error": "item_not_found"},
        )
    patch = req.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=400, detail={"error": "empty_patch"})
    for k, v in patch.items():
        if hasattr(existing, k):
            setattr(existing, k, v)
    updated = await repo.upsert(existing)
    return _item_to_dict(updated)


class ApproveRequest(BaseModel):
    reason: str | None = None


@admin_router.post("/items/{item_id}/approve")
async def approve_item(
    domain_id: str,
    item_id: str,
    req: ApproveRequest,
    user: UserContext = Depends(require_admin),
    repo=Depends(get_assessment_item_repository),
):
    """draft/reviewed → approved. retired·이미 approved는 400."""
    await _tenant_guard(domain_id, user)
    existing = await repo.get(domain_id=domain_id, item_id=item_id)
    if existing is None:
        raise HTTPException(
            status_code=404, detail={"error": "item_not_found"},
        )
    if existing.quality_status not in {"draft", "reviewed"}:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_transition", "from": existing.quality_status},
        )
    updated = await repo.update_quality(
        domain_id=domain_id, item_id=item_id, quality_status="approved",
    )
    return _item_to_dict(updated)


@admin_router.get("/review-queue")
async def review_queue(
    domain_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(require_admin),
    repo=Depends(get_assessment_item_repository),
):
    await _tenant_guard(domain_id, user)
    items, total = await repo.list_review_queue(
        domain_id=domain_id, page=page, page_size=page_size,
    )
    return {
        "items": [_item_to_dict(r) for r in items],
        "total": total, "page": page, "page_size": page_size,
    }


@admin_router.get("/analytics")
async def analytics(
    domain_id: str,
    user: UserContext = Depends(require_admin),
    repo=Depends(get_assessment_item_repository),
):
    await _tenant_guard(domain_id, user)
    summary = await repo.analytics_summary(domain_id=domain_id)
    return {"domain_id": domain_id, **summary}
