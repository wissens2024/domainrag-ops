"""tenant_register_failures dead-letter (ADR-021 §5)

Revision ID: 010_tenant_register_failures
Revises: 009_notify_payload_4field
Create Date: 2026-05-15

ADR-021 §5 atomic rollback이 끝나도 *부분 성공 후 자동 회복 불가* 케이스는 dead-letter에
누적하여 platform_admin이 수동 정리할 수 있게 한다.

본 테이블은 hard_delete의 `tenant_delete_failures` (ADR-012 §6 + migration 006) 대칭.
"""

from alembic import op

revision = "010_tenant_register_failures"
down_revision = "009_notify_payload_4field"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tenant_register_failures (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id VARCHAR(64) NOT NULL,
            failed_step VARCHAR(50) NOT NULL,
            error_message TEXT,
            rollback_succeeded BOOLEAN DEFAULT FALSE,
            details JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            resolved_at TIMESTAMPTZ,
            resolved_by VARCHAR(255),
            resolution_note TEXT
        );
        CREATE INDEX idx_register_failures_unresolved
            ON tenant_register_failures (created_at DESC)
            WHERE resolved_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_register_failures;")
