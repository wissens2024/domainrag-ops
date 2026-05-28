#!/bin/bash
# ============================================================================
# DomainRAG Ops — Tenant 복원 (ADR-012 §8 + ADR-021 §5 + Y5)
# ============================================================================
# tenant_register.sh와 의도적으로 분리. Y5 — 본 스크립트는 *archived/deleted/suspended/
# 미존재* 상태에서만 실행. status='active'면 거부.
#
# 책임:
#   0) tenants.status 검증 — active면 거부
#   1) Manifest 무결성 검증
#   2) PostgreSQL data 복원 (BYPASSRLS, platform_admin role)
#   3) Qdrant snapshot 복원
#   4) MinIO mirror
#   5) configs 복원
#   6) status='active' 전이
#   7) 검증 — chunk count + collection 정합
#
# 사용법:
#   ./tenant_restore.sh <backup_dir> <domain_id>
# ============================================================================

set -euo pipefail

BACKUP_DIR="${1:?backup_dir 필수}"
TENANT_ID="${2:?domain_id 필수}"
[[ -d "$BACKUP_DIR" ]] || { echo "ERROR: $BACKUP_DIR 없음" >&2; exit 1; }

PG_ADMIN_DSN="${POSTGRES_ADMIN_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

echo "[tenant_restore] domain_id=$TENANT_ID backup_dir=$BACKUP_DIR"

# ----------------------------------------------------------------------------
# Y5 — Step 0: status 검증
# ----------------------------------------------------------------------------
STATUS=$(psql -X -A -t "$PG_ADMIN_DSN" \
  -c "SELECT status FROM tenants WHERE domain_id='$TENANT_ID';" 2>/dev/null | tr -d '[:space:]')

if [[ "$STATUS" == "active" ]]; then
  echo "[tenant_restore] ERROR: tenant '$TENANT_ID' is already active. Aborting to avoid clobbering live data." >&2
  exit 2
fi

if [[ -z "$STATUS" ]]; then
  echo "[Step 0] tenant '$TENANT_ID' missing — will create row from backup"
elif [[ "$STATUS" == "archived" || "$STATUS" == "deleted" || "$STATUS" == "suspended" ]]; then
  echo "[Step 0] tenant '$TENANT_ID' current status='$STATUS' — proceeding with restore"
else
  echo "[tenant_restore] ERROR: unexpected status='$STATUS'" >&2
  exit 2
fi

# ----------------------------------------------------------------------------
# Step 1: Manifest 무결성 검증
# ----------------------------------------------------------------------------
MANIFEST="$BACKUP_DIR/manifest.json"
if [[ -f "$MANIFEST" ]] && command -v jq >/dev/null 2>&1; then
  echo "[Step 1] manifest 무결성 검증..."
  jq -r '.artifacts[]? | "\(.path) \(.sha256)"' "$MANIFEST" | while read -r path sha; do
    [[ -n "$path" && -n "$sha" ]] || continue
    [[ -f "$BACKUP_DIR/$path" ]] || { echo "  ⚠ missing: $path"; continue; }
    actual=$(sha256sum "$BACKUP_DIR/$path" | awk '{print $1}')
    if [[ "$actual" != "$sha" ]]; then
      echo "ERROR: $path sha mismatch (expected=$sha actual=$actual)" >&2
      exit 1
    fi
  done
else
  echo "[Step 1] manifest 또는 jq 없음 — sha 검증 skip"
fi

# ----------------------------------------------------------------------------
# Step 2: PostgreSQL data 복원 (BYPASSRLS)
# ----------------------------------------------------------------------------
echo "[Step 2] PostgreSQL data 복원..."
for tbl in tenants user_tenant_membership documents chunks conversations messages \
           chat_logs indexing_jobs tenant_config_overrides tenant_input_schemas \
           tenant_lifecycle_logs assessment_items assessment_logs adapter_registry \
           chunks_archive; do
  CSV="$BACKUP_DIR/postgres/${tbl}.csv"
  if [[ -f "$CSV" ]]; then
    psql -X "$PG_ADMIN_DSN" -c "DELETE FROM $tbl WHERE domain_id='$TENANT_ID';" 2>/dev/null || true
    psql -X "$PG_ADMIN_DSN" -c "\COPY $tbl FROM '$CSV' CSV HEADER"
    echo "  ✓ $tbl"
  fi
done

# ----------------------------------------------------------------------------
# Step 3: Qdrant snapshot 복원
# ----------------------------------------------------------------------------
echo "[Step 3] Qdrant snapshot 복원..."
for cname in chunks items; do
  SNAPSHOT_FILE="$BACKUP_DIR/qdrant/${cname}_${TENANT_ID}.snapshot"
  if [[ -f "$SNAPSHOT_FILE" ]]; then
    SNAPSHOT_NAME=$(basename "$SNAPSHOT_FILE")
    curl -fsS -X POST "$QDRANT_URL/collections/${cname}_$TENANT_ID/snapshots/upload" \
      -F "snapshot=@$SNAPSHOT_FILE" > /dev/null || \
      echo "  ⚠ ${cname}_${TENANT_ID} snapshot upload 실패"
    curl -fsS -X PUT "$QDRANT_URL/collections/${cname}_$TENANT_ID/snapshots/recover" \
      -H "Content-Type: application/json" \
      -d "{\"location\":\"file:///qdrant/snapshots/$SNAPSHOT_NAME\"}" > /dev/null || \
      echo "  ⚠ ${cname}_${TENANT_ID} recover 실패"
  fi
done

# ----------------------------------------------------------------------------
# Step 4: MinIO mirror
# ----------------------------------------------------------------------------
echo "[Step 4] MinIO mirror..."
if command -v mc >/dev/null 2>&1 && [[ -d "$BACKUP_DIR/minio" ]]; then
  MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
  MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-domainrag}"
  MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-changeme_minio_root}"
  MINIO_BUCKET="${MINIO_BUCKET:-domainrag}"
  mc alias set domainrag "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" 2>/dev/null || true
  mc mirror "$BACKUP_DIR/minio/" "domainrag/$MINIO_BUCKET/$TENANT_ID/"
fi

# ----------------------------------------------------------------------------
# Step 5: configs 복원
# ----------------------------------------------------------------------------
echo "[Step 5] configs 복원..."
if [[ -d "$BACKUP_DIR/configs" ]]; then
  mkdir -p "configs/tenants/$TENANT_ID"
  cp -rn "$BACKUP_DIR/configs/." "configs/tenants/$TENANT_ID/"
fi

# ----------------------------------------------------------------------------
# Step 6: tenants.status = 'active'
# ----------------------------------------------------------------------------
echo "[Step 6] tenants.status = 'active'..."
psql -X "$PG_ADMIN_DSN" -c "
  UPDATE tenants
     SET status='active', delete_status=NULL, deleted_at=NULL
   WHERE domain_id='$TENANT_ID';
"

# ----------------------------------------------------------------------------
# Step 7: 검증
# ----------------------------------------------------------------------------
echo "[Step 7] 검증 — chunk count + collection 정합..."
CHUNK_COUNT_DB=$(psql -X -A -t "$PG_ADMIN_DSN" \
  -c "SELECT count(*) FROM chunks WHERE domain_id='$TENANT_ID';" | tr -d '[:space:]')
COLLECTION_INFO=$(curl -fsS "$QDRANT_URL/collections/chunks_$TENANT_ID" 2>/dev/null \
  | jq -r '.result.points_count // 0' 2>/dev/null || echo "?")

echo "  chunks(DB): $CHUNK_COUNT_DB"
echo "  chunks(Qdrant): $COLLECTION_INFO"
if [[ "$CHUNK_COUNT_DB" != "$COLLECTION_INFO" && "$COLLECTION_INFO" != "?" ]]; then
  echo "  ⚠ 불일치 — 운영자 검토 필요"
fi

cat <<EOF

[tenant_restore] $TENANT_ID 복원 완료
  - admin UI 확인: /$TENANT_ID/admin/dashboard
  - AuthFusion client 등록 여부는 운영자가 별도 확인
EOF
