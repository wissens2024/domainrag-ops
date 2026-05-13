#!/bin/bash
# ============================================================================
# Tenant 단위 restore (ADR-012)
# ============================================================================
# 주의: 같은 tenant_id가 이미 존재하면 충돌 — 먼저 archived 상태인지 확인.

set -euo pipefail

BACKUP_DIR="${1:?backup directory 필수}"
TENANT_ID="${2:?tenant_id 필수}"

echo "[restore] tenant=$TENANT_ID ← $BACKUP_DIR"

# 사전 검증: tenant 상태가 archived 또는 미존재여야 함
STATUS=$(psql "${POSTGRES_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}" \
  -t -A -c "SELECT status FROM tenants WHERE tenant_id='$TENANT_ID'" 2>/dev/null || true)

if [[ -n "$STATUS" && "$STATUS" != "archived" ]]; then
  echo "[restore] ERROR: tenant 상태가 '$STATUS' — restore는 archived 또는 미존재 상태에서만 가능"
  exit 1
fi

# 1) PostgreSQL restore
for tbl in tenants user_tenant_membership documents chunks conversations messages chat_logs \
           indexing_jobs tenant_config_overrides tenant_input_schemas tenant_lifecycle_logs \
           assessment_items assessment_logs; do
  if [[ -f "$BACKUP_DIR/$tbl.csv" ]]; then
    psql "${POSTGRES_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}" \
      -c "DELETE FROM $tbl WHERE tenant_id='$TENANT_ID'" 2>/dev/null || true
    psql "${POSTGRES_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}" \
      -c "\COPY $tbl FROM '$BACKUP_DIR/$tbl.csv' CSV HEADER"
  fi
done

# 2) Qdrant: snapshot으로 복원 (수동 admin API)
echo "[restore] Qdrant snapshot 복원은 platform_admin이 별도 API 호출 필요"
echo "  POST $QDRANT_URL/collections/chunks_$TENANT_ID/snapshots/recover"

# 3) MinIO
mc mirror "$BACKUP_DIR/minio/" "domainrag/${MINIO_BUCKET:-domainrag}/$TENANT_ID/" 2>/dev/null || true

# 4) configs
if [[ -d "$BACKUP_DIR/configs" ]]; then
  cp -rn "$BACKUP_DIR/configs/." "configs/tenants/$TENANT_ID/"
fi

# 5) tenant 상태를 active로 전이
psql "${POSTGRES_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}" \
  -c "UPDATE tenants SET status='active', updated_at=NOW() WHERE tenant_id='$TENANT_ID'"

echo "[restore] 완료"
