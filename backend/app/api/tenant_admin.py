"""
Tenant Admin API — `/api/{tenant_id}/admin/*` (ADR-017 §4~17).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from rag_core.interfaces.chunk_repository import IndexingConflictError
from rag_core.services.indexing_service import ReindexMode

from app.core.auth_adapter import UserContext, get_user_context
from app.core.db import get_tenant_session
from app.core.input_schema import InputSchemaService, InputSchemaValidationError
from app.core.tenant_config_service import TenantConfigService
from app.core.tenant_guard import ensure_tenant_match
from app.deps import (
    get_chat_log_reader,
    get_citation_distribution,
    get_citation_reverify_service,
    get_dashboard_analytics,
    get_document_approval_service,
    get_document_metadata_service,
    get_evaluation_orchestrator,
    get_hard_delete_service,
    get_indexing_orchestrator,
    get_input_schema_service,
    get_ledger_audit_service,
    get_lora_registry,
    get_prompt_studio_service,
    get_schema_editor_service,
    get_tenant_config_override_service,
)
from app.services.document_approval_service import DocumentApprovalService
from app.services.document_metadata_service import DocumentMetadataService
from app.services.evaluation_orchestrator import EvaluationOrchestrator
from app.services.hard_delete_service import HardDeleteService
from app.services.indexing_orchestrator import IndexingOrchestrator
from app.services.routing_config_service import (
    RoutingSchemaError,
    dryrun_decide,
    routing_decision_to_dict,
    validate_routing_yaml,
)
from app.services.tenant_config_service import ConfigKeyRestrictedError

router = APIRouter()


def require_admin(user: UserContext = Depends(get_user_context)) -> UserContext:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail={"error": "insufficient_role"})
    return user


async def _ensure_tenant_match(tenant_id: str, user: UserContext) -> None:
    """URL path tenant_id와 JWT의 tenant_id가 다르면 403 (ADR-008 §2 3중 방어).

    platform_admin은 모든 tenant 접근 가능. mismatch 시 Ledger publish_tenant_mismatch
    (ADR-020 §8) — ledger는 module-level lookup하므로 endpoint 의존 추가 없음.
    """
    from app.core.config import get_settings

    ledger = get_ledger_audit_service(get_settings())
    await ensure_tenant_match(tenant_id, user, ledger=ledger)


# ----------------------------------------------------------------------------
# Documents (ADR-017 §6)
# ----------------------------------------------------------------------------


@router.post("/documents/upload", status_code=202)
async def upload_document(
    tenant_id: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    input_type: str | None = Form(None),
    title: str | None = Form(None),
    version: str = Form("v1"),
    doc_id: str | None = Form(None),
    metadata: str | None = Form(None),
    user: UserContext = Depends(require_admin),
    orchestrator: IndexingOrchestrator = Depends(get_indexing_orchestrator),
    schema_service: InputSchemaService = Depends(get_input_schema_service),
):
    """문서 multipart upload + indexing job 생성 (ADR-017 §6.1).

    Response 202: indexing은 BackgroundTask로 비동기 실행.
    Response 409: 같은 (doc_id, version)에 대한 active 인덱싱 job이 이미 존재 (ADR-012 §10).
    Response 422: input_type schema 검증 실패 (ADR-015).
    """
    await _ensure_tenant_match(tenant_id, user)
    meta_dict = _parse_metadata_json(metadata)
    effective_title = title or (file.filename or "untitled")

    # ADR-015 — input_type이 명시되면 metadata schema 검증.
    if input_type is not None:
        # title도 schema의 common_required에 포함되므로 metadata에 보강 후 검증
        merged_for_validation = dict(meta_dict)
        merged_for_validation.setdefault("title", effective_title)
        try:
            schema_service.validate(
                tenant_id=tenant_id,
                input_type=input_type,
                metadata=merged_for_validation,
            )
        except InputSchemaValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "input_schema_validation_failed",
                    "input_type": input_type,
                    "fields": [
                        {"path": e.path, "code": e.code, "message": e.message}
                        for e in exc.errors
                    ],
                },
            ) from exc

    try:
        prepared = await orchestrator.prepare_upload(
            tenant_id=tenant_id,
            doc_id=doc_id,
            version=version,
            title=effective_title,
            filename=file.filename or "upload.bin",
            stream=file.file,
            input_type=input_type,
            metadata=meta_dict,
        )
    except IndexingConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "indexing_job_active",
                "existing_job_id": exc.existing_job_id,
            },
        ) from exc

    background.add_task(orchestrator.execute, job_id=prepared.job_id)
    return {
        "doc_id": prepared.doc_id,
        "version": prepared.version,
        "job_id": prepared.job_id,
        "status": "pending",
        "object_storage_path": prepared.stored.object_storage_path if prepared.stored else None,
    }


class ReindexRequest(BaseModel):
    mode: Literal["parser_only", "chunk_re_split", "embedding_only", "full"] = "full"


@router.post("/documents/{doc_id}/reindex", status_code=202)
async def reindex_document(
    tenant_id: str,
    doc_id: str,
    req: ReindexRequest,
    background: BackgroundTasks,
    version: str = Query("v1"),
    user: UserContext = Depends(require_admin),
    orchestrator: IndexingOrchestrator = Depends(get_indexing_orchestrator),
):
    """기존 문서 재색인 (ADR-017 §6.6, ADR-012 §11)."""
    await _ensure_tenant_match(tenant_id, user)
    try:
        mode = ReindexMode(req.mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid_reindex_mode", "mode": req.mode}
        ) from exc

    try:
        prepared = await orchestrator.prepare_reindex(
            tenant_id=tenant_id,
            doc_id=doc_id,
            version=version,
            mode=mode,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "document_not_found", "doc_id": doc_id, "version": version},
        ) from exc
    except IndexingConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "indexing_job_active",
                "existing_job_id": exc.existing_job_id,
            },
        ) from exc

    background.add_task(orchestrator.execute, job_id=prepared.job_id)
    return {"job_id": prepared.job_id, "status": "pending", "mode": mode.value}


@router.get("/documents")
async def list_documents(
    tenant_id: str,
    keyword: str | None = Query(None),
    approval_status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: UserContext = Depends(require_admin),
    orchestrator: IndexingOrchestrator = Depends(get_indexing_orchestrator),
):
    """ADR-017 §6.2 — admin documents 목록 페이징. doc_id별 최신 version만 노출."""
    await _ensure_tenant_match(tenant_id, user)
    offset = (page - 1) * page_size
    docs = await orchestrator.service.document_repo.list_by_tenant(
        tenant_id=tenant_id,
        keyword=keyword,
        approval_status=approval_status,
        limit=page_size,
        offset=offset,
    )
    return {
        "items": [_document_to_dict(d) for d in docs],
        "page": page,
        "page_size": page_size,
    }


@router.get("/documents/{doc_id}")
async def get_document(
    tenant_id: str,
    doc_id: str,
    version: str = Query("v1"),
    user: UserContext = Depends(require_admin),
    orchestrator: IndexingOrchestrator = Depends(get_indexing_orchestrator),
):
    """ADR-017 §6.3 — document 메타 + 같은 (doc_id, version) chunks 요약."""
    await _ensure_tenant_match(tenant_id, user)
    doc = await orchestrator.service.document_repo.get(
        tenant_id=tenant_id, doc_id=doc_id, version=version
    )
    if doc is None:
        raise HTTPException(
            status_code=404, detail={"error": "document_not_found"}
        )
    chunks = await orchestrator.service.chunk_repo.list_by_doc(
        tenant_id=tenant_id, doc_id=doc_id, doc_version=version
    )
    return {
        **_document_to_dict(doc),
        "chunks_summary": {
            "total": len(chunks),
            "approval_status_distribution": _distribution(
                [c.approval_status for c in chunks]
            ),
        },
    }


class MetadataPatchRequest(BaseModel):
    """ADR-017 §6.4 — documents 부분 갱신.

    `patch`에 변경할 필드만 명시. 허용 필드: title / input_type / source_type /
    object_storage_path / department / doc_type / security_level / owner / tags /
    language / valid_from / valid_until / metadata.
    """

    version: str = "v1"
    patch: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


@router.patch("/documents/{doc_id}")
async def patch_document_metadata(
    tenant_id: str,
    doc_id: str,
    req: MetadataPatchRequest,
    user: UserContext = Depends(require_admin),
    metadata_service: DocumentMetadataService = Depends(get_document_metadata_service),
):
    """ADR-017 §6.4 — documents 메타데이터 부분 갱신 (payload-only).

    chunks/Qdrant payload는 chunk-syncable 필드(title/department/doc_type/security_level/
    tags/valid_from/valid_until)만 자동 동기화. content/embedding 재생성 없음.
    """
    await _ensure_tenant_match(tenant_id, user)
    if not req.patch:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_patch", "message": "patch must contain at least one field"},
        )
    try:
        result = await metadata_service.update(
            tenant_id=tenant_id,
            doc_id=doc_id,
            version=req.version,
            patch=req.patch,
            actor=user.user_id,
            reason=req.reason,
        )
    except InputSchemaValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "input_schema_validation_failed",
                "fields": [
                    {"path": e.path, "code": e.code, "message": e.message}
                    for e in exc.errors
                ],
            },
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=404, detail={"error": "document_not_found"}
        )
    return {
        "doc_id": doc_id,
        "version": req.version,
        "document": _document_to_dict(result.document),
        "affected_chunks": result.affected_chunks,
        "synced_keys": result.synced_keys,
    }


class ApprovalRequest(BaseModel):
    approval_status: Literal["draft", "approved", "archived"]
    version: str = "v1"
    reason: str | None = None


@router.patch("/documents/{doc_id}/approval")
async def patch_approval(
    tenant_id: str,
    doc_id: str,
    req: ApprovalRequest,
    user: UserContext = Depends(require_admin),
    approval_service: DocumentApprovalService = Depends(get_document_approval_service),
):
    """ADR-012 §3-8 — payload-only 자동 갱신.

    documents.approval_status + chunks.approval_status + Qdrant payload 3개 layer를
    한 transaction-ish 흐름으로 동기화. content/embedding 재생성 없음.
    """
    await _ensure_tenant_match(tenant_id, user)
    result = await approval_service.set_approval(
        tenant_id=tenant_id,
        doc_id=doc_id,
        version=req.version,
        approval_status=req.approval_status,
        actor=user.user_id,
        reason=req.reason,
    )
    if result is None:
        raise HTTPException(
            status_code=404, detail={"error": "document_not_found"}
        )
    return {
        "doc_id": doc_id,
        "version": req.version,
        "approval_status": result.document.approval_status,
        "affected_chunks": result.affected_chunks,
    }


class HardDeleteRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)
    chat_logs_action: Literal["keep_excerpts", "mask_excerpts", "delete_logs"] = (
        "keep_excerpts"
    )
    version: str | None = None  # None = 모든 version


# ----------------------------------------------------------------------------
# Schema Editor (ADR-017 §15 + ADR-015)
# ----------------------------------------------------------------------------


def _schema_record_to_dict(record) -> dict[str, Any]:
    return {
        "schema_id": record.schema_id,
        "tenant_id": record.tenant_id,
        "schema_version": record.schema_version,
        "status": record.status,
        "schema_yaml": record.schema_yaml,
        "ui_schema_yaml": record.ui_schema_yaml,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "deprecated_at": (
            record.deprecated_at.isoformat() if record.deprecated_at else None
        ),
    }


@router.get("/schema")
async def get_schema_active(
    tenant_id: str,
    user: UserContext = Depends(require_admin),
    service=Depends(get_schema_editor_service),
):
    """ADR-017 §15 — 현재 active schema. None이면 404 schema_not_initialized."""
    await _ensure_tenant_match(tenant_id, user)
    record = await service.get_active(tenant_id=tenant_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "schema_not_initialized"},
        )
    return _schema_record_to_dict(record)


class SchemaPutRequest(BaseModel):
    schema_yaml: dict[str, Any] = Field(..., description="전체 input_schema yaml dict")
    ui_schema_yaml: dict[str, Any] | None = None
    base_version: int | None = Field(
        None,
        description="현재 active schema_version (optimistic lock token). 초기 등록은 null",
    )


@router.put("/schema")
async def put_schema(
    tenant_id: str,
    req: SchemaPutRequest,
    user: UserContext = Depends(require_admin),
    service=Depends(get_schema_editor_service),
):
    """ADR-017 §15 + Y8 — 전체 schema yaml 교체.

    optimistic lock: req.base_version이 현재 active.schema_version과 다르면 409.
    backward compat: 이전 active의 input_type 제거 시 422 + errors[].
    성공 시 새 schema_version + 기존 active는 deprecated 전이.
    """
    await _ensure_tenant_match(tenant_id, user)
    from rag_core.interfaces.tenant_input_schema_repository import (
        SchemaVersionConflictError,
    )
    from app.services.schema_editor_service import SchemaBackwardCompatError

    try:
        result = await service.put(
            tenant_id=tenant_id,
            base_version=req.base_version,
            schema_yaml=req.schema_yaml,
            ui_schema_yaml=req.ui_schema_yaml,
        )
    except SchemaBackwardCompatError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "schema_invalid", "errors": exc.errors},
        ) from exc
    except SchemaVersionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "schema_version_conflict",
                "current": exc.current,
                "base": exc.base,
            },
        ) from exc

    return {
        **_schema_record_to_dict(result.record),
        "deprecated_version": result.deprecated_version,
    }


@router.get("/schema/history")
async def get_schema_history(
    tenant_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_admin),
    service=Depends(get_schema_editor_service),
):
    """ADR-017 §15 — schema_version 역순 페이징."""
    await _ensure_tenant_match(tenant_id, user)
    items = await service.list_history(
        tenant_id=tenant_id, limit=page_size, offset=(page - 1) * page_size,
    )
    return {
        "tenant_id": tenant_id,
        "items": [_schema_record_to_dict(r) for r in items],
        "page": page,
        "page_size": page_size,
    }


@router.get("/input_schemas")
async def list_input_schemas(
    tenant_id: str,
    user: UserContext = Depends(require_admin),
    schema_service: InputSchemaService = Depends(get_input_schema_service),
):
    """ADR-015 §3 — Frontend react-jsonschema-form 입력. input_types 목록 + 합성 schema."""
    await _ensure_tenant_match(tenant_id, user)
    types = schema_service.list_input_types(tenant_id)
    return {
        "tenant_id": tenant_id,
        "input_types": [
            {
                "name": t.name,
                "display_name": t.display_name,
                "schema": t.to_json_schema(),
            }
            for t in types
        ],
    }


@router.delete("/documents/{doc_id}/hard")
async def hard_delete(
    tenant_id: str,
    doc_id: str,
    req: HardDeleteRequest,
    user: UserContext = Depends(require_admin),
    service: HardDeleteService = Depends(get_hard_delete_service),
):
    """ADR-007/012 — Hard delete cross-system 일관성 (Postgres+Qdrant+MinIO+chat_logs).

    Response: removed_chunks / removed_storage_files / affected_chat_logs /
    chat_logs_action_applied / dead_letters(부분 실패 누적). 동일 doc_id가 없어도
    200 + 0건 (idempotent).
    """
    await _ensure_tenant_match(tenant_id, user)
    result = await service.execute(
        tenant_id=tenant_id,
        doc_id=doc_id,
        version=req.version,
        actor=user.user_id,
        reason=req.reason,
        chat_logs_action=req.chat_logs_action,
    )
    return {
        "tenant_id": result.tenant_id,
        "doc_id": result.doc_id,
        "version": result.version,
        "removed_chunks": result.removed_chunks,
        "removed_storage_files": result.removed_storage_files,
        "removed_documents": result.removed_documents,
        "affected_chat_logs": result.affected_chat_logs,
        "chat_logs_action_applied": result.chat_logs_action_applied,
        "dead_letters": result.dead_letters,
    }


# ----------------------------------------------------------------------------
# Indexing Jobs (ADR-017 §7)
# ----------------------------------------------------------------------------


@router.get("/indexing/jobs")
async def list_indexing_jobs(
    tenant_id: str,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_admin),
    orchestrator: IndexingOrchestrator = Depends(get_indexing_orchestrator),
):
    """ADR-017 §7 — Tenant Admin 인덱싱 모니터링 페이징 조회."""
    await _ensure_tenant_match(tenant_id, user)
    offset = (page - 1) * page_size
    jobs = await orchestrator.service.job_repo.list_by_tenant(
        tenant_id=tenant_id,
        status=status,
        limit=page_size,
        offset=offset,
    )
    return {
        "items": [_job_to_dict(j) for j in jobs],
        "page": page,
        "page_size": page_size,
    }


@router.get("/indexing/jobs/{job_id}")
async def get_indexing_job(
    tenant_id: str,
    job_id: str,
    user: UserContext = Depends(require_admin),
    orchestrator: IndexingOrchestrator = Depends(get_indexing_orchestrator),
):
    """ADR-017 §7 — 단일 job 상세 (failed_chunks JSONB 포함)."""
    await _ensure_tenant_match(tenant_id, user)
    job = await orchestrator.service.job_repo.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "job_not_found"})
    if job.tenant_id != tenant_id:
        # path-mirror 검증 — RLS context 누락 시에도 안전.
        raise HTTPException(status_code=404, detail={"error": "job_not_found"})
    return _job_to_dict(job)


@router.post("/indexing/jobs/{job_id}/retry", status_code=202)
async def retry_indexing_job(
    tenant_id: str,
    job_id: str,
    background: BackgroundTasks,
    user: UserContext = Depends(require_admin),
    orchestrator: IndexingOrchestrator = Depends(get_indexing_orchestrator),
):
    """ADR-017 §7 — failed/partial job in-place 재실행.

    status가 failed/partial이 아니면 400 invalid_status. job·document 미발견은 404.
    202 + job_id 반환. 실제 indexing은 BackgroundTask로 수행.
    """
    await _ensure_tenant_match(tenant_id, user)
    try:
        prepared = await orchestrator.retry_job(
            tenant_id=tenant_id, job_id=job_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"error": "job_or_document_not_found"},
        ) from exc
    except ValueError as exc:
        if str(exc) == "invalid_status":
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_status_for_retry"},
            ) from exc
        raise
    background.add_task(orchestrator.execute, job_id=prepared.job_id)
    return {
        "job_id": prepared.job_id,
        "doc_id": prepared.doc_id,
        "version": prepared.version,
        "status": "pending",
    }


# ----------------------------------------------------------------------------
# Evaluation (ADR-009 §7 + ADR-017 §16)
# ----------------------------------------------------------------------------


class EvaluationRunRequest(BaseModel):
    dataset_name: str
    config_override: dict[str, Any] | None = None


class EvaluationPromoteRequest(BaseModel):
    target: Literal["model", "prompt", "lora", "routing"]
    version: str


@router.get("/evaluation/datasets")
async def list_evaluation_datasets(
    tenant_id: str,
    user: UserContext = Depends(require_admin),
    orchestrator: EvaluationOrchestrator = Depends(get_evaluation_orchestrator),
):
    """ADR-017 §16 — 평가셋 목록."""
    await _ensure_tenant_match(tenant_id, user)
    return {"datasets": orchestrator.list_datasets(tenant_id)}


@router.post("/evaluation/run", status_code=202)
async def run_evaluation(
    tenant_id: str,
    req: EvaluationRunRequest,
    background: BackgroundTasks,
    user: UserContext = Depends(require_admin),
    orchestrator: EvaluationOrchestrator = Depends(get_evaluation_orchestrator),
):
    """ADR-017 §16 — 평가 실행 트리거. 202 + job_id 반환, background에서 실제 실행."""
    await _ensure_tenant_match(tenant_id, user)
    try:
        prepared = await orchestrator.prepare_run(
            tenant_id=tenant_id,
            dataset_name=req.dataset_name,
            actor=user.user_id,
            config_override=req.config_override or {},
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "dataset_not_found", "dataset_name": req.dataset_name},
        ) from exc

    background.add_task(
        orchestrator.execute, job_id=prepared.job_id, tenant_id=tenant_id
    )
    return {
        "job_id": prepared.job_id,
        "tenant_id": tenant_id,
        "dataset_name": prepared.dataset_name,
        "status": "pending",
    }


@router.get("/evaluation/jobs/{job_id}")
async def get_evaluation_job(
    tenant_id: str,
    job_id: str,
    user: UserContext = Depends(require_admin),
    orchestrator: EvaluationOrchestrator = Depends(get_evaluation_orchestrator),
):
    """ADR-017 §16 — 단일 evaluation job 진행/결과 조회."""
    await _ensure_tenant_match(tenant_id, user)
    record = await orchestrator.repo.get(tenant_id=tenant_id, job_id=job_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail={"error": "evaluation_job_not_found"}
        )
    return _evaluation_to_dict(record)


@router.get("/evaluation/jobs")
async def list_evaluation_jobs(
    tenant_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_admin),
    orchestrator: EvaluationOrchestrator = Depends(get_evaluation_orchestrator),
):
    """ADR-017 §16 보강 — 페이징 목록."""
    await _ensure_tenant_match(tenant_id, user)
    offset = (page - 1) * page_size
    items = await orchestrator.repo.list_by_tenant(
        tenant_id=tenant_id, limit=page_size, offset=offset
    )
    return {
        "items": [_evaluation_to_dict(j) for j in items],
        "page": page,
        "page_size": page_size,
    }


@router.post("/evaluation/jobs/{job_id}/promote")
async def promote_evaluation_job(
    tenant_id: str,
    job_id: str,
    req: EvaluationPromoteRequest,
    user: UserContext = Depends(require_admin),
    orchestrator: EvaluationOrchestrator = Depends(get_evaluation_orchestrator),
):
    """ADR-017 §16 — promotion_gate 통과한 job을 promoted 상태로 전이.

    실제 모델/prompt 승격(prompt_registry/model_registry 갱신)은 별도 ADR에서 정의되며,
    본 endpoint는 audit row(status='promoted' + promoted_by/target/version) 기록만 담당한다.
    """
    await _ensure_tenant_match(tenant_id, user)
    try:
        record = await orchestrator.promote(
            tenant_id=tenant_id,
            job_id=job_id,
            actor=user.user_id,
            target=req.target,
            version=req.version,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"error": "evaluation_job_not_found"}
        ) from exc
    except ValueError as exc:
        msg = str(exc)
        if msg == "promotion_gate_not_passed":
            raise HTTPException(
                status_code=409, detail={"error": "promotion_gate_not_passed"}
            ) from exc
        raise HTTPException(
            status_code=409,
            detail={"error": "invalid_state_for_promote", "reason": msg},
        ) from exc

    return _evaluation_to_dict(record)


def _evaluation_to_dict(job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "tenant_id": job.tenant_id,
        "dataset_name": job.dataset_name,
        "status": job.status,
        "actor": job.actor,
        "config_override": dict(job.config_override or {}),
        "summary": dict(job.summary or {}),
        "gate_result": dict(job.gate_result or {}),
        "error_message": job.error_message,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "promoted_at": job.promoted_at.isoformat() if job.promoted_at else None,
        "promoted_by": job.promoted_by,
        "promotion_target": job.promotion_target,
        "promotion_version": job.promotion_version,
    }


# ----------------------------------------------------------------------------
# Tenant Configs (ADR-009 §3·§8 + ADR-017 §11)
# ----------------------------------------------------------------------------


_CONFIG_CATEGORIES = (
    "citation", "retrieval", "model", "routing", "query_classifier",
    "lifecycle", "auth", "pii", "audit", "data_retention",
)


@router.get("/configs")
async def get_all_configs(
    tenant_id: str,
    user: UserContext = Depends(require_admin),
):
    """ADR-017 §11 — defaults + overrides 합성된 효과적 config 전체."""
    await _ensure_tenant_match(tenant_id, user)
    cfg = TenantConfigService.load(tenant_id)
    return {
        "tenant_id": tenant_id,
        "categories": {
            cat: getattr(cfg, cat) for cat in _CONFIG_CATEGORIES
        },
        "compliance_mode": cfg.compliance_mode,
    }


@router.get("/configs/{category}")
async def get_config_category(
    tenant_id: str,
    category: str,
    user: UserContext = Depends(require_admin),
):
    """ADR-017 §11 — 카테고리별 효과적 config (defaults + overrides 합성)."""
    await _ensure_tenant_match(tenant_id, user)
    if category not in _CONFIG_CATEGORIES:
        raise HTTPException(
            status_code=404, detail={"error": "unknown_category", "category": category}
        )
    cfg = TenantConfigService.load(tenant_id)
    return {
        "tenant_id": tenant_id,
        "category": category,
        "value": getattr(cfg, category),
    }


class ConfigPatchRequest(BaseModel):
    key: str = Field(..., min_length=1)
    value: Any
    reason: str | None = None


@router.patch("/configs/{category}")
async def patch_config_category(
    tenant_id: str,
    category: str,
    req: ConfigPatchRequest,
    user: UserContext = Depends(require_admin),
    override_service=Depends(get_tenant_config_override_service),
):
    """ADR-017 §11 + ADR-009 §8 — DB override 갱신.

    restricted_to platform_admin 키를 일반 admin이 patch 시도하면 403.
    breaking 키 변경 시 Ledger publish_config_change_breaking 자동 호출.
    """
    await _ensure_tenant_match(tenant_id, user)
    if category not in _CONFIG_CATEGORIES:
        raise HTTPException(
            status_code=404, detail={"error": "unknown_category", "category": category}
        )

    try:
        record = await override_service.patch(
            tenant_id=tenant_id, category=category, key=req.key, value=req.value,
            actor=user.user_id, is_platform_admin=user.is_platform_admin,
            reason=req.reason,
        )
    except ConfigKeyRestrictedError as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "config_key_restricted",
                "key": exc.full_key,
                "required_role": "PLATFORM_ADMIN",
            },
        ) from exc

    return {
        "tenant_id": record.tenant_id,
        "category": record.category,
        "key": record.key,
        "old_value": record.old_value,
        "new_value": record.new_value,
        "changed_by": record.changed_by,
        "changed_at": record.changed_at.isoformat() if record.changed_at else None,
        "reason": record.reason,
    }


@router.post("/configs/reload", status_code=204)
async def reload_configs(
    tenant_id: str,
    user: UserContext = Depends(require_admin),
    override_service=Depends(get_tenant_config_override_service),
):
    """ADR-017 §11 — 수동 cache invalidate. LISTEN/NOTIFY 무관하게 즉시 반영 강제."""
    await _ensure_tenant_match(tenant_id, user)
    await override_service.reload(tenant_id=tenant_id)
    return None


@router.get("/configs/{category}/history")
async def get_config_history(
    tenant_id: str,
    category: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_admin),
    override_service=Depends(get_tenant_config_override_service),
):
    """ADR-017 §11 — tenant_config_change_logs 페이징 조회."""
    await _ensure_tenant_match(tenant_id, user)
    if category not in _CONFIG_CATEGORIES:
        raise HTTPException(
            status_code=404, detail={"error": "unknown_category", "category": category}
        )
    items = await override_service.list_history(
        tenant_id=tenant_id, category=category,
        limit=page_size, offset=(page - 1) * page_size,
    )
    return {"items": items, "page": page, "page_size": page_size}


# ----------------------------------------------------------------------------
# Chat Logs (ADR-017 §8 + ADR-019 §2)
# ----------------------------------------------------------------------------


@router.get("/logs/chat")
async def list_chat_logs(
    tenant_id: str,
    user_id: str | None = Query(None),
    conversation_id: str | None = Query(None),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    fallback_only: bool = Query(False),
    ui_mode: Literal["chat_structured", "chat_streaming"] | None = Query(None),
    citation_type: Literal["direct", "synthesis", "inference", "conflict"] | None = (
        Query(None)
    ),
    min_confidence: float | None = Query(None, ge=0.0, le=1.0),
    max_confidence: float | None = Query(None, ge=0.0, le=1.0),
    keyword: str | None = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(require_admin),
    reader=Depends(get_chat_log_reader),
):
    """ADR-017 §8 — chat_logs 페이징 조회. tenant_id로 RLS 자동 격리.

    필터:
      - user_id / conversation_id: 정확 일치
      - from_date / to_date: created_at 범위 (partitioning pruning)
      - fallback_only: fallback_reason IS NOT NULL
      - ui_mode: chat_structured | chat_streaming
      - citation_type: direct | synthesis | inference | conflict (citation_types 배열에 포함)
      - min/max_confidence: 0.0~1.0
      - keyword: question / rewritten_query / answer ILIKE 부분 일치
    """
    await _ensure_tenant_match(tenant_id, user)

    from rag_core.interfaces.chat_log_reader import ChatLogListFilters

    filters = ChatLogListFilters(
        user_id=user_id,
        conversation_id=conversation_id,
        from_date=from_date,
        to_date=to_date,
        fallback_only=fallback_only,
        ui_mode=ui_mode,
        citation_type=citation_type,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        keyword=keyword,
    )
    result = await reader.list_by_tenant(
        tenant_id=tenant_id, filters=filters, page=page, page_size=page_size
    )
    return {
        "items": [_chat_log_to_dict(r) for r in result.items],
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
    }


@router.get("/logs/chat/{request_id}")
async def get_chat_log(
    tenant_id: str,
    request_id: str,
    user: UserContext = Depends(require_admin),
    reader=Depends(get_chat_log_reader),
):
    """ADR-017 §8 — 단건 상세 (모든 chat_logs 컬럼)."""
    await _ensure_tenant_match(tenant_id, user)
    record = await reader.get(tenant_id=tenant_id, request_id=request_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "chat_log_not_found", "request_id": request_id},
        )
    return _chat_log_to_dict(record)


def _chat_log_to_dict(record) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "request_id": record.request_id,
        "user_id": record.user_id,
        "conversation_id": record.conversation_id,
        "question": record.question,
        "rewritten_query": record.rewritten_query,
        "answer": record.answer,
        "retrieved_chunks": record.retrieved_chunks,
        "citations": record.citations,
        "citation_types": record.citation_types,
        "verifier_metrics": record.verifier_metrics,
        "routing_decision": record.routing_decision,
        "classifier_decision": record.classifier_decision,
        "model_failure_chain": record.model_failure_chain,
        "inference_judge_results": record.inference_judge_results,
        "conflict_groups": record.conflict_groups,
        "input_pii_found": record.input_pii_found,
        "output_pii_masked": record.output_pii_masked,
        "pii_storage_policy": record.pii_storage_policy,
        "llm_model": record.llm_model,
        "embedding_model": record.embedding_model,
        "reranker_model": record.reranker_model,
        "prompt_version": record.prompt_version,
        "latency_ms": record.latency_ms,
        "ui_mode": record.ui_mode,
        "confidence": record.confidence,
        "fallback_reason": record.fallback_reason,
        "unsupported_ratio": record.unsupported_ratio,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


# ----------------------------------------------------------------------------
# LoRA Registry (ADR-017 §14 + ADR-013)
# ----------------------------------------------------------------------------


def _adapter_to_dict(rec) -> dict[str, Any]:
    return {
        "adapter_id": rec.adapter_id,
        "tenant_id": rec.tenant_id,
        "version": rec.version,
        "base_model": rec.base_model,
        "keyhub_secret_ref": rec.keyhub_secret_ref,
        "status": rec.status,
        "training_metadata": rec.training_metadata,
        "registered_at": rec.registered_at.isoformat() if rec.registered_at else None,
        "activated_at": rec.activated_at.isoformat() if rec.activated_at else None,
        "retired_at": rec.retired_at.isoformat() if rec.retired_at else None,
    }


@router.get("/lora")
async def list_lora_adapters(
    tenant_id: str,
    status: Literal["registered", "active", "retired"] | None = Query(None),
    user: UserContext = Depends(require_admin),
    registry=Depends(get_lora_registry),
):
    """ADR-017 §14 — tenant scope LoRA adapter 목록 (status 필터)."""
    await _ensure_tenant_match(tenant_id, user)
    items = await registry.list_by_tenant(tenant_id=tenant_id, status=status)
    return {
        "tenant_id": tenant_id,
        "items": [_adapter_to_dict(r) for r in items],
        "total": len(items),
    }


@router.post("/lora/upload", status_code=201)
async def upload_lora_adapter(
    tenant_id: str,
    weights: UploadFile = File(...),
    metadata: str = Form(...),
    user: UserContext = Depends(require_admin),
    registry=Depends(get_lora_registry),
):
    """ADR-017 §14 — multipart adapter weights + metadata JSON.

    metadata: `{adapter_id, version, base_model, training_metadata?, keyhub_secret_ref?}`.
    실제 weights 저장은 KeyHub 통합 작업(ADR-019)이 별도 — 본 endpoint는 메타데이터만
    adapter_registry에 INSERT한다. weights 바이트는 size 검증 후 폐기(또는 KeyHub upload
    호출이 추후 추가).
    """
    await _ensure_tenant_match(tenant_id, user)

    meta = _parse_metadata_json(metadata)
    adapter_id = meta.get("adapter_id")
    if not adapter_id or not isinstance(adapter_id, str):
        raise HTTPException(
            status_code=422,
            detail={"error": "adapter_id_missing"},
        )

    # weights는 size 정도만 보관 (KeyHub 통합 전까지 placeholder)
    blob = await weights.read()
    training_metadata = dict(meta.get("training_metadata") or {})
    training_metadata.setdefault("weights_size_bytes", len(blob))
    training_metadata.setdefault("filename", weights.filename)

    from rag_core.interfaces.lora_registry import (
        AdapterRecord,
        LoRAConflictError,
    )

    record = AdapterRecord(
        adapter_id=adapter_id,
        tenant_id=tenant_id,
        version=meta.get("version"),
        base_model=meta.get("base_model"),
        keyhub_secret_ref=meta.get("keyhub_secret_ref"),
        training_metadata=training_metadata,
    )
    try:
        created = await registry.upload(record)
    except LoRAConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "adapter_id_exists", "adapter_id": adapter_id},
        ) from exc

    return _adapter_to_dict(created)


@router.post("/lora/{adapter_id}/activate")
async def activate_lora_adapter(
    tenant_id: str,
    adapter_id: str,
    user: UserContext = Depends(require_admin),
    registry=Depends(get_lora_registry),
):
    """ADR-017 §14 — registered → active. retired→active는 400."""
    await _ensure_tenant_match(tenant_id, user)
    from rag_core.interfaces.lora_registry import (
        LoRAInvalidTransitionError,
        LoRANotFoundError,
    )

    try:
        rec = await registry.activate(tenant_id=tenant_id, adapter_id=adapter_id)
    except LoRANotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "adapter_not_found", "adapter_id": adapter_id},
        ) from exc
    except LoRAInvalidTransitionError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_transition",
                "from": exc.current, "to": exc.target,
            },
        ) from exc
    return _adapter_to_dict(rec)


@router.post("/lora/{adapter_id}/retire")
async def retire_lora_adapter(
    tenant_id: str,
    adapter_id: str,
    user: UserContext = Depends(require_admin),
    registry=Depends(get_lora_registry),
):
    """ADR-017 §14 — active|registered → retired. idempotent."""
    await _ensure_tenant_match(tenant_id, user)
    from rag_core.interfaces.lora_registry import LoRANotFoundError

    try:
        rec = await registry.retire(tenant_id=tenant_id, adapter_id=adapter_id)
    except LoRANotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "adapter_not_found", "adapter_id": adapter_id},
        ) from exc
    return _adapter_to_dict(rec)


@router.delete("/lora/{adapter_id}", status_code=204)
async def delete_lora_adapter(
    tenant_id: str,
    adapter_id: str,
    user: UserContext = Depends(require_admin),
    registry=Depends(get_lora_registry),
):
    """ADR-017 §14 — adapter row 삭제. active 상태면 409 (retire 후 삭제 필요)."""
    await _ensure_tenant_match(tenant_id, user)
    from rag_core.interfaces.lora_registry import LoRADeleteForbiddenError

    try:
        affected = await registry.delete(tenant_id=tenant_id, adapter_id=adapter_id)
    except LoRADeleteForbiddenError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "adapter_active_cannot_delete",
                "adapter_id": adapter_id,
            },
        ) from exc
    if affected == 0:
        raise HTTPException(
            status_code=404,
            detail={"error": "adapter_not_found", "adapter_id": adapter_id},
        )
    return None


# ----------------------------------------------------------------------------
# Prompt Studio (ADR-017 §12)
# ----------------------------------------------------------------------------


def _prompt_to_dict(record) -> dict[str, Any]:
    return {
        "task": record.task,
        "version": record.version,
        "ab_slot": record.ab_slot,
        "system": record.system,
        "user": record.user,
        "schema_version": record.schema_version,
        "response_schema_path": record.response_schema_path,
        "source": record.source,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "updated_by": record.updated_by,
        "reason": record.reason,
    }


@router.get("/prompts")
async def list_prompts(
    tenant_id: str,
    user: UserContext = Depends(require_admin),
    service=Depends(get_prompt_studio_service),
):
    """ADR-017 §12 — 모든 task의 effective prompt 목록(task별 정렬)."""
    await _ensure_tenant_match(tenant_id, user)
    items = service.list_tasks(tenant_id)
    return {
        "tenant_id": tenant_id,
        "items": [_prompt_to_dict(r) for r in items],
        "total": len(items),
    }


@router.get("/prompts/{task}")
async def get_prompt(
    tenant_id: str,
    task: str,
    version: str | None = Query(None),
    ab_slot: str | None = Query(None),
    user: UserContext = Depends(require_admin),
    service=Depends(get_prompt_studio_service),
):
    """ADR-017 §12 — 특정 task의 effective prompt. version/ab_slot 미지정 시 첫 항목."""
    await _ensure_tenant_match(tenant_id, user)
    record = service.get_prompt(tenant_id, task, version, ab_slot)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "prompt_not_found", "task": task},
        )
    return _prompt_to_dict(record)


class PromptPatchRequest(BaseModel):
    system: str | None = None
    user: str | None = None
    reason: str | None = None


@router.patch("/prompts/{task}/{version}/{ab_slot}")
async def patch_prompt(
    tenant_id: str,
    task: str,
    version: str,
    ab_slot: str,
    req: PromptPatchRequest,
    user: UserContext = Depends(require_admin),
    service=Depends(get_prompt_studio_service),
):
    """ADR-017 §12 — system/user 템플릿 갱신. 적어도 한쪽은 필수.

    GenerationService 재구성은 follow-up — 본 endpoint는 PromptStudioService runtime
    override만 갱신한다.
    """
    await _ensure_tenant_match(tenant_id, user)
    try:
        change = service.patch(
            tenant_id=tenant_id,
            task=task, version=version, ab_slot=ab_slot,
            system=req.system, user=req.user,
            actor=user.user_id, reason=req.reason,
        )
    except ValueError as exc:
        if str(exc) == "empty_patch":
            raise HTTPException(
                status_code=400,
                detail={"error": "empty_patch"},
            ) from exc
        raise
    return {
        "tenant_id": change.tenant_id,
        "task": change.task,
        "version": change.version,
        "ab_slot": change.ab_slot,
        "new": _prompt_to_dict(change.new),
        "changed_at": change.changed_at.isoformat(),
        "changed_by": change.changed_by,
        "reason": change.reason,
    }


class PromptPreviewRequest(BaseModel):
    system: str | None = None
    user: str | None = None
    sample_question: str = Field(..., min_length=1, max_length=2000)
    sample_contexts: list[dict[str, Any]] | None = None
    invoke_llm: bool = False


@router.post("/prompts/{task}/preview")
async def preview_prompt(
    tenant_id: str,
    task: str,
    req: PromptPreviewRequest,
    user: UserContext = Depends(require_admin),
    service=Depends(get_prompt_studio_service),
):
    """ADR-017 §12 — Jinja2 렌더 + 선택적 LLM 호출.

    system/user 미지정 시 현재 effective 사용. invoke_llm=False면 sample_answer는
    null (랜더 결과만 검토).
    """
    await _ensure_tenant_match(tenant_id, user)
    result = await service.preview(
        tenant_id=tenant_id,
        task=task,
        system=req.system, user=req.user,
        sample_question=req.sample_question,
        sample_contexts=req.sample_contexts,
        invoke_llm=req.invoke_llm,
    )
    if result.get("render_error"):
        raise HTTPException(
            status_code=422,
            detail={"error": "prompt_render_failed", "reason": result["render_error"]},
        )
    return {
        "tenant_id": tenant_id,
        "task": task,
        "rendered_system": result["rendered_system"],
        "rendered_user": result["rendered_user"],
        "sample_answer": result["sample_answer"],
    }


# ----------------------------------------------------------------------------
# Routing Rules (ADR-017 §13 + ADR-013)
# ----------------------------------------------------------------------------


@router.get("/routing")
async def get_routing(
    tenant_id: str,
    user: UserContext = Depends(require_admin),
):
    """ADR-017 §13 — 현재 effective routing.yaml dict.

    platform routing.yaml + tenant overrides.yaml + runtime override(ADR-009 §5
    DB→load 영구화 대기 동안) 합성 결과.
    """
    await _ensure_tenant_match(tenant_id, user)
    cfg = TenantConfigService.load(tenant_id)
    return {"tenant_id": tenant_id, "routing": cfg.routing or {}}


class RoutingPutRequest(BaseModel):
    value: dict[str, Any] = Field(..., description="전체 routing yaml dict")
    reason: str | None = None


@router.put("/routing")
async def put_routing(
    tenant_id: str,
    req: RoutingPutRequest,
    user: UserContext = Depends(require_admin),
    override_service=Depends(get_tenant_config_override_service),
):
    """ADR-017 §13 — routing yaml 전체 교체 (schema 검증 + DB persist + runtime 적용).

    검증 실패 시 422 routing_schema_invalid + errors[]. 성공 시 200 + effective dict.
    """
    await _ensure_tenant_match(tenant_id, user)
    try:
        validate_routing_yaml(req.value)
    except RoutingSchemaError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "routing_schema_invalid", "errors": exc.errors},
        ) from exc

    # DB 영구화 (path="" → 전체 카테고리 단일 override)
    await override_service.patch(
        tenant_id=tenant_id, category="routing", key="",
        value=req.value,
        actor=user.user_id, is_platform_admin=user.is_platform_admin,
        reason=req.reason,
    )
    # 같은 프로세스 즉시 반영 (다음 chat 요청부터 새 routing 사용)
    TenantConfigService.apply_runtime_override(tenant_id, "routing", req.value)

    return {"tenant_id": tenant_id, "routing": req.value}


class RoutingDryRunRequest(BaseModel):
    classifier_decision: dict[str, Any] = Field(
        ..., description="ClassificationResult dict (query_type/support_type/complexity)"
    )
    sample_query: str | None = None
    routing_config: dict[str, Any] | None = Field(
        None, description="None이면 현재 effective routing 사용"
    )
    retrieval_confidence: float | None = Field(None, ge=0.0, le=1.0)


@router.post("/routing/dryrun")
async def dryrun_routing(
    tenant_id: str,
    req: RoutingDryRunRequest,
    user: UserContext = Depends(require_admin),
):
    """ADR-017 §13 — 시나리오 시뮬레이션. ModelRouter.decide() 한 번 호출 결과.

    routing_config 미지정 시 현재 effective config 사용 — Routing Rules Editor
    프리뷰 용도.
    """
    await _ensure_tenant_match(tenant_id, user)
    cfg = TenantConfigService.load(tenant_id)
    routing_cfg = req.routing_config if req.routing_config is not None else (cfg.routing or {})
    # routing_config가 주어졌으면 schema 검증
    if req.routing_config is not None:
        try:
            validate_routing_yaml(req.routing_config)
        except RoutingSchemaError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "routing_schema_invalid", "errors": exc.errors},
            ) from exc

    decision = dryrun_decide(
        routing_config=routing_cfg,
        classifier_decision=req.classifier_decision,
        tenant_model_config=cfg.model or None,
        retrieval_confidence=req.retrieval_confidence,
    )
    return {
        "tenant_id": tenant_id,
        "sample_query": req.sample_query,
        "classifier_decision": req.classifier_decision,
        "decision": routing_decision_to_dict(decision),
    }


# ----------------------------------------------------------------------------
# Citation Inspector (ADR-017 §9 + ADR-010)
# ----------------------------------------------------------------------------


@router.get("/citation-inspector/distribution")
async def citation_distribution(
    tenant_id: str,
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    group_by: Literal["day", "hour"] = Query("day"),
    user: UserContext = Depends(require_admin),
    analytics=Depends(get_citation_distribution),
):
    """ADR-017 §9 — citation_type 분포 시계열 (date_trunc 버킷)."""
    await _ensure_tenant_match(tenant_id, user)
    result = await analytics.distribution(
        tenant_id=tenant_id,
        from_date=from_date, to_date=to_date,
        group_by=group_by,
    )
    return {
        "tenant_id": tenant_id,
        "granularity": result.granularity,
        "total_messages": result.total_messages,
        "from_date": result.from_date.isoformat() if result.from_date else None,
        "to_date": result.to_date.isoformat() if result.to_date else None,
        "buckets": [
            {"bucket": b.bucket, "counts": b.counts} for b in result.buckets
        ],
    }


@router.get("/citation-inspector/segments/{message_id}")
async def citation_segments(
    tenant_id: str,
    message_id: str,
    user: UserContext = Depends(require_admin),
    reader=Depends(get_chat_log_reader),
):
    """ADR-017 §9 — 단일 답변의 claim ↔ chunk 매핑 + verifier 통계.

    chat_logs.citations(claim_text/excerpt/support_level/similarity/chunk_id 포함)
    를 support_type별로 그룹화 + retrieved_chunks/verifier_metrics/conflict_groups/
    inference_judge_results를 함께 노출.
    """
    await _ensure_tenant_match(tenant_id, user)
    record = await reader.get(tenant_id=tenant_id, request_id=message_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "message_not_found", "message_id": message_id},
        )
    citations_by_type: dict[str, list[dict[str, Any]]] = {}
    for c in record.citations or []:
        st = c.get("support_type") or "direct"
        citations_by_type.setdefault(st, []).append(c)
    return {
        "tenant_id": tenant_id,
        "message_id": message_id,
        "question": record.question,
        "answer": record.answer,
        "citations": record.citations,
        "citation_types": record.citation_types,
        "citations_by_type": citations_by_type,
        "retrieved_chunks": record.retrieved_chunks,
        "verifier_metrics": record.verifier_metrics,
        "inference_judge_results": record.inference_judge_results,
        "conflict_groups": record.conflict_groups,
        "confidence": record.confidence,
        "fallback_reason": record.fallback_reason,
        "ui_mode": record.ui_mode,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


class ReverifyRequest(BaseModel):
    from_date: datetime | None = None
    to_date: datetime | None = None
    max_records: int = Field(default=100, ge=1, le=500)


@router.post("/citation-inspector/reverify")
async def citation_reverify(
    tenant_id: str,
    req: ReverifyRequest,
    user: UserContext = Depends(require_admin),
    service=Depends(get_citation_reverify_service),
):
    """ADR-017 §9 + ADR-010 §4 — Tier 2 재검증 동기 실행.

    현재 tenant의 citation.verification.tier2.thresholds로 chat_logs.citations를
    재계산. excerpt(audit truth) + claim_text를 재임베딩해 cosine similarity 산출.
    chat_logs.citations + verifier_metrics(reverified_at/by/tier2_avg_similarity)
    UPDATE. 운영 cap: max_records (default 100, 최대 500).
    """
    await _ensure_tenant_match(tenant_id, user)
    cfg = TenantConfigService.load(tenant_id)
    tier2 = ((cfg.citation or {}).get("verification") or {}).get("tier2") or {}
    thresholds = tier2.get("thresholds") or {}
    from rag_core.services.citation_reverifier import ReverifyThresholds

    summary = await service.reverify(
        tenant_id=tenant_id,
        actor=user.user_id,
        from_date=req.from_date,
        to_date=req.to_date,
        thresholds=ReverifyThresholds(
            strong=float(thresholds.get("strong", 0.75)),
            medium=float(thresholds.get("medium", 0.55)),
        ),
        max_records=req.max_records,
    )
    return {
        "tenant_id": summary.tenant_id,
        "from_date": summary.from_date.isoformat() if summary.from_date else None,
        "to_date": summary.to_date.isoformat() if summary.to_date else None,
        "scanned": summary.scanned,
        "updated": summary.updated,
        "skipped": summary.skipped,
        "upgraded": summary.upgraded,
        "downgraded": summary.downgraded,
        "avg_similarity_before": summary.avg_similarity_before,
        "avg_similarity_after": summary.avg_similarity_after,
        "failures": summary.failures,
    }


# ----------------------------------------------------------------------------
# Dashboard (ADR-017 §10)
# ----------------------------------------------------------------------------


@router.get("/dashboard")
async def dashboard(
    tenant_id: str,
    user: UserContext = Depends(require_admin),
    analytics=Depends(get_dashboard_analytics),
):
    """ADR-017 §10 — admin 대시보드. tenant scope (RLS) + KST today 집계.

    응답: total_documents / total_chunks / uploaded_today / indexing_completed_today /
    indexing_failed_today / questions_today / avg_latency_ms / answers_without_citation /
    negative_feedback_rate / citation_type_distribution / fallback_distribution /
    routing_distribution.
    """
    await _ensure_tenant_match(tenant_id, user)
    snap = await analytics.get_snapshot(tenant_id=tenant_id)
    return {
        "tenant_id": tenant_id,
        "total_documents": snap.total_documents,
        "total_chunks": snap.total_chunks,
        "uploaded_today": snap.uploaded_today,
        "indexing_completed_today": snap.indexing_completed_today,
        "indexing_failed_today": snap.indexing_failed_today,
        "questions_today": snap.questions_today,
        "avg_latency_ms": round(snap.avg_latency_ms, 4),
        "answers_without_citation": snap.answers_without_citation,
        "negative_feedback_rate": round(snap.negative_feedback_rate, 4),
        "citation_type_distribution": snap.citation_type_distribution,
        "fallback_distribution": snap.fallback_distribution,
        "routing_distribution": snap.routing_distribution,
    }


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _parse_metadata_json(raw: str | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "metadata_not_json", "reason": str(exc)},
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422,
            detail={"error": "metadata_not_object"},
        )
    return parsed


def _document_to_dict(doc) -> dict[str, Any]:
    return {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "version": doc.version,
        "input_type": doc.input_type,
        "source_type": doc.source_type,
        "object_storage_path": doc.object_storage_path,
        "department": doc.department,
        "doc_type": doc.doc_type,
        "security_level": doc.security_level,
        "owner": doc.owner,
        "tags": list(doc.tags or []),
        "language": doc.language,
        "valid_from": doc.valid_from,
        "valid_until": doc.valid_until,
        "approval_status": doc.approval_status,
        "file_hash": doc.file_hash,
        "parser_version": doc.parser_version,
        "metadata": dict(doc.metadata or {}),
    }


def _distribution(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return counts


def _job_to_dict(job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "tenant_id": job.tenant_id,
        "doc_id": job.doc_id,
        "doc_version": job.doc_version,
        "filename": job.filename,
        "status": job.status,
        "step": job.step,
        "progress": job.progress,
        "total_chunks": job.total_chunks,
        "indexed_chunks": job.indexed_chunks,
        "failed_chunks": list(job.failed_chunks or []),
        "error_message": job.error_message,
        "failure_rate": job.failure_rate,
        "retry_count": job.retry_count,
    }
