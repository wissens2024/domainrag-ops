"""ADR-021 §6 — /health/live + /health/ready + /platform/admin/health/metrics."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.auth_adapter import UserContext, get_user_context
from app.main import app


def _platform_admin_user() -> UserContext:
    return UserContext(
        user_id="ops-001",
        domain_id="platform",
        roles=["PLATFORM_ADMIN"],
        clearance="top_secret",
    )


def _regular_admin_user() -> UserContext:
    return UserContext(
        user_id="admin-001",
        domain_id="security",
        roles=["ADMIN"],
        clearance="restricted",
    )


@pytest.fixture(autouse=True)
def _reset():
    yield
    app.dependency_overrides.clear()


def test_health_live_no_auth():
    with TestClient(app) as client:
        r = client.get("/api/health/live")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}


def test_health_ready_inmemory_backend():
    """inmemory backend는 외부 의존 0 — 항상 ready."""
    with TestClient(app) as client:
        r = client.get("/api/health/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["checks"]["backend_mode"] == "inmemory"


def test_health_legacy_endpoint_still_works():
    """ADR-021 §6 — /api/health 단축형 보존."""
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"


def test_platform_health_metrics_requires_platform_admin():
    app.dependency_overrides[get_user_context] = _regular_admin_user
    with TestClient(app) as client:
        r = client.get(
            "/api/platform/admin/health/metrics",
            headers={"Authorization": "Bearer mock"},
        )
        assert r.status_code == 403


def test_platform_health_metrics_returns_failure_buckets():
    from app.services.chat_log_writer import (
        CHAT_LOG_WRITE_DEAD_LETTERS,
        reset_chat_log_failure_metrics,
    )
    from app.services.ledger_client import (
        LEDGER_DEAD_LETTERS,
        reset_ledger_failure_metrics,
    )

    reset_ledger_failure_metrics()
    reset_chat_log_failure_metrics()
    # 가짜 실패 1건씩 누적
    LEDGER_DEAD_LETTERS.append(
        {"event_type": "auth_failure", "domain_id": "t1", "error": "x"}
    )
    CHAT_LOG_WRITE_DEAD_LETTERS.append(
        {"domain_id": "t1", "request_id": "r1", "exc_type": "RuntimeError"}
    )

    app.dependency_overrides[get_user_context] = _platform_admin_user
    with TestClient(app) as client:
        r = client.get(
            "/api/platform/admin/health/metrics",
            headers={"Authorization": "Bearer mock"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ledger"]["dead_letter_count"] == 1
        assert body["chat_log_writer"]["dead_letter_count"] == 1
    reset_ledger_failure_metrics()
    reset_chat_log_failure_metrics()
