"""notify_tenant_schema_changed + notify_tenant_lifecycle_changed triggers — ADR-021 §2.

Revision ID: 008_notify_schema_lifecycle
Revises: 007_chat_logs_feedback_comment
Create Date: 2026-05-15

ADR-009 §5는 LISTEN/NOTIFY를 결정했으나 channel/payload를 미정의했다. ADR-021 §2가
세 channel(tenant_config_changed/tenant_schema_changed/tenant_lifecycle_changed)을
결선한다.

001 마이그레이션이 tenant_config_changed 트리거를 이미 만들었다 — 본 마이그레이션은
나머지 두 채널의 trigger function + AFTER INSERT/UPDATE trigger를 추가한다.

application path가 NOTIFY를 보내는 단일 진실 소스이지만 DDL 트리거는 *보조 안전망*으로
보존한다(운영자가 raw SQL로 row를 변경했을 때도 NOTIFY 보장).
"""

from alembic import op

revision = "008_notify_schema_lifecycle"
down_revision = "007_chat_logs_feedback_comment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------------
    # tenant_input_schemas — INSERT 시 새 active version NOTIFY
    # ------------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_tenant_schema_changed() RETURNS TRIGGER AS $$
        BEGIN
            -- 새 active row 진입 시에만 발사 (deprecated 전이는 별도 발사 안 함 — 같은
            -- 트랜잭션에서 INSERT new active가 같이 일어나므로 cumulative 효과 동일).
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

        CREATE TRIGGER trg_tenant_schema_changed
          AFTER INSERT OR UPDATE OF status ON tenant_input_schemas
          FOR EACH ROW EXECUTE FUNCTION notify_tenant_schema_changed();
        """
    )

    # ------------------------------------------------------------------------
    # tenants — status 전이 또는 INSERT 시 NOTIFY
    # ------------------------------------------------------------------------
    op.execute(
        """
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

        CREATE TRIGGER trg_tenant_lifecycle_changed
          AFTER INSERT OR UPDATE OF status ON tenants
          FOR EACH ROW EXECUTE FUNCTION notify_tenant_lifecycle_changed();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tenant_lifecycle_changed ON tenants;")
    op.execute("DROP TRIGGER IF EXISTS trg_tenant_schema_changed ON tenant_input_schemas;")
    op.execute("DROP FUNCTION IF EXISTS notify_tenant_lifecycle_changed() CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS notify_tenant_schema_changed() CASCADE;")
