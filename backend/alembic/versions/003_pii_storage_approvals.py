"""pii_storage_approvals — platform_admin이 승인한 plain 보관 정책 추적 (ADR-020 §4·§8).

Revision ID: 003_pii_storage_approvals
Revises: 002_indexing_jobs_doc_version
Create Date: 2026-05-12

ADR-020 §4 — chat_logs.pii_storage_policy='plain'은 platform_admin 명시 승인 시에만
허용된다. 본 테이블은 (tenant_id) 단위로 활성 승인을 1건만 갖도록 부분 유니크 인덱스를
적용해 race 없이 정책을 평가할 수 있게 한다. 승인 / 만료 / 회수는 별도 row 갱신으로
표시하고, 모든 변경은 ADR-020 §8 audit ledger 대상이다.

RLS 미적용 — platform_admin이 관리하는 cross-tenant 메타이며 admin engine으로 접근한다.
"""

from alembic import op

# revision identifiers
revision = "003_pii_storage_approvals"
down_revision = "002_indexing_jobs_doc_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE pii_storage_approvals (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        policy VARCHAR(20) NOT NULL DEFAULT 'plain',
        reason TEXT NOT NULL,
        approved_by VARCHAR(255) NOT NULL,
        valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        valid_until TIMESTAMPTZ,
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        revoked_at TIMESTAMPTZ,
        revoked_by VARCHAR(255),
        revoke_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- 같은 tenant에 active 승인은 1건만
    CREATE UNIQUE INDEX uq_pii_storage_approvals_active
      ON pii_storage_approvals (tenant_id)
      WHERE status = 'active';

    CREATE INDEX idx_pii_storage_approvals_tenant_status
      ON pii_storage_approvals (tenant_id, status, created_at DESC);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pii_storage_approvals CASCADE;")
