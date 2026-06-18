"""assessment_items 멀티모달 컬럼 (ADR-025) — assets/figure_dependent

Revision ID: 014_assessment_multimodal
Revises: 013_assessment_item_cols
Create Date: 2026-06-18

ADR-025 §1 — 시험 문제 item에 이미지 자산(assets)과 그림 의존 표식(figure_dependent)을
추가한다. assets[i] = {asset_id, kind, storage_key, content_hash, source_page, bbox,
vlm_description}. 이미지 bytes는 MinIO(prefix-per-tenant, ADR-024 암호화)에 저장되고
본 컬럼은 참조 메타만 보관한다.
"""

from alembic import op

# revision id ≤ 32자 (alembic_version.version_num VARCHAR(32) 제약)
revision = "014_assessment_multimodal"
down_revision = "013_assessment_item_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE assessment_items
            ADD COLUMN IF NOT EXISTS assets JSONB DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS figure_dependent BOOLEAN DEFAULT FALSE;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE assessment_items
            DROP COLUMN IF EXISTS assets,
            DROP COLUMN IF EXISTS figure_dependent;
        """
    )
