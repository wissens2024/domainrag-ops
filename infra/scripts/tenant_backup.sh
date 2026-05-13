#!/bin/bash
# ============================================================================
# Tenant 단위 backup (ADR-012)
# ============================================================================
set -euo pipefail

TENANT_ID="${1:?tenant_id 필수}"
BACKUP_DIR="${BACKUP_DIR:-./backups/$(date +%Y%m%d_%H%M%S)_$TENANT_ID}"

mkdir -p "$BACKUP_DIR"

echo "[backup] tenant=$TENANT_ID → $BACKUP_DIR"

# 1) PostgreSQL: tenant_id 필터한 모든 핵심 테이블 dump
psql "${POSTGRES_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}" \
  -c "COPY (SELECT * FROM tenants WHERE tenant_id='$TENANT_ID') TO '$BACKUP_DIR/tenants.csv' CSV HEADER"

for tbl in user_tenant_membership documents chunks conversations messages chat_logs indexing_jobs \
           tenant_config_overrides tenant_input_schemas tenant_lifecycle_logs assessment_items assessment_logs; do
  psql "${POSTGRES_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}" \
    -c "COPY (SELECT * FROM $tbl WHERE tenant_id='$TENANT_ID') TO '$BACKUP_DIR/$tbl.csv' CSV HEADER" \
    2>/dev/null || echo "  (skip $tbl — 테이블 없음 또는 미설치 모듈)"
done

# 2) Qdrant: collection snapshot
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
curl -s -X POST "$QDRANT_URL/collections/chunks_$TENANT_ID/snapshots" -o /dev/null
curl -s "$QDRANT_URL/collections/chunks_$TENANT_ID/snapshots" > "$BACKUP_DIR/qdrant_chunks_snapshots.json"
curl -s -X POST "$QDRANT_URL/collections/items_$TENANT_ID/snapshots" -o /dev/null 2>/dev/null || true

# 3) MinIO: tenant prefix 전체 export
mc mirror "domainrag/${MINIO_BUCKET:-domainrag}/$TENANT_ID/" "$BACKUP_DIR/minio/" 2>/dev/null || true

# 4) configs/tenants/<tenant_id>/ 사본
cp -r "configs/tenants/$TENANT_ID" "$BACKUP_DIR/configs/" 2>/dev/null || true

echo "[backup] 완료 → $BACKUP_DIR"
