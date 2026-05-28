"""tenant_id → domain_id 전면 개명 + RLS GUC app.current_tenant → app.current_domain (ADR-022 §8)

Revision ID: 012_rename_tenant_to_domain
Revises: 011_tenant_enrollment_policy
Create Date: 2026-05-28

ADR-022 §1·§8 — 사용자에겐 "도메인", 내부 키도 domain_id로 통일. 코드·rag_core·프론트는
별도 개명되었고, 본 마이그레이션이 DB 스키마를 정합시킨다:
  1) 모든 테이블의 tenant_id 컬럼 → domain_id (FK 참조는 RENAME COLUMN이 자동 갱신)
  2) RLS tenant_isolation 정책 전부 재생성 — domain_id + GUC app.current_domain
  3) NOTIFY trigger 함수 3종 재생성 — payload key 'domain_id' + NEW.domain_id

개발 단계(실데이터 없음) 전제이나, 데이터가 있어도 RENAME은 보존적이다.
"""

from alembic import op

revision = "012_rename_tenant_to_domain"
down_revision = "011_tenant_enrollment_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) 모든 tenant_id 컬럼 → domain_id (tenants PK 포함; FK·인덱스 참조 자동 갱신)
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT table_name FROM information_schema.columns
                 WHERE column_name = 'tenant_id' AND table_schema = 'public'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I RENAME COLUMN tenant_id TO domain_id', r.table_name
                );
            END LOOP;
        END $$;
        """
    )

    # 2) RLS tenant_isolation 정책 재생성 — domain_id + app.current_domain GUC
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation'
            LOOP
                EXECUTE format('DROP POLICY tenant_isolation ON %I', r.tablename);
                EXECUTE format(
                    'CREATE POLICY tenant_isolation ON %I '
                    'USING (domain_id = current_setting(''app.current_domain'', true))',
                    r.tablename
                );
            END LOOP;
        END $$;
        """
    )

    # 3) NOTIFY trigger 함수 재생성 — payload key 'domain_id' + NEW.domain_id
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_tenant_config_changed() RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify(
                'tenant_config_changed',
                json_build_object(
                    'domain_id', NEW.domain_id,
                    'category', NEW.category,
                    'key', NEW.path,
                    'schema_version', NEW.schema_version_at_write
                )::text
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION notify_tenant_schema_changed() RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.status = 'active' THEN
                PERFORM pg_notify(
                    'tenant_schema_changed',
                    json_build_object(
                        'domain_id', NEW.domain_id,
                        'schema_version', NEW.schema_version
                    )::text
                );
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION notify_tenant_lifecycle_changed() RETURNS TRIGGER AS $$
        DECLARE
            old_status TEXT;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                old_status := OLD.status;
            ELSE
                old_status := NULL;
            END IF;
            PERFORM pg_notify(
                'tenant_lifecycle_changed',
                json_build_object(
                    'domain_id', NEW.domain_id,
                    'old_status', old_status,
                    'new_status', NEW.status
                )::text
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    # 역순: domain_id → tenant_id, 정책·함수 원복
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT table_name FROM information_schema.columns
                 WHERE column_name = 'domain_id' AND table_schema = 'public'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I RENAME COLUMN domain_id TO tenant_id', r.table_name
                );
            END LOOP;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation'
            LOOP
                EXECUTE format('DROP POLICY tenant_isolation ON %I', r.tablename);
                EXECUTE format(
                    'CREATE POLICY tenant_isolation ON %I '
                    'USING (tenant_id = current_setting(''app.current_tenant'', true))',
                    r.tablename
                );
            END LOOP;
        END $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_tenant_config_changed() RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify(
                'tenant_config_changed',
                json_build_object(
                    'tenant_id', NEW.tenant_id,
                    'category', NEW.category,
                    'key', NEW.path,
                    'schema_version', NEW.schema_version_at_write
                )::text
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION notify_tenant_schema_changed() RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.status = 'active' THEN
                PERFORM pg_notify(
                    'tenant_schema_changed',
                    json_build_object(
                        'tenant_id', NEW.tenant_id,
                        'schema_version', NEW.schema_version
                    )::text
                );
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION notify_tenant_lifecycle_changed() RETURNS TRIGGER AS $$
        DECLARE
            old_status TEXT;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                old_status := OLD.status;
            ELSE
                old_status := NULL;
            END IF;
            PERFORM pg_notify(
                'tenant_lifecycle_changed',
                json_build_object(
                    'tenant_id', NEW.tenant_id,
                    'old_status', old_status,
                    'new_status', NEW.status
                )::text
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
