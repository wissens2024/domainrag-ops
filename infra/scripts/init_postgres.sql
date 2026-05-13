-- ============================================================================
-- DomainRAG Ops — PostgreSQL Bootstrap (ADR-019 §2 two-role RLS)
-- ============================================================================
-- 본 스크립트는 docker-entrypoint-initdb.d로 실행되며 PostgreSQL 컨테이너의 superuser
-- 컨텍스트에서 1회만 동작한다. 책임:
--   1. 필수 extension(pgcrypto for gen_random_uuid)
--   2. 두 운영 role 생성 (domainrag_app / domainrag_platform_admin)
--   3. 권한 부여 (CONNECT/USAGE/SCHEMA grant)
--
-- 테이블·인덱스·RLS 정책은 alembic migrations (001~007)이 책임. 본 파일은 alembic이
-- 적용되기 전 prerequisites만 정리한다.
--
-- 환경변수:
--   POSTGRES_USER, POSTGRES_PASSWORD — docker-compose가 PostgreSQL 컨테이너에 superuser로
--     주입한 계정. 본 스크립트는 superuser 컨텍스트에서 실행된다.
--   APP_PASSWORD, ADMIN_PASSWORD — `\set` 또는 환경변수로 받아도 좋지만 본 스크립트는
--     하드코딩 placeholder. 운영 배포는 secret 관리 도구(KeyHub)로 password 주입 후
--     실행 직전 치환 권장.

\set ON_ERROR_STOP on

-- ----------------------------------------------------------------------------
-- 1. Extensions
-- ----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- ILIKE 가속 (선택)

-- ----------------------------------------------------------------------------
-- 2. Two operational roles (ADR-019 §2)
-- ----------------------------------------------------------------------------
-- domainrag_app: 일반 read/write. RLS 자동 적용. 모든 endpoint·서비스의 기본 세션.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'domainrag_app') THEN
        CREATE ROLE domainrag_app WITH
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            INHERIT
            NOREPLICATION
            PASSWORD 'changeme';
    END IF;
END
$$;

-- domainrag_platform_admin: cross-tenant 분석·audit·hard_delete·migration용. BYPASSRLS.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'domainrag_platform_admin') THEN
        CREATE ROLE domainrag_platform_admin WITH
            LOGIN
            NOSUPERUSER
            CREATEDB
            CREATEROLE
            BYPASSRLS
            INHERIT
            NOREPLICATION
            PASSWORD 'changeme_admin';
    END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- 3. Database grants
-- ----------------------------------------------------------------------------
-- POSTGRES_DB(현 docker-compose default: domainrag)에 대해 두 role에 CONNECT 허용.
GRANT CONNECT ON DATABASE domainrag TO domainrag_app;
GRANT CONNECT ON DATABASE domainrag TO domainrag_platform_admin;

-- public schema 권한 (alembic이 테이블을 public에 생성)
GRANT USAGE ON SCHEMA public TO domainrag_app;
GRANT USAGE ON SCHEMA public TO domainrag_platform_admin;

-- 향후 생성되는 테이블에 대한 기본 권한 (alembic이 superuser로 CREATE TABLE 실행)
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO domainrag_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO domainrag_platform_admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO domainrag_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO domainrag_platform_admin;

-- 이미 생성된 테이블(향후 마이그레이션 재실행 시)에도 권한 부여
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA public TO domainrag_app;
GRANT ALL PRIVILEGES
    ON ALL TABLES IN SCHEMA public TO domainrag_platform_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO domainrag_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO domainrag_platform_admin;
