"""
Tenant-scope API — `/api/{tenant_id}/...` (ADR-017).

UserContext + tenant DB session이 모든 endpoint에 자동 주입됨.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.tenant_assessment import (
    admin_router as assessment_admin_router,
    router as assessment_router,
)
from app.api.tenant_chat import router as chat_router
from app.api.tenant_admin import router as admin_router
from app.api.tenant_me import router as me_router
from app.core.auth_adapter import UserContext, get_user_context
from app.core.db import get_tenant_session

router = APIRouter()

# `/api/{tenant_id}/chat` 등
router.include_router(chat_router, tags=["chat"])

# `/api/{tenant_id}/admin/*`
router.include_router(admin_router, prefix="/admin", tags=["tenant-admin"])

# `/api/{tenant_id}/assessment/*` (ADR-014)
router.include_router(assessment_router, prefix="/assessment", tags=["assessment"])

# `/api/{tenant_id}/admin/assessment/*`
router.include_router(
    assessment_admin_router, prefix="/admin/assessment", tags=["assessment-admin"]
)

# `/api/{tenant_id}/me/*` — user-self
router.include_router(me_router, prefix="/me", tags=["tenant-me"])
