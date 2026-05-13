"""User-self API — `/api/{tenant_id}/me/*` (ADR-020 §10, ADR-017 §17 보강).

사용자 본인 데이터에 대한 권한. tenant_admin/platform_admin도 본 경로로는 타인 logs를
지울 수 없다 (admin endpoint는 별도 ADR에서 정의됨). service account는 erase 호출 차단.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from rag_core.services.chat_log_erasure import ErasureMode

from app.core.auth_adapter import UserContext, get_user_context
from app.core.config import get_settings
from app.core.tenant_guard import ensure_tenant_match
from app.deps import get_chat_log_eraser, get_ledger_audit_service

router = APIRouter()


class EraseChatLogsRequest(BaseModel):
    # 기본값 mask_only — 운영 통계 보존 + PII/발화 본문만 제거 (ADR-020 §10 결정)
    mode: Literal["mask_only", "hard_delete"] = "mask_only"
    reason: str = Field(..., min_length=1, max_length=2000)


@router.delete("/chat_logs")
async def erase_my_chat_logs(
    tenant_id: str,
    req: EraseChatLogsRequest,
    user: UserContext = Depends(get_user_context),
    eraser=Depends(get_chat_log_eraser),
):
    """본인 chat_logs 삭제/마스킹 (ADR-020 §10).

    - service_account는 본 endpoint 호출 차단 (사용자 권한 대리 행위 금지)
    - JWT의 tenant_id와 path tenant_id가 일치하지 않으면 403 (get_user_context에서
      AuthFusionAdapter가 검증; mock에서는 default_user 매핑)
    - mode=mask_only(기본): chat_logs 컬럼 단위 마스킹, 운영 지표 보존
    - mode=hard_delete: row 자체 DELETE
    """
    if user.is_service_account:
        raise HTTPException(
            status_code=403, detail={"error": "service_account_cannot_erase"}
        )
    await ensure_tenant_match(
        tenant_id, user, ledger=get_ledger_audit_service(get_settings())
    )

    result = await eraser.erase_my_logs(
        tenant_id=tenant_id,
        user_id=user.user_id,
        mode=ErasureMode(req.mode),
        reason=req.reason,
    )
    return {
        "tenant_id": result.tenant_id,
        "user_id": result.user_id,
        "mode": result.mode.value,
        "affected_rows": result.affected_rows,
        "reason": result.reason,
    }
