"""indexing_jobs — doc_version + active-job 유니크 제약 (ADR-012 §10).

Revision ID: 002_indexing_jobs_doc_version
Revises: 001_initial_schema
Create Date: 2026-05-12

ADR-012 §10 — 같은 (tenant_id, doc_id, doc_version)에서 active(pending/parsing/
chunking/embedding/indexing) 상태의 job은 동시에 1건만 존재 가능. Migration 001은
doc_version 컬럼 자체가 누락되어 있어 본 마이그레이션에서 컬럼 추가 + 부분 유니크
인덱스로 race condition을 DB 차원에서 차단한다.
"""

from alembic import op

# revision identifiers
revision = "002_indexing_jobs_doc_version"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE indexing_jobs ADD COLUMN doc_version VARCHAR(50);")
    # 부분 유니크 — active 상태 한정
    op.execute("""
        CREATE UNIQUE INDEX uq_indexing_jobs_active
          ON indexing_jobs (tenant_id, doc_id, doc_version)
          WHERE status IN ('pending', 'parsing', 'chunking', 'embedding', 'indexing');
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_indexing_jobs_active;")
    op.execute("ALTER TABLE indexing_jobs DROP COLUMN IF EXISTS doc_version;")
