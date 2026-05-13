"""chat_logs.feedback_comment — ADR-017 §5 POST /feedback comment 저장.

Revision ID: 007_chat_logs_feedback_comment
Revises: 006_tenants_delete_failures
Create Date: 2026-05-13

chat_logs는 PARTITION BY RANGE (created_at) — 부모 테이블에 ADD COLUMN하면 모든
partition에 자동 전파된다 (PostgreSQL 11+).

feedback 컬럼은 001_initial_schema에 이미 VARCHAR(50)로 존재. comment는 TEXT NULL.
"""

from alembic import op

revision = "007_chat_logs_feedback_comment"
down_revision = "006_tenants_delete_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_logs ADD COLUMN feedback_comment TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE chat_logs DROP COLUMN IF EXISTS feedback_comment;")
