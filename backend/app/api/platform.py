"""
Platform Admin API — `/api/platform/admin/*` (ADR-017 §18).

platform_admin role 한정. BYPASSRLS DB session 사용 가능.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_adapter import UserContext, get_user_context
from app.core.db import get_admin_db_session
from app.deps import (
    get_pii_approval_service,
    get_rag_service,
    get_tenant_lifecycle_service,
    reset_rag_service,
)
from app.services.pii_storage_approval_service import (
    PiiApprovalConflictError,
    PiiApprovalNotFoundError,
    PiiApprovalRecord,
)
from app.services.tenant_lifecycle_service import (
    InvalidStatusTransitionError,
    TenantConflictError,
    TenantNotArchivedError,
    TenantNotFoundError,
)

router = APIRouter()


def require_platform_admin(user: UserContext = Depends(get_user_context)) -> UserContext:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail={"error": "insufficient_role"})
    return user


def _approval_to_dict(record: PiiApprovalRecord) -> dict[str, Any]:
    return {
        "approval_id": record.approval_id,
        "tenant_id": record.tenant_id,
        "policy": record.policy,
        "reason": record.reason,
        "approved_by": record.approved_by,
        "valid_from": record.valid_from.isoformat() if record.valid_from else None,
        "valid_until": record.valid_until.isoformat() if record.valid_until else None,
        "status": record.status,
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
        "revoked_by": record.revoked_by,
        "revoke_reason": record.revoke_reason,
    }


class RegisterTenantRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=200)
    domain_type: str | None = None
    modules: list[str] = Field(default_factory=lambda: ["rag"])


class StatusPatchRequest(BaseModel):
    status: str = Field(..., min_length=1)  # active | suspended | archived
    reason: str | None = None


class HardDeleteTenantRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


@router.get("/tenants")
async def list_tenants(
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    user: UserContext = Depends(require_platform_admin),
    service=Depends(get_tenant_lifecycle_service),
):
    """ADR-017 §18 — tenant 목록 (cross-tenant, BYPASSRLS)."""
    if page < 1 or page_size < 1:
        raise HTTPException(status_code=400, detail={"error": "invalid_paging"})
    items = await service.list_(
        status=status, limit=page_size, offset=(page - 1) * page_size
    )
    return {"items": [t.to_dict() for t in items], "page": page, "page_size": page_size}


@router.post("/tenants", status_code=201)
async def register_tenant(
    req: RegisterTenantRequest,
    user: UserContext = Depends(require_platform_admin),
    service=Depends(get_tenant_lifecycle_service),
):
    """ADR-008 §4 + ADR-012 §2 — tenants row 등록 + collection 자동 생성 + Ledger publish.

    AuthFusion client 등록은 별도 step (운영자 수동 또는 후속 ADR). 본 endpoint는 DomainRAG
    내부 메타·인프라 setup만 처리.
    """
    try:
        record = await service.register(
            tenant_id=req.tenant_id,
            display_name=req.display_name,
            domain_type=req.domain_type,
            modules=req.modules,
            actor=user.user_id,
        )
    except TenantConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "tenant_already_exists", "tenant_id": exc.tenant_id},
        ) from exc
    return record.to_dict()


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str,
    user: UserContext = Depends(require_platform_admin),
    service=Depends(get_tenant_lifecycle_service),
):
    try:
        rec = await service.get(tenant_id)
    except TenantNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"error": "tenant_not_found", "tenant_id": tenant_id}
        ) from exc
    return rec.to_dict()


@router.patch("/tenants/{tenant_id}")
async def patch_tenant_status(
    tenant_id: str,
    req: StatusPatchRequest,
    user: UserContext = Depends(require_platform_admin),
    service=Depends(get_tenant_lifecycle_service),
):
    """ADR-012 §2 — status 전이. active ↔ suspended ↔ archived, archived → active (복구)."""
    try:
        rec = await service.update_status(
            tenant_id=tenant_id, to_status=req.status,
            actor=user.user_id, reason=req.reason,
        )
    except TenantNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"error": "tenant_not_found"}
        ) from exc
    except InvalidStatusTransitionError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_status_transition",
                "from": exc.frm,
                "to": exc.to,
            },
        ) from exc
    return rec.to_dict()


@router.delete("/tenants/{tenant_id}/hard")
async def hard_delete_tenant(
    tenant_id: str,
    req: HardDeleteTenantRequest,
    user: UserContext = Depends(require_platform_admin),
    service=Depends(get_tenant_lifecycle_service),
):
    """ADR-012 §6 cross-system 일관성. archived 상태만 허용. 부분 실패는 dead-letter."""
    try:
        rec = await service.hard_delete(
            tenant_id=tenant_id, actor=user.user_id, reason=req.reason
        )
    except TenantNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"error": "tenant_not_found"}
        ) from exc
    except TenantNotArchivedError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "tenant_must_be_archived",
                "current_status": exc.status,
            },
        ) from exc
    return rec.to_dict()


@router.get("/endpoints")
async def list_endpoints(user: UserContext = Depends(require_platform_admin)):
    """ADR-013 endpoint health dashboard.

    설정된 endpoint URL과 backend 모드(production/inmemory)에 따라 상태를 종합.
    InMemory backend는 실 endpoint 미호출 — 'configured'(설정만 됨)로 응답. 운영
    backend도 실시간 health probe는 별도 (이 endpoint는 설정 + last_known status만
    노출하며, 실제 ping은 모니터링 시스템 또는 별도 worker가 수행 — ADR-019).
    """
    from app.core.config import get_settings

    settings = get_settings()
    inmemory = settings.rag_backend == "inmemory"
    base_status = "configured" if inmemory else "unknown"
    items = [
        {"name": "tenant_slm", "kind": "vllm",
         "url": settings.tenant_slm_base_url, "status": base_status},
        {"name": "shared_llm", "kind": "vllm",
         "url": settings.shared_llm_base_url, "status": base_status},
        {"name": "embedder", "kind": "tei",
         "url": settings.embedding_server_url, "status": base_status},
        {"name": "reranker", "kind": "tei",
         "url": settings.reranker_server_url, "status": base_status},
        {"name": "qdrant", "kind": "vector_store",
         "url": f"http://{settings.qdrant_host}:{settings.qdrant_port}",
         "status": base_status},
        {"name": "postgres", "kind": "database",
         "url": f"{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}",
         "status": base_status},
        {"name": "minio", "kind": "object_storage",
         "url": settings.minio_endpoint, "status": base_status},
        {"name": "ollama", "kind": "vision_llm",
         "url": settings.ollama_base_url, "status": base_status},
    ]
    return {
        "backend": settings.rag_backend,
        "items": items,
        "total": len(items),
    }


# ----------------------------------------------------------------------------
# Cross-tenant Analytics (ADR-017 §18) — BYPASSRLS via admin_engine
# ----------------------------------------------------------------------------


@router.get("/analytics/usage")
async def cross_tenant_usage(
    user: UserContext = Depends(require_platform_admin),
):
    """ADR-017 §18 — tenant별 사용량 종합. InMemory backend는 RAGService의 writer
    records로부터 직접 집계 (BYPASSRLS 불필요). production은 admin engine으로 chat_logs
    cross-tenant aggregate.
    """
    from app.core.config import get_settings

    settings = get_settings()
    if settings.rag_backend == "inmemory":
        rag = get_rag_service(settings)
        writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
        by_tenant: dict[str, dict[str, Any]] = {}
        for rec in writer.records:
            t = rec.tenant_id
            entry = by_tenant.setdefault(
                t, {"tenant_id": t, "messages": 0, "fallbacks": 0,
                     "avg_latency_ms": 0.0, "_latencies": []},
            )
            entry["messages"] += 1
            if rec.fallback_reason:
                entry["fallbacks"] += 1
            if rec.latency_ms:
                entry["_latencies"].append(rec.latency_ms)
        for entry in by_tenant.values():
            lat = entry.pop("_latencies")
            entry["avg_latency_ms"] = (
                round(sum(lat) / len(lat), 4) if lat else 0.0
            )
        return {"items": list(by_tenant.values()), "total": len(by_tenant)}

    # production
    from sqlalchemy import text

    from app.core.db import AdminSessionLocal

    async with AdminSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT tenant_id,
                           COUNT(*) AS messages,
                           COUNT(*) FILTER (WHERE fallback_reason IS NOT NULL) AS fallbacks,
                           COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                      FROM chat_logs
                     GROUP BY tenant_id
                     ORDER BY messages DESC
                    """
                )
            )
        ).all()
    return {
        "items": [
            {
                "tenant_id": r[0],
                "messages": int(r[1] or 0),
                "fallbacks": int(r[2] or 0),
                "avg_latency_ms": round(float(r[3] or 0.0), 4),
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/analytics/health")
async def cross_tenant_health(
    user: UserContext = Depends(require_platform_admin),
):
    """ADR-017 §18 — endpoint health 종합 (위 /endpoints 재활용 + 모니터링 요약)."""
    return await list_endpoints(user=user)


@router.get("/health/metrics")
async def get_health_metrics(
    user: UserContext = Depends(require_platform_admin),
):
    """ADR-021 후속 — 단일 process 내 운영 지표 노출 (관제 polling).

    포함 항목:
      - ledger publish 실패 누적 + dead-letter 최근 10건
      - chat_log write 실패 누적 + dead-letter 최근 10건

    multi-instance 환경에선 각 process별로 다른 값이므로 관제 dashboard에선 instance
    단위로 분리 표시 권장.
    """
    from app.services.chat_log_writer import get_chat_log_failure_metrics
    from app.services.ledger_client import get_ledger_failure_metrics

    return {
        "ledger": get_ledger_failure_metrics(),
        "chat_log_writer": get_chat_log_failure_metrics(),
    }


# ----------------------------------------------------------------------------
# Platform Configs (ADR-017 §18)
# ----------------------------------------------------------------------------


_PLATFORM_CONFIG_CATEGORIES = (
    "citation", "retrieval", "model", "routing", "query_classifier",
    "lifecycle", "auth", "pii", "audit", "data_retention",
)


@router.get("/configs/{category}")
async def get_platform_config(
    category: str,
    user: UserContext = Depends(require_platform_admin),
):
    """ADR-017 §18 — platform/<category>.yaml read."""
    if category not in _PLATFORM_CONFIG_CATEGORIES:
        raise HTTPException(
            status_code=404, detail={"error": "unknown_category", "category": category},
        )
    from pathlib import Path

    import yaml

    from app.core.config import get_settings

    path: Path = get_settings().config_dir.resolve() / "platform" / f"{category}.yaml"
    if not path.exists():
        return {"category": category, "value": {}, "exists": False}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {"category": category, "value": data, "exists": True}


class PlatformConfigPutRequest(BaseModel):
    value: dict[str, Any] = Field(..., description="전체 yaml dict")
    reason: str | None = None


@router.put("/configs/{category}")
async def put_platform_config(
    category: str,
    req: PlatformConfigPutRequest,
    user: UserContext = Depends(require_platform_admin),
):
    """ADR-017 §18 — platform/<category>.yaml 전체 교체.

    파일 시스템 mutation — 운영에서는 신중. 변경 후 TenantConfigService.invalidate(전체)
    로 모든 tenant 캐시 무효화.
    """
    if category not in _PLATFORM_CONFIG_CATEGORIES:
        raise HTTPException(
            status_code=404, detail={"error": "unknown_category", "category": category},
        )
    from pathlib import Path

    import yaml

    from app.core.config import get_settings
    from app.core.tenant_config_service import TenantConfigService

    settings = get_settings()
    platform_dir: Path = settings.config_dir.resolve() / "platform"
    platform_dir.mkdir(parents=True, exist_ok=True)
    path = platform_dir / f"{category}.yaml"
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(req.value, f, allow_unicode=True, sort_keys=False)
    TenantConfigService.invalidate()
    return {"category": category, "value": req.value, "reason": req.reason}


# ----------------------------------------------------------------------------
# PII storage policy approvals (ADR-020 §4)
# ----------------------------------------------------------------------------


class PiiApprovalRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)
    valid_until: datetime | None = None


class PiiApprovalRevokeRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


@router.post(
    "/tenants/{tenant_id}/pii-storage-approvals",
    status_code=201,
)
async def approve_plain_storage(
    tenant_id: str,
    req: PiiApprovalRequest,
    user: UserContext = Depends(require_platform_admin),
    approval_service=Depends(get_pii_approval_service),
):
    """ADR-020 §4 — plain 보관 정책 승인.

    같은 tenant에 이미 active 승인이 있으면 409. 승인 직후 RAGService 싱글턴을 reset해
    config_loader가 새 plain_approved 값을 다음 chat 요청에 즉시 반영하게 한다.
    """
    try:
        record = await approval_service.approve_plain(
            tenant_id=tenant_id,
            reason=req.reason,
            approved_by=user.user_id,
            valid_until=req.valid_until,
        )
    except PiiApprovalConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "approval_active", "existing_id": exc.existing_id},
        ) from exc

    reset_rag_service()
    return _approval_to_dict(record)


@router.get("/tenants/{tenant_id}/pii-storage-approvals")
async def list_plain_storage_approvals(
    tenant_id: str,
    page: int = 1,
    page_size: int = 50,
    user: UserContext = Depends(require_platform_admin),
    approval_service=Depends(get_pii_approval_service),
):
    """ADR-020 §4 — tenant 단위 승인 이력 (active 1건 + 과거 revoked rows)."""
    if page < 1 or page_size < 1:
        raise HTTPException(status_code=400, detail={"error": "invalid_paging"})
    offset = (page - 1) * page_size
    rows = await approval_service.list_by_tenant(
        tenant_id=tenant_id, limit=page_size, offset=offset
    )
    return {
        "items": [_approval_to_dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
    }


@router.delete("/tenants/{tenant_id}/pii-storage-approvals/active")
async def revoke_plain_storage_approval(
    tenant_id: str,
    req: PiiApprovalRevokeRequest,
    user: UserContext = Depends(require_platform_admin),
    approval_service=Depends(get_pii_approval_service),
):
    """ADR-020 §4 — active 승인 회수. 회수 즉시 RAGService config_loader가 mask로 복귀."""
    try:
        record = await approval_service.revoke_active(
            tenant_id=tenant_id,
            revoked_by=user.user_id,
            revoke_reason=req.reason,
        )
    except PiiApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"error": "no_active_approval"}
        ) from exc

    reset_rag_service()
    return _approval_to_dict(record)
