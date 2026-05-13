"""L4 PostgreSQL integration test fixtures.

testcontainers가 임시 PostgreSQL 컨테이너를 띄우고:
  1. init_postgres.sql 실행 (두 role 생성 + extension)
  2. alembic upgrade head (모든 migration 적용)
  3. domainrag_app 세션 / domainrag_platform_admin 세션 양쪽을 fixture로 제공

Docker daemon이 없으면 자동 skip. unit suite와 격리.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-not-found]
    _HAS_TESTCONTAINERS = True
except ImportError:
    _HAS_TESTCONTAINERS = False


REPO_ROOT = Path(__file__).resolve().parents[3]
INIT_SQL = REPO_ROOT / "infra" / "scripts" / "init_postgres.sql"


def pytest_collection_modifyitems(config, items):
    """testcontainers 미설치 또는 docker daemon 미가용 시 통합 테스트 skip."""
    skip_marker = None
    if not _HAS_TESTCONTAINERS:
        skip_marker = pytest.mark.skip(reason="testcontainers not installed")
    else:
        try:
            import docker  # type: ignore[import-not-found]

            docker.from_env().ping()
        except Exception:  # noqa: BLE001
            skip_marker = pytest.mark.skip(reason="docker daemon unavailable")
    if skip_marker is None:
        return
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def postgres_container():
    """superuser 컨테이너 — 1 session 동안 유지. init_sql + alembic은 caller가 처리."""
    if not _HAS_TESTCONTAINERS:
        pytest.skip("testcontainers not installed")
    container = PostgresContainer("postgres:16-alpine")
    container.with_env("POSTGRES_DB", "domainrag")
    container.with_env("POSTGRES_USER", "postgres")
    container.with_env("POSTGRES_PASSWORD", "changeme_superuser")
    container.start()
    try:
        # init_postgres.sql 실행 (두 role 생성 + extension)
        import psycopg

        super_dsn = container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql://"
        )
        with psycopg.connect(super_dsn) as conn:
            with conn.cursor() as cur:
                # \set 같은 psql meta는 동작 안 함 — 해당 라인 제거 후 실행
                sql_text = INIT_SQL.read_text(encoding="utf-8")
                cleaned = "\n".join(
                    line for line in sql_text.splitlines() if not line.startswith("\\set")
                )
                cur.execute(cleaned)
            conn.commit()
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def admin_dsn(postgres_container):
    """domainrag_platform_admin (BYPASSRLS) 접속 DSN."""
    base = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    # postgres:changeme_superuser → domainrag_platform_admin:changeme_admin
    import urllib.parse as up

    parsed = up.urlparse(base)
    netloc = f"domainrag_platform_admin:changeme_admin@{parsed.hostname}:{parsed.port}"
    return up.urlunparse(parsed._replace(netloc=netloc))


@pytest.fixture(scope="session")
def app_dsn(postgres_container):
    """domainrag_app (RLS 적용) 접속 DSN."""
    base = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    import urllib.parse as up

    parsed = up.urlparse(base)
    netloc = f"domainrag_app:changeme@{parsed.hostname}:{parsed.port}"
    return up.urlunparse(parsed._replace(netloc=netloc))


@pytest.fixture(scope="session")
def alembic_upgraded(postgres_container, admin_dsn):
    """alembic upgrade head 1회 실행. session 동안 모든 테이블 사용 가능."""
    # alembic은 settings.db_admin_dsn을 사용 — 환경변수로 주입
    parsed_host = admin_dsn  # 그대로 사용
    import urllib.parse as up

    parsed = up.urlparse(parsed_host)
    os.environ["POSTGRES_HOST"] = parsed.hostname
    os.environ["POSTGRES_PORT"] = str(parsed.port)
    os.environ["POSTGRES_DB"] = (parsed.path or "/domainrag").lstrip("/")
    os.environ["POSTGRES_ADMIN_USER"] = "domainrag_platform_admin"
    os.environ["POSTGRES_ADMIN_PASSWORD"] = "changeme_admin"
    os.environ["POSTGRES_USER"] = "domainrag_app"
    os.environ["POSTGRES_PASSWORD"] = "changeme"

    # settings cache invalidate
    from app.core.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    # alembic 명령 실행 — backend 디렉토리에서 'alembic upgrade head'
    import subprocess

    backend_dir = REPO_ROOT / "backend"
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=str(backend_dir),
        capture_output=True, text=True,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic upgrade head failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return True
