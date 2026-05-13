"""tenants delete_status + tenant_delete_failures — ADR-012 §6.

Revision ID: 006_tenants_delete_failures
Revises: 005_evaluation_jobs
Create Date: 2026-05-13

tenants.delete_status / deleted_at 컬럼 추가 + tenant_delete_failures 테이블 신설.
hard_delete cross-system 일관성의 dead-letter 큐. RLS 미적용 (platform_admin 전용 메타).
"""

from alembic import op

# revision identifiers
revision = "006_tenants_delete_failures"
down_revision = "005_evaluation_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN delete_status VARCHAR(20);")
    op.execute("ALTER TABLE tenants ADD COLUMN deleted_at TIMESTAMPTZ;")

    op.execute(
        """
        CREATE TABLE tenant_delete_failures (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id VARCHAR(64) NOT NULL,
            failed_step VARCHAR(50) NOT NULL,
            error_message TEXT,
            retry_count INT NOT NULL DEFAULT 0,
            last_attempt_at TIMESTAMPTZ DEFAULT NOW(),
            resolved_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_tenant_delete_failures_unresolved
          ON tenant_delete_failures (tenant_id, last_attempt_at DESC)
         WHERE resolved_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_delete_failures CASCADE;")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS deleted_at;")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS delete_status;")
