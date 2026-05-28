#!/bin/bash
# ============================================================================
# DomainRAG Ops — Tenant 단위 backup (ADR-012 §8 + ADR-021 §5)
# ============================================================================
# 출력 구조 (tenant_restore.sh와 정합):
#   $BACKUP_DIR/
#     manifest.json         (artifacts + sha256)
#     postgres/<table>.csv
#     qdrant/<coll>.snapshot
#     minio/                (mc mirror)
#     configs/              (configs/tenants/<id>/ 사본)
#
# 사용법:
#   ./tenant_backup.sh <domain_id> [BACKUP_DIR=path]
# ============================================================================

set -euo pipefail

TENANT_ID="${1:?domain_id 필수}"
BACKUP_DIR="${BACKUP_DIR:-./backups/$(date +%Y%m%d_%H%M%S)_$TENANT_ID}"

mkdir -p "$BACKUP_DIR/postgres" "$BACKUP_DIR/qdrant" "$BACKUP_DIR/minio" "$BACKUP_DIR/configs"

PG_ADMIN_DSN="${POSTGRES_ADMIN_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

echo "[tenant_backup] tenant=$TENANT_ID → $BACKUP_DIR"

# ----------------------------------------------------------------------------
# 1) PostgreSQL — domain_id 필터한 모든 핵심 테이블
# ----------------------------------------------------------------------------
echo "[1] PostgreSQL CSV export..."
psql -X "$PG_ADMIN_DSN" -c "\COPY (SELECT * FROM tenants WHERE domain_id='$TENANT_ID') TO '$BACKUP_DIR/postgres/tenants.csv' CSV HEADER"

for tbl in user_tenant_membership documents chunks conversations messages chat_logs \
           indexing_jobs tenant_config_overrides tenant_input_schemas \
           tenant_lifecycle_logs adapter_registry assessment_items assessment_logs \
           chunks_archive; do
  psql -X "$PG_ADMIN_DSN" \
    -c "\COPY (SELECT * FROM $tbl WHERE domain_id='$TENANT_ID') TO '$BACKUP_DIR/postgres/$tbl.csv' CSV HEADER" \
    2>/dev/null || echo "  (skip $tbl — 테이블 없음)"
done

# ----------------------------------------------------------------------------
# 2) Qdrant — collection snapshot
# ----------------------------------------------------------------------------
echo "[2] Qdrant snapshot..."
for cname in chunks items; do
  COL="${cname}_${TENANT_ID}"
  # snapshot 생성
  SNAPSHOT_NAME=$(curl -fsS -X POST "$QDRANT_URL/collections/$COL/snapshots" 2>/dev/null \
    | jq -r '.result.name // empty' 2>/dev/null || echo "")
  if [[ -n "$SNAPSHOT_NAME" ]]; then
    curl -fsS "$QDRANT_URL/collections/$COL/snapshots/$SNAPSHOT_NAME" \
      -o "$BACKUP_DIR/qdrant/${COL}.snapshot"
    echo "  ✓ $COL"
  else
    echo "  (skip $COL — collection 없음)"
  fi
done

# ----------------------------------------------------------------------------
# 3) MinIO mirror
# ----------------------------------------------------------------------------
echo "[3] MinIO mirror..."
if command -v mc >/dev/null 2>&1; then
  MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
  MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-domainrag}"
  MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-changeme_minio_root}"
  MINIO_BUCKET="${MINIO_BUCKET:-domainrag}"
  mc alias set domainrag "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" 2>/dev/null || true
  mc mirror "domainrag/$MINIO_BUCKET/$TENANT_ID/" "$BACKUP_DIR/minio/" 2>/dev/null || true
fi

# ----------------------------------------------------------------------------
# 4) configs/tenants/<id>/ 사본
# ----------------------------------------------------------------------------
echo "[4] configs 사본..."
if [[ -d "configs/tenants/$TENANT_ID" ]]; then
  cp -r "configs/tenants/$TENANT_ID/." "$BACKUP_DIR/configs/"
fi

# ----------------------------------------------------------------------------
# 5) manifest.json (sha256 of all artifacts)
# ----------------------------------------------------------------------------
echo "[5] manifest.json 생성..."
if command -v jq >/dev/null 2>&1; then
  ARTIFACTS=$(cd "$BACKUP_DIR" && find . -type f ! -name manifest.json \
    | sed 's|^\./||' \
    | while read -r p; do
        sha=$(sha256sum "$p" | awk '{print $1}')
        size=$(wc -c < "$p" | tr -d '[:space:]')
        printf '{"path":"%s","sha256":"%s","size":%s}\n' "$p" "$sha" "$size"
      done \
    | jq -s '.')
  jq -n \
    --arg tid "$TENANT_ID" \
    --arg ts "$(date -u +%FT%TZ)" \
    --argjson arts "${ARTIFACTS:-[]}" \
    '{domain_id: $tid, created_at: $ts, artifacts: $arts}' \
    > "$BACKUP_DIR/manifest.json"
else
  echo "  ⚠ jq 미설치 — manifest.json 생성 skip (tenant_restore.sh sha 검증 불가)"
fi

echo "[tenant_backup] 완료 → $BACKUP_DIR"
