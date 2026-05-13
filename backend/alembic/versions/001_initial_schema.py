"""initial schema — multi-tenant + RLS + chat_logs partitioning

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-05-08

ADR 정합:
  - ADR-008: tenant_id 1차 분기 + RLS + collection-per-tenant
  - ADR-009: tenant_config_overrides + audit
  - ADR-012: chunks·documents·indexing_jobs lifecycle 컬럼
  - ADR-014: assessment_items + assessment_logs
  - ADR-015: tenant_input_schemas
  - ADR-018: user_tenant_membership
  - ADR-019: chat_logs partitioning, RLS 인덱스 가이드라인
  - ADR-020: chat_logs PII 컬럼
"""

from alembic import op

# revision identifiers
revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------------
    # tenants — ADR-008
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE tenants (
        tenant_id VARCHAR(64) PRIMARY KEY,
        display_name TEXT NOT NULL,
        domain_type VARCHAR(50),
        embedding_model VARCHAR(100) DEFAULT 'bge-m3',
        embedding_migration_state VARCHAR(20) DEFAULT 'idle',
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        modules JSONB DEFAULT '["rag"]',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX idx_tenants_status ON tenants (status);
    """)

    # ------------------------------------------------------------------------
    # user_tenant_membership — ADR-018 §4
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE user_tenant_membership (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id VARCHAR(255) NOT NULL,
        tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        clearance VARCHAR(50) NOT NULL DEFAULT 'internal',
        department VARCHAR(100),
        domain_groups JSONB DEFAULT '[]',
        is_active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (user_id, tenant_id)
    );
    CREATE INDEX idx_user_tenant ON user_tenant_membership (user_id, tenant_id) WHERE is_active = TRUE;
    """)

    # ------------------------------------------------------------------------
    # documents — ADR-001/012
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE documents (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        doc_id VARCHAR(255) NOT NULL,
        title TEXT NOT NULL,
        input_type VARCHAR(100),
        source_type VARCHAR(50),
        source_path TEXT,
        object_storage_path TEXT,
        department VARCHAR(100),
        doc_type VARCHAR(100),
        security_level VARCHAR(50) DEFAULT 'internal',
        version VARCHAR(50) DEFAULT 'v1',
        owner VARCHAR(255),
        tags JSONB DEFAULT '[]',
        language VARCHAR(20) DEFAULT 'ko',
        valid_from DATE,
        valid_until DATE,
        approval_status VARCHAR(50) DEFAULT 'draft',
        file_hash VARCHAR(128),
        parser_version VARCHAR(50) DEFAULT 'p1',
        metadata JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (tenant_id, doc_id, version)
    );
    CREATE INDEX idx_documents_tenant ON documents (tenant_id, approval_status, valid_until);
    CREATE INDEX idx_documents_metadata ON documents USING GIN (metadata);
    """)

    # ------------------------------------------------------------------------
    # chunks — ADR-007/012
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE chunks (
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
        UNIQUE (tenant_id, doc_id, doc_version, parser_version, chunk_index)
    );
    CREATE INDEX idx_chunks_tenant_doc ON chunks (tenant_id, doc_id, doc_version, parser_version);
    CREATE INDEX idx_chunks_valid ON chunks (tenant_id, valid_until) WHERE valid_until IS NOT NULL;
    CREATE INDEX idx_chunks_approval ON chunks (tenant_id, approval_status);
    """)

    # ------------------------------------------------------------------------
    # conversations / messages
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE conversations (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        user_id VARCHAR(255) NOT NULL,
        title TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX idx_conversations_tenant_user ON conversations (tenant_id, user_id);

    CREATE TABLE messages (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL,
        conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role VARCHAR(50) NOT NULL,
        content TEXT NOT NULL,
        citations JSONB DEFAULT '[]',
        metadata JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX idx_messages_tenant_conv ON messages (tenant_id, conversation_id, created_at);
    """)

    # ------------------------------------------------------------------------
    # chat_logs — month partitioning (ADR-019 §2)
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE chat_logs (
        id UUID DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL,
        request_id VARCHAR(255) NOT NULL,
        user_id VARCHAR(255),
        conversation_id UUID,
        question TEXT NOT NULL,
        rewritten_query TEXT,
        answer TEXT,
        retrieved_chunks JSONB DEFAULT '[]',
        citations JSONB DEFAULT '[]',
        citation_types JSONB DEFAULT '[]',
        llm_model VARCHAR(100),
        embedding_model VARCHAR(100),
        reranker_model VARCHAR(100),
        prompt_version VARCHAR(100),
        latency_ms INT,
        feedback VARCHAR(50),
        ui_mode VARCHAR(20),
        confidence FLOAT,
        fallback_reason VARCHAR(50),
        unsupported_ratio FLOAT,
        verifier_metrics JSONB DEFAULT '{}',
        routing_decision JSONB DEFAULT '{}',
        classifier_decision JSONB DEFAULT '{}',
        model_failure_chain JSONB DEFAULT '[]',
        inference_judge_results JSONB DEFAULT '[]',
        conflict_groups JSONB DEFAULT '[]',
        input_pii_found JSONB DEFAULT '[]',
        output_pii_masked JSONB DEFAULT '[]',
        pii_storage_policy VARCHAR(20) DEFAULT 'mask',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (id, created_at)
    ) PARTITION BY RANGE (created_at);

    CREATE INDEX idx_chat_logs_tenant_time ON chat_logs (tenant_id, created_at DESC);
    CREATE INDEX idx_chat_logs_request ON chat_logs (tenant_id, request_id);

    -- 첫 partition (현재월) — pgcron 또는 운영 cron으로 매월 추가
    CREATE TABLE chat_logs_y2026m05 PARTITION OF chat_logs
      FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
    """)

    # ------------------------------------------------------------------------
    # indexing_jobs — ADR-007/012/019
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE indexing_jobs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL,
        job_id VARCHAR(255) NOT NULL UNIQUE,
        doc_id VARCHAR(255),
        filename TEXT,
        status VARCHAR(50) NOT NULL,
        progress INT DEFAULT 0,
        step VARCHAR(100),
        error_message TEXT,
        total_chunks INT DEFAULT 0,
        indexed_chunks INT DEFAULT 0,
        failed_chunks JSONB DEFAULT '[]',
        retry_count INT DEFAULT 0,
        failure_rate FLOAT,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX idx_indexing_tenant_status ON indexing_jobs (tenant_id, status, created_at DESC);
    """)

    # ------------------------------------------------------------------------
    # tenant_config_overrides — ADR-009
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE tenant_config_overrides (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        category VARCHAR(50) NOT NULL,
        path TEXT NOT NULL,
        value JSONB NOT NULL,
        schema_version_at_write INT NOT NULL DEFAULT 1,
        reason TEXT,
        author VARCHAR(255),
        active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (tenant_id, category, path) DEFERRABLE INITIALLY DEFERRED
    );

    CREATE TABLE tenant_config_change_logs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL,
        category VARCHAR(50) NOT NULL,
        path TEXT NOT NULL,
        old_value JSONB,
        new_value JSONB,
        author VARCHAR(255),
        reason TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX idx_config_changes_tenant_time ON tenant_config_change_logs (tenant_id, created_at DESC);
    """)

    # ------------------------------------------------------------------------
    # tenant_input_schemas — ADR-015
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE tenant_input_schemas (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        schema_version INT NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        schema_yaml JSONB NOT NULL,
        ui_schema_yaml JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        deprecated_at TIMESTAMPTZ,
        UNIQUE (tenant_id, schema_version)
    );
    """)

    # ------------------------------------------------------------------------
    # tenant_lifecycle_logs — ADR-012
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE tenant_lifecycle_logs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL,
        action VARCHAR(50) NOT NULL,
        from_state VARCHAR(20),
        to_state VARCHAR(20),
        actor VARCHAR(255),
        reason TEXT,
        details JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX idx_lifecycle_tenant_time ON tenant_lifecycle_logs (tenant_id, created_at DESC);
    """)

    # ------------------------------------------------------------------------
    # adapter_registry — ADR-013
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE adapter_registry (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        adapter_id VARCHAR(255) NOT NULL UNIQUE,
        version VARCHAR(50),
        base_model VARCHAR(100),
        keyhub_secret_ref TEXT,
        status VARCHAR(20) DEFAULT 'registered',
        training_metadata JSONB DEFAULT '{}',
        registered_at TIMESTAMPTZ DEFAULT NOW(),
        activated_at TIMESTAMPTZ,
        retired_at TIMESTAMPTZ
    );
    """)

    # ------------------------------------------------------------------------
    # assessment_items + assessment_logs — ADR-014 (assessment 모듈 활성 시)
    # ------------------------------------------------------------------------
    op.execute("""
    CREATE TABLE assessment_items (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        item_id VARCHAR(255) NOT NULL,
        subject VARCHAR(100),
        chapter VARCHAR(100),
        difficulty VARCHAR(20),
        question_type VARCHAR(50),
        question_text TEXT NOT NULL,
        choices JSONB,
        answer TEXT,
        explanation TEXT,
        tags JSONB DEFAULT '[]',
        used_count INT DEFAULT 0,
        last_used_at TIMESTAMPTZ,
        quality_status VARCHAR(20) DEFAULT 'draft',
        source VARCHAR(50),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (tenant_id, item_id)
    );
    CREATE INDEX idx_items_tenant_status ON assessment_items (tenant_id, quality_status);
    CREATE INDEX idx_items_tenant_subject ON assessment_items (tenant_id, subject, chapter, difficulty);

    CREATE TABLE assessment_logs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(64) NOT NULL,
        request_id VARCHAR(255) NOT NULL,
        action VARCHAR(50) NOT NULL,
        criteria JSONB,
        result_summary JSONB,
        validator_summary JSONB,
        similarity_results JSONB,
        latency_ms INT,
        actor VARCHAR(255),
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX idx_assessment_logs_tenant_time ON assessment_logs (tenant_id, created_at DESC);
    """)

    # ------------------------------------------------------------------------
    # RLS — ADR-008/019
    # ------------------------------------------------------------------------
    rls_tables = [
        "user_tenant_membership",
        "documents",
        "chunks",
        "conversations",
        "messages",
        "chat_logs",
        "indexing_jobs",
        "tenant_config_overrides",
        "tenant_config_change_logs",
        "tenant_input_schemas",
        "tenant_lifecycle_logs",
        "adapter_registry",
        "assessment_items",
        "assessment_logs",
    ]
    for tbl in rls_tables:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {tbl}
              USING (tenant_id = current_setting('app.current_tenant', true));
        """)

    # tenants 테이블 자체는 platform_admin이 cross-tenant로 다루므로 RLS 미적용.

    # LISTEN/NOTIFY for tenant_config_changed
    op.execute("""
    CREATE OR REPLACE FUNCTION notify_tenant_config_changed() RETURNS TRIGGER AS $$
    BEGIN
        PERFORM pg_notify('tenant_config_changed',
            json_build_object('tenant_id', NEW.tenant_id, 'category', NEW.category)::text);
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_tenant_config_changed
      AFTER INSERT OR UPDATE ON tenant_config_overrides
      FOR EACH ROW EXECUTE FUNCTION notify_tenant_config_changed();
    """)


def downgrade() -> None:
    # 운영에서 downgrade는 거의 안 함. 골격 단계는 drop all.
    op.execute("DROP TABLE IF EXISTS assessment_logs CASCADE;")
    op.execute("DROP TABLE IF EXISTS assessment_items CASCADE;")
    op.execute("DROP TABLE IF EXISTS adapter_registry CASCADE;")
    op.execute("DROP TABLE IF EXISTS tenant_lifecycle_logs CASCADE;")
    op.execute("DROP TABLE IF EXISTS tenant_input_schemas CASCADE;")
    op.execute("DROP TABLE IF EXISTS tenant_config_change_logs CASCADE;")
    op.execute("DROP TABLE IF EXISTS tenant_config_overrides CASCADE;")
    op.execute("DROP TABLE IF EXISTS indexing_jobs CASCADE;")
    op.execute("DROP TABLE IF EXISTS chat_logs CASCADE;")
    op.execute("DROP TABLE IF EXISTS messages CASCADE;")
    op.execute("DROP TABLE IF EXISTS conversations CASCADE;")
    op.execute("DROP TABLE IF EXISTS chunks CASCADE;")
    op.execute("DROP TABLE IF EXISTS documents CASCADE;")
    op.execute("DROP TABLE IF EXISTS user_tenant_membership CASCADE;")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS notify_tenant_config_changed() CASCADE;")
