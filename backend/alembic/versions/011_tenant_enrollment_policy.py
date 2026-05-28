"""tenant enrollment_policy (ADR-022 §4)

Revision ID: 011_tenant_enrollment_policy
Revises: 010_tenant_register_failures
Create Date: 2026-05-28

ADR-022 §4 — 도메인별 가입 정책. open = 인증된 모든 user 자동 접근(JIT membership),
assigned = 관리자 명시 배정만. 기본값 assigned (폐쇄·전문 도메인 안전 기본).
기본 도메인(general)만 운영에서 open으로 전환한다.
"""

from alembic import op

revision = "011_tenant_enrollment_policy"
down_revision = "010_tenant_register_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tenants
            ADD COLUMN IF NOT EXISTS enrollment_policy VARCHAR(20)
            NOT NULL DEFAULT 'assigned';
        -- 기본 도메인이 이미 존재하면 open으로 전환 (없으면 no-op).
        UPDATE tenants SET enrollment_policy = 'open' WHERE tenant_id = 'general';
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS enrollment_policy;")
