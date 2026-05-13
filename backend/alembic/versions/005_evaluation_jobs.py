"""evaluation_jobs — ADR-009 §7 + ADR-017 §16. 평가 실행 jobs.

Revision ID: 005_evaluation_jobs
Revises: 004_chunks_archive
Create Date: 2026-05-12

평가 실행은 비동기 background task. 본 테이블이 job lifecycle(pending/running/completed/
failed/promoted)을 추적하고 결과 summary/gate_result를 JSONB로 보관한다. RLS 적용 —
각 tenant는 자기 jobs만 조회 가능.
"""

from alembic import op

# revision identifiers
revision = "005_evaluation_jobs"
down_revision = "004_chunks_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE evaluation_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id VARCHAR(255) NOT NULL UNIQUE,
            tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            dataset_name VARCHAR(255) NOT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'pending',
            actor VARCHAR(255),
            config_override JSONB DEFAULT '{}',
            summary JSONB DEFAULT '{}',
            gate_result JSONB DEFAULT '{}',
            error_message TEXT,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            promoted_at TIMESTAMPTZ,
            promoted_by VARCHAR(255),
            promotion_target VARCHAR(100),
            promotion_version VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_evaluation_jobs_tenant_status
          ON evaluation_jobs (tenant_id, status, created_at DESC);
        """
    )
    op.execute("ALTER TABLE evaluation_jobs ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON evaluation_jobs
          USING (tenant_id = current_setting('app.current_tenant', true));
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS evaluation_jobs CASCADE;")
