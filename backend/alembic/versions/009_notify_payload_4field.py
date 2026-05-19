"""notify_tenant_config_changed payload 4-field 정합 (ADR-021 §2)

Revision ID: 009_notify_payload_4field
Revises: 008_notify_schema_lifecycle
Create Date: 2026-05-15

migration 001이 만든 trigger는 payload가 `{tenant_id, category}` 2-필드. ADR-021 §2
표는 `{tenant_id, category, key, schema_version}` 4-필드를 약속한다. 본 마이그레이션이
trigger 함수를 CREATE OR REPLACE 로 갱신해 contract을 정합시킨다.

application path(`TenantConfigOverrideService.patch`)는 commit과 함께 명시 NOTIFY를
4-필드로 발사한다 — trigger는 *보조 안전망*이라 동일 payload 형식 의무.

`tenant_config_overrides` 컬럼 매핑:
- key      ← path (overrides 테이블의 path 컬럼)
- schema_version ← schema_version_at_write
"""

from alembic import op

revision = "009_notify_payload_4field"
down_revision = "008_notify_schema_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
        """
    )


def downgrade() -> None:
    # 2-필드 형태로 복원 (001 마이그레이션 원본)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_tenant_config_changed() RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify('tenant_config_changed',
                json_build_object('tenant_id', NEW.tenant_id, 'category', NEW.category)::text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
