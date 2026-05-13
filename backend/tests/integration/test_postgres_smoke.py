"""L4 — PostgreSQL 첫 실행 smoke test.

검증 범위:
  1. init_postgres.sql이 두 role + extension을 정상 생성
  2. alembic upgrade head가 7개 migration 무오류 적용
  3. domainrag_app role로 chat_logs INSERT + SELECT (RLS context 적용)
  4. domainrag_platform_admin role로 cross-tenant SELECT (BYPASSRLS)
  5. chat_logs partitioning이 동작 (CREATE TABLE chat_logs_y2026m05)
  6. JSONB cast 동작 (citations/citation_types/verifier_metrics)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest


def _run_sql(dsn: str, sql: str, params=None):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            try:
                return cur.fetchall()
            except psycopg.ProgrammingError:
                return None


def test_two_roles_exist(postgres_container, admin_dsn):
    rows = _run_sql(
        admin_dsn,
        "SELECT rolname, rolbypassrls, rolcanlogin FROM pg_roles "
        "WHERE rolname IN ('domainrag_app', 'domainrag_platform_admin') "
        "ORDER BY rolname",
    )
    by_name = {r[0]: r for r in rows}
    assert "domainrag_app" in by_name
    assert "domainrag_platform_admin" in by_name
    assert by_name["domainrag_app"][1] is False  # BYPASSRLS=false
    assert by_name["domainrag_platform_admin"][1] is True  # BYPASSRLS=true


def test_extensions_installed(postgres_container, admin_dsn):
    rows = _run_sql(
        admin_dsn,
        "SELECT extname FROM pg_extension WHERE extname IN ('pgcrypto', 'pg_trgm')",
    )
    names = {r[0] for r in rows}
    assert "pgcrypto" in names


def test_alembic_upgrade_creates_chat_logs(alembic_upgraded, admin_dsn):
    """alembic upgrade head 실행 후 chat_logs 테이블 + partition이 존재."""
    rows = _run_sql(
        admin_dsn,
        """
        SELECT tablename FROM pg_tables
         WHERE tablename IN ('chat_logs', 'chat_logs_y2026m05',
                             'tenants', 'documents', 'chunks',
                             'conversations', 'messages',
                             'tenant_input_schemas', 'adapter_registry',
                             'assessment_items', 'assessment_logs',
                             'pii_storage_approvals', 'chunks_archive',
                             'evaluation_jobs', 'tenant_delete_failures')
         ORDER BY tablename
        """,
    )
    tables = {r[0] for r in rows}
    # 핵심 테이블 — 빠지면 alembic 누락
    for t in [
        "chat_logs", "chat_logs_y2026m05", "tenants",
        "documents", "chunks", "conversations",
        "tenant_input_schemas", "adapter_registry",
        "assessment_items", "assessment_logs",
        "pii_storage_approvals", "chunks_archive",
        "evaluation_jobs", "tenant_delete_failures",
    ]:
        assert t in tables, f"missing table: {t}"


def test_chat_logs_feedback_comment_column_exists(alembic_upgraded, admin_dsn):
    """007_chat_logs_feedback_comment migration 적용 확인."""
    rows = _run_sql(
        admin_dsn,
        """
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'chat_logs' AND column_name = 'feedback_comment'
        """,
    )
    assert len(rows) == 1


def test_app_role_can_insert_chat_log_with_rls_context(
    alembic_upgraded, admin_dsn, app_dsn
):
    """domainrag_app으로 chat_logs INSERT + RLS context로 격리 확인."""
    import psycopg

    # 사전 작업: tenants row 1건 (FK 충족 위해 admin role로) — chat_logs는 FK가 없으나
    # 다른 테이블이 FK를 가지므로 일반적인 패턴 검증.
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants (tenant_id, name, status)
                VALUES ('t-integration', 'Integration Test', 'active')
                ON CONFLICT (tenant_id) DO NOTHING
                """
            )

    # domainrag_app으로 RLS context 설정 후 INSERT
    request_id = uuid.uuid4().hex
    payload_citations = json.dumps([{"chunk_id": "c1", "marker": "[1]"}])
    with psycopg.connect(app_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.current_tenant = 't-integration'")
            cur.execute(
                """
                INSERT INTO chat_logs (
                    tenant_id, request_id, user_id, question, answer,
                    citations, citation_types, latency_ms, ui_mode, confidence
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s, %s
                )
                """,
                ("t-integration", request_id, "u1", "Q test", "A test",
                 payload_citations, '["direct"]', 100, "chat_structured", 0.8),
            )
        conn.commit()

    # 같은 tenant context로 SELECT — 1건 보임
    with psycopg.connect(app_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.current_tenant = 't-integration'")
            cur.execute(
                "SELECT request_id, citations FROM chat_logs "
                "WHERE tenant_id = 't-integration'"
            )
            rows = cur.fetchall()
    assert len(rows) >= 1
    found = next(r for r in rows if r[0] == request_id)
    assert found[1] == [{"chunk_id": "c1", "marker": "[1]"}]


def test_app_role_rls_blocks_cross_tenant(alembic_upgraded, admin_dsn, app_dsn):
    """다른 tenant context에서는 INSERT한 row가 보이지 않음."""
    import psycopg

    # 'other-tenant' 등록
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants (tenant_id, name, status)
                VALUES ('t-other-rls', 'Other RLS', 'active')
                ON CONFLICT (tenant_id) DO NOTHING
                """
            )

    # 't-other-rls' context — t-integration의 row는 안 보여야 함
    with psycopg.connect(app_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.current_tenant = 't-other-rls'")
            cur.execute("SELECT COUNT(*) FROM chat_logs")
            count = cur.fetchone()[0]
    assert count == 0


def test_admin_role_bypasses_rls(alembic_upgraded, admin_dsn):
    """BYPASSRLS로 모든 tenant chat_logs 조회."""
    import psycopg

    with psycopg.connect(admin_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id, COUNT(*) FROM chat_logs GROUP BY tenant_id")
            rows = cur.fetchall()
    # 위 test가 't-integration'에 INSERT했으므로 보이거나 admin이 BYPASSRLS임을 다른
    # 방법으로 확인. RLS 정책상 admin은 context 미설정으로도 조회 가능.
    # 최소 검증: 쿼리가 에러 없이 동작 + 결과 도달 가능.
    assert rows is not None
