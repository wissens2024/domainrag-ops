"""assessment_items 누락 컬럼 보강 (ADR-014) — quality_score/validator_results 등

Revision ID: 013_assessment_items_quality_columns
Revises: 012_rename_tenant_to_domain
Create Date: 2026-05-28

migration 001의 assessment_items는 quality_status까지만 생성했으나,
AssessmentItemRepository(list_by_tenant/upsert)는 quality_score·validator_results·
reference_item_ids·embedding_model·vector_id를 SELECT/INSERT한다. 컬럼 부재로
Item Bank 목록이 500(UndefinedColumnError). ADR-014 품질/벡터 메타 컬럼을 보강한다.
"""

from alembic import op

# revision id ≤ 32자 (alembic_version.version_num VARCHAR(32) 제약)
revision = "013_assessment_item_cols"
down_revision = "012_rename_tenant_to_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE assessment_items
            ADD COLUMN IF NOT EXISTS quality_score DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS validator_results JSONB DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS reference_item_ids JSONB DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(100),
            ADD COLUMN IF NOT EXISTS vector_id VARCHAR(255);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE assessment_items
            DROP COLUMN IF EXISTS quality_score,
            DROP COLUMN IF EXISTS validator_results,
            DROP COLUMN IF EXISTS reference_item_ids,
            DROP COLUMN IF EXISTS embedding_model,
            DROP COLUMN IF EXISTS vector_id;
        """
    )
