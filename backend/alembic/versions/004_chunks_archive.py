"""chunks_archive — ADR-012 §3 chunks archival 정책.

Revision ID: 004_chunks_archive
Revises: 003_pii_storage_approvals
Create Date: 2026-05-12

1. chunks 테이블에 `archived_at TIMESTAMPTZ NULL` 추가 + 부분 인덱스(archived_at IS NULL인
   active chunk lookup용)
2. chunks_archive 테이블 신설 — chunks와 동일 컬럼 구조 + archived_at NOT NULL.
   RLS 동일 적용(tenant_isolation policy).
3. (tenant_id, valid_until) 부분 인덱스 — archival_worker가 90일 이전 후보를 빠르게 스캔
"""

from alembic import op

# revision identifiers
revision = "004_chunks_archive"
down_revision = "003_pii_storage_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) chunks.archived_at 컬럼 + 부분 인덱스
    op.execute("ALTER TABLE chunks ADD COLUMN archived_at TIMESTAMPTZ;")
    op.execute(
        """
        CREATE INDEX idx_chunks_archival_candidates
          ON chunks (tenant_id, valid_until)
         WHERE archived_at IS NULL AND valid_until IS NOT NULL;
        """
    )

    # 2) chunks_archive — chunks와 동일 스키마 (archived_at NOT NULL)
    op.execute(
        """
        CREATE TABLE chunks_archive (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            chunk_id VARCHAR(512) NOT NULL,
            doc_id VARCHAR(255) NOT NULL,
            doc_version VARCHAR(50),
            parser_version VARCHAR(50) DEFAULT 'p1',
            chunk_strategy VARCHAR(50),
            chunk_index INT NOT NULL,
            title TEXT,
            page_number INT,
            section_title TEXT,
            heading_path JSONB DEFAULT '[]',
            content TEXT NOT NULL,
            content_hash VARCHAR(128),
            start_char INT,
            end_char INT,
            token_count INT,
            department VARCHAR(100),
            doc_type VARCHAR(100),
            security_level VARCHAR(50),
            acl JSONB DEFAULT '[]',
            approval_status VARCHAR(50) DEFAULT 'draft',
            tags JSONB DEFAULT '[]',
            valid_from DATE,
            valid_until DATE,
            embedding_model VARCHAR(100),
            embedding_version VARCHAR(100),
            vector_id VARCHAR(255),
            pii_warnings JSONB DEFAULT '[]',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            archive_reason VARCHAR(100) DEFAULT 'valid_until_threshold',
            UNIQUE (tenant_id, chunk_id, archived_at)
        );
        CREATE INDEX idx_chunks_archive_tenant_doc
          ON chunks_archive (tenant_id, doc_id, doc_version);
        """
    )
    op.execute("ALTER TABLE chunks_archive ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON chunks_archive
          USING (tenant_id = current_setting('app.current_tenant', true));
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chunks_archive CASCADE;")
    op.execute("DROP INDEX IF EXISTS idx_chunks_archival_candidates;")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS archived_at;")
