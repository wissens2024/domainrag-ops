"""
DomainRAG Ops Backend — FastAPI Application

멀티테넌트 RAG 플랫폼 (ADR-008/017/018).
URL: /api/{tenant_id}/..., /api/platform/admin/..., /api/auth/...

lifespan 책임 (ADR-021 §1):
  1. structured logging
  2. tenant_config_overrides 일괄 preload (재시작 후 runtime override 회복)
  3. tenant_input_schemas active 일괄 preload
  4. PostgreSQL LISTEN 3채널 구독 (multi-instance 동기화)

RAG_BACKEND=inmemory 일 때는 DB 의존이 없으므로 preload·LISTEN 모두 skip.
"""

import asyncio
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
from app.core.db import AdminSessionLocal
from app.core.logging import setup_logging
from app.services.cron_scheduler import CronScheduler
from app.services.listen_notify import PostgresNotifyListener
from app.services.startup_preload import (
    preload_tenant_configs,
    preload_tenant_input_schemas,
    reload_tenant_config,
    reload_tenant_schema,
)

setup_logging()
logger = structlog.get_logger(__name__)
settings = get_settings()


async def _on_config_changed(payload: dict) -> None:
    tid = payload.get("tenant_id")
    if not tid:
        return
    await reload_tenant_config(
        admin_session_factory=AdminSessionLocal, tenant_id=str(tid)
    )
    logger.info("config_listener.applied", tenant_id=str(tid))


async def _on_schema_changed(payload: dict) -> None:
    tid = payload.get("tenant_id")
    if not tid:
        return
    await reload_tenant_schema(
        admin_session_factory=AdminSessionLocal, tenant_id=str(tid)
    )
    logger.info("schema_listener.applied", tenant_id=str(tid))


async def _on_lifecycle_changed(payload: dict) -> None:
    # tenant register / status 전이 / hard delete 직후. 캐시 invalidate 위주.
    tid = payload.get("tenant_id")
    if not tid:
        return
    from app.core.tenant_config_service import TenantConfigService
    TenantConfigService.invalidate(str(tid))
    logger.info(
        "lifecycle_listener.applied",
        tenant_id=str(tid),
        old=payload.get("old_status"),
        new=payload.get("new_status"),
    )


async def _on_listener_reconnect(_payload: dict) -> None:
    """LISTEN connection 재연결 직후 — NOTIFY 누락 보전을 위해 full preload 1회."""
    logger.info("config_listener.reconnect — running full preload")
    try:
        await preload_tenant_configs(admin_session_factory=AdminSessionLocal)
        await preload_tenant_input_schemas(admin_session_factory=AdminSessionLocal)
    except Exception as exc:  # noqa: BLE001
        logger.warning("preload after reconnect failed", error=str(exc))


async def _archival_job() -> None:
    """ArchivalWorker 호출 — tenant lifecycle.yaml.chunks_archive_days 기준."""
    from app.services.archival_worker import ArchivalWorker

    worker = ArchivalWorker(admin_session_factory=AdminSessionLocal)
    await worker.run_all_tenants()


async def _partition_job() -> None:
    """ChatLogsPartitionService — 다음달 partition 보장."""
    from app.services.chat_logs_partition_service import (
        ChatLogsPartitionService,
    )

    svc = ChatLogsPartitionService(admin_session_factory=AdminSessionLocal)
    await svc.ensure_next_month_partition()


async def _old_collection_drop_scan_job() -> None:
    """OldCollectionDropService — 30일 hold 경과 old collection 후보 알림 (ADR-021 §3)."""
    from app.deps import get_ledger_audit_service
    from app.services.old_collection_drop_service import OldCollectionDropService

    ledger = get_ledger_audit_service(settings)
    svc = OldCollectionDropService(
        admin_session_factory=AdminSessionLocal,
        ledger_audit=ledger,
    )
    await svc.scan()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup", env=settings.env, auth_mode=settings.auth_mode,
                rag_backend=settings.rag_backend, ops_cron_mode=settings.ops_cron_mode)

    listener: PostgresNotifyListener | None = None
    scheduler: CronScheduler | None = None

    # RAG_BACKEND=inmemory에선 DB 의존 없음 — preload·LISTEN·cron 모두 skip
    if settings.rag_backend == "production":
        try:
            cfg_groups = await preload_tenant_configs(
                admin_session_factory=AdminSessionLocal
            )
            schema_tids = await preload_tenant_input_schemas(
                admin_session_factory=AdminSessionLocal
            )
            logger.info(
                "preload.done", config_groups=cfg_groups, schema_tenants=schema_tids
            )
        except Exception as exc:  # noqa: BLE001
            # preload 실패는 fail-fast — DB 미가용이면 백엔드가 의미 없음
            logger.error("preload.failed", error=str(exc))
            raise

        listener = PostgresNotifyListener(
            dsn=settings.db_admin_dsn,
            handlers={
                "tenant_config_changed": _on_config_changed,
                "tenant_schema_changed": _on_schema_changed,
                "tenant_lifecycle_changed": _on_lifecycle_changed,
            },
            on_reconnect=_on_listener_reconnect,
        )
        listener.start()
        _health_state["listener_alive"] = True
        logger.info("pg_notify_listener.started")

        if settings.ops_cron_mode == "internal":
            scheduler = CronScheduler()
            scheduler.register(
                name="chunks_archival",
                interval_seconds=settings.ops_cron_archival_interval_seconds,
                run=_archival_job,
                initial_delay_seconds=300.0,  # 5분 후 첫 실행 — startup 안정화 대기
            )
            scheduler.register(
                name="chat_logs_partition",
                interval_seconds=settings.ops_cron_partition_interval_seconds,
                run=_partition_job,
                initial_delay_seconds=60.0,
            )
            scheduler.register(
                name="old_collection_drop_scan",
                interval_seconds=settings.ops_cron_old_collection_interval_seconds,
                run=_old_collection_drop_scan_job,
                initial_delay_seconds=600.0,  # 10분 후 첫 실행 — 우선순위 낮음
            )
            scheduler.start()
            logger.info("cron_scheduler.started")
        else:
            logger.info(
                "ops_cron_mode=external — internal scheduler skipped"
            )

    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()
            logger.info("cron_scheduler.stopped")
        if listener is not None:
            await listener.stop()
            _health_state["listener_alive"] = False
            logger.info("pg_notify_listener.stopped")
        logger.info("shutdown")


app = FastAPI(
    title="DomainRAG Ops",
    description="폐쇄망 멀티테넌트 RAG 플랫폼 (ADR-008~021)",
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
    """ADR-021 §6 — process liveness 단축형 (k8s liveness probe와 동등).

    이 endpoint는 인증 불필요 + DB/외부 의존 검증 안 함. 단지 process 살아있음 확인.
    DB/Qdrant/Storage 검증은 /api/health/ready 사용.
    """
    return {"status": "healthy", "service": "domainrag-ops", "version": "0.1.0"}


@app.get("/api/health/live", tags=["health"])
async def health_live():
    """ADR-021 §6 — process 살아있음만 (k8s liveness probe). 항상 200.

    외부 의존 확인 안 함 — DB 단절 시에도 process 자체는 alive로 응답.
    """
    return {"status": "alive"}


_health_state: dict = {"listener_alive": False}


@app.get("/api/health/ready", tags=["health"])
async def health_ready():
    """ADR-021 §6 — readiness probe.

    Production 모드 5-check:
      - db (admin engine SELECT 1 ≤ 1초)
      - migration (alembic_version 존재)
      - qdrant (client.health() ≤ 1초)
      - storage (MinIO bucket exists ≤ 1초)
      - config_listener (LISTEN task alive)
      - partition (현재달 + 다음달 chat_logs partition 존재 — 자가치유 가능하므로 warn만)

    어느 하나라도 fail 시 503. inmemory backend는 외부 의존 0이므로 항상 200.
    """
    checks: dict[str, str | bool] = {}
    overall_ok = True

    if settings.rag_backend == "production":
        from sqlalchemy import text

        # 1) DB readiness
        try:
            async with AdminSessionLocal() as session:
                await asyncio.wait_for(
                    session.execute(text("SELECT 1")), timeout=1.0
                )
            checks["db"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["db"] = f"error: {exc}"
            overall_ok = False

        # 2) Migration version
        try:
            async with AdminSessionLocal() as session:
                row = (
                    await asyncio.wait_for(
                        session.execute(text("SELECT version_num FROM alembic_version")),
                        timeout=1.0,
                    )
                ).first()
            checks["migration"] = row[0] if row else "missing"
            if not row:
                overall_ok = False
        except Exception as exc:  # noqa: BLE001
            checks["migration"] = f"error: {exc}"
            overall_ok = False

        # 3) Qdrant — lazy import
        try:
            import httpx

            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.get(
                    f"http://{settings.qdrant_host}:{settings.qdrant_port}/healthz"
                )
            checks["qdrant"] = "ok" if resp.status_code < 400 else f"status={resp.status_code}"
            if resp.status_code >= 400:
                overall_ok = False
        except Exception as exc:  # noqa: BLE001
            checks["qdrant"] = f"error: {exc}"
            overall_ok = False

        # 4) Storage (MinIO)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.get(
                    f"http://{settings.minio_endpoint}/minio/health/live"
                )
            checks["storage"] = "ok" if resp.status_code < 400 else f"status={resp.status_code}"
            if resp.status_code >= 400:
                overall_ok = False
        except Exception as exc:  # noqa: BLE001
            checks["storage"] = f"error: {exc}"
            overall_ok = False

        # 5) Config listener
        checks["config_listener"] = "alive" if _health_state.get("listener_alive") else "not_started"
        if not _health_state.get("listener_alive"):
            overall_ok = False

        # 6) Partition self-heal (warn만 — INSERT 시 self-heal로 보완)
        try:
            from datetime import datetime as _dt

            now = _dt.utcnow()
            this_month = f"chat_logs_y{now.year:04d}m{now.month:02d}"
            async with AdminSessionLocal() as session:
                row = (
                    await asyncio.wait_for(
                        session.execute(
                            text(
                                "SELECT 1 FROM pg_class WHERE relname = :name"
                            ),
                            {"name": this_month},
                        ),
                        timeout=1.0,
                    )
                ).first()
            checks["partition_current"] = "ok" if row else f"missing_{this_month}_warn"
            # missing은 503 안 만듦 — INSERT 시 자가치유
        except Exception as exc:  # noqa: BLE001
            checks["partition_current"] = f"error: {exc}"
    else:
        checks["backend_mode"] = "inmemory"

    if not overall_ok:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": checks},
        )
    return {"status": "ready", "checks": checks}


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
