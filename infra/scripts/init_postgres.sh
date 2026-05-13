#!/bin/bash
# ============================================================================
# DomainRAG Ops — PostgreSQL 초기화 (dev compose 전용)
# ============================================================================
# 운영(115번)에서는 본 스크립트 대신 ADR-019 §1·§2의 절차를 직접 적용.
# Dev compose는 postgres 컨테이너 첫 기동 시 본 스크립트가 자동 실행됨.

set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL

  -- ADR-019 §1: 별도 database
  CREATE DATABASE domainrag;

  -- ADR-019 §1: role 분리
  CREATE ROLE domainrag_app LOGIN PASSWORD 'changeme';
  CREATE ROLE domainrag_platform_admin LOGIN PASSWORD 'changeme_admin' BYPASSRLS;

  GRANT CONNECT ON DATABASE domainrag TO domainrag_app;
  GRANT CONNECT ON DATABASE domainrag TO domainrag_platform_admin;

EOSQL

# domainrag database 안에서 schema 권한 부여
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname domainrag <<-EOSQL

  GRANT USAGE, CREATE ON SCHEMA public TO domainrag_app;
  GRANT USAGE, CREATE ON SCHEMA public TO domainrag_platform_admin;

  -- 향후 모든 테이블에 두 role의 권한 자동 부여
  ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO domainrag_app;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON TABLES TO domainrag_platform_admin;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO domainrag_app;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON SEQUENCES TO domainrag_platform_admin;

  -- pgcrypto 확장 (UUID 등)
  CREATE EXTENSION IF NOT EXISTS pgcrypto;

EOSQL

echo "[init_postgres] domainrag database + roles 초기화 완료"
