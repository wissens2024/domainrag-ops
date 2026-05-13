"""
DomainRAG Ops Backend — FastAPI Application

멀티테넌트 RAG 플랫폼 (ADR-008/017/018).
URL: /api/{tenant_id}/..., /api/platform/admin/..., /api/auth/...
"""

from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.platform import router as platform_router
from app.api.tenant import router as tenant_router
from app.core.config import get_settings
from app.core.logging import setup_logging

setup_logging()
logger = structlog.get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup", env=settings.env, auth_mode=settings.auth_mode)
    yield
    logger.info("shutdown")


app = FastAPI(
    title="DomainRAG Ops",
    description="폐쇄망 멀티테넌트 RAG 플랫폼 (ADR-008~020)",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록 (ADR-017 URL 컨벤션) — platform prefix를 tenant{id}보다 먼저 등록해
# `/api/platform/admin/...` 경로가 tenant_id="platform"로 흡수되지 않도록 한다.
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(platform_router, prefix="/api/platform/admin", tags=["platform"])
app.include_router(tenant_router, prefix="/api/{tenant_id}", tags=["tenant"])


@app.get("/api/health", tags=["health"])
async def health_check():
    """인증 불필요 — 헬스체크 (ADR-017 §1)."""
    return {"status": "healthy", "service": "domainrag-ops", "version": "0.1.0"}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        exc_type=type(exc).__name__,
        exc_msg=str(exc),
    )
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "message": "운영자 문의"},
    )


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.env == "development",
    )
