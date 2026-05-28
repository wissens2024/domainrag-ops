#!/bin/bash
# ============================================================================
# DomainRAG Ops — 신규 Tenant 등록 자동화 (ADR-008·018·021 §5 + Y5)
# ============================================================================
# 책임:
#   1) tenants 테이블에 *해당 id가 존재하지 않을 때만* 새 row INSERT
#   2) configs/tenants/<id> 시드 (template 복사)
#   3) Qdrant collection 생성 (chunks_<id>, items_<id> if assessment)
#   4) MinIO prefix 초기화
#   5) tenant_input_schemas 시드
#   6) AuthFusion OAuth2 client 등록 안내 (수동)
#
# 중간 실패 시 자동 rollback (역순):
#   step5 → tenant_input_schemas row 제거
#   step4 → MinIO prefix 제거
#   step3 → Qdrant collection drop
#   step2 → configs/tenants/<id> 디렉터리 제거 (git untracked 한정)
#   step1 → tenants row 제거
#
# 사용법:
#   ./tenant_register.sh <domain_id> --display-name "..." --domain-type security ...
# ============================================================================

set -euo pipefail

TENANT_ID="${1:?domain_id 필수}"
shift

# 기본값
DISPLAY_NAME="$TENANT_ID"
DOMAIN_TYPE="security"
EMBEDDING_MODEL="bge-m3"
MODULES="rag"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --display-name) DISPLAY_NAME="$2"; shift 2 ;;
    --domain-type) DOMAIN_TYPE="$2"; shift 2 ;;
    --embedding-model) EMBEDDING_MODEL="$2"; shift 2 ;;
    --modules) MODULES="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# 환경 변수 (init_postgres.sql이 만든 두 role 사용)
PG_ADMIN_DSN="${POSTGRES_ADMIN_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-domainrag}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-changeme_minio_root}"
MINIO_BUCKET="${MINIO_BUCKET:-domainrag}"

echo "[tenant_register] domain_id=$TENANT_ID display=$DISPLAY_NAME domain=$DOMAIN_TYPE modules=$MODULES"

# ----------------------------------------------------------------------------
# Y5 — Step 0: status 검증 — 존재하지 않을 때만 등록 진행
# ----------------------------------------------------------------------------
EXISTS=$(psql -X -A -t "$PG_ADMIN_DSN" -c "SELECT count(*) FROM tenants WHERE domain_id='$TENANT_ID';" | tr -d '[:space:]')
if [[ "$EXISTS" != "0" ]]; then
  echo "[tenant_register] ERROR: tenant '$TENANT_ID' already exists. Use tenant_restore.sh for archived tenants." >&2
  exit 2
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[tenant_register] DRY_RUN — would register domain_id=$TENANT_ID"
  exit 0
fi

# ----------------------------------------------------------------------------
# Rollback 함수 — 각 step 직후 등록
# ----------------------------------------------------------------------------
ROLLBACK_STEPS=()

trap 'rollback' ERR

rollback() {
  FAILED_STEP="${CURRENT_STEP:-unknown}"
  echo "[tenant_register] FAILURE at $FAILED_STEP — rolling back ($(echo ${#ROLLBACK_STEPS[@]}) steps)..." >&2
  # 역순 실행
  ROLLBACK_OK=true
  for ((i=${#ROLLBACK_STEPS[@]}-1; i>=0; i--)); do
    echo "  ↩ ${ROLLBACK_STEPS[$i]}" >&2
    eval "${ROLLBACK_STEPS[$i]}" || ROLLBACK_OK=false
  done

  # ADR-021 §5 — dead-letter 누적
  psql -X "$PG_ADMIN_DSN" -c "
    INSERT INTO tenant_register_failures (
        domain_id, failed_step, error_message, rollback_succeeded, details
    ) VALUES (
        '$TENANT_ID', '$FAILED_STEP', 'see operator console',
        $ROLLBACK_OK, '{}'::jsonb
    );
  " 2>/dev/null || echo "  ⚠ dead-letter INSERT 실패 (tenant_register_failures 미존재?)" >&2

  exit 3
}

add_rollback() {
  ROLLBACK_STEPS+=("$1")
}

# ----------------------------------------------------------------------------
# Step 1: AuthFusion OAuth2 client 자동 등록 (ADR-018 §10 + ADR-021 §5)
# ----------------------------------------------------------------------------
CURRENT_STEP="step1_authfusion_register"
AUTHFUSION_ADMIN_URL="${AUTHFUSION_ADMIN_URL:-http://localhost:8081}"
AUTHFUSION_ADMIN_API_KEY="${AUTHFUSION_ADMIN_API_KEY:-}"

if [[ -n "$AUTHFUSION_ADMIN_API_KEY" ]]; then
  echo "[Step 1] AuthFusion OAuth2 client 자동 등록..."
  HTTP_CODE=$(curl -s -o /tmp/authfusion_register.json -w "%{http_code}" \
    -X POST "$AUTHFUSION_ADMIN_URL/api/v1/clients" \
    -H "Authorization: Bearer $AUTHFUSION_ADMIN_API_KEY" \
    -H "Content-Type: application/json" \
    -d "{
      \"client_id\": \"client-$TENANT_ID\",
      \"redirect_uris\": [\"https://domainrag.local/auth/callback\"],
      \"scopes\": [\"openid\", \"profile\", \"email\"],
      \"grant_types\": [\"authorization_code\", \"refresh_token\"]
    }")
  if [[ "$HTTP_CODE" -lt 200 || "$HTTP_CODE" -ge 300 ]]; then
    echo "  ERROR: AuthFusion register HTTP $HTTP_CODE — $(cat /tmp/authfusion_register.json)" >&2
    rm -f /tmp/authfusion_register.json
    exit 4
  fi
  rm -f /tmp/authfusion_register.json
  add_rollback "curl -s -X DELETE \"$AUTHFUSION_ADMIN_URL/api/v1/clients/client-$TENANT_ID\" -H \"Authorization: Bearer $AUTHFUSION_ADMIN_API_KEY\" || true"
  echo "  ✓ client-$TENANT_ID 등록 완료"
else
  echo "[Step 1] AUTHFUSION_ADMIN_API_KEY 미설정 — AuthFusion client 등록은 운영자가 수동 처리:"
  echo "  client_id: client-$TENANT_ID"
  echo "  redirect_uris: https://domainrag.local/auth/callback"
  echo "  scopes: openid, profile, email"
  echo "  grant_types: authorization_code, refresh_token"
fi

# ----------------------------------------------------------------------------
# Step 2: PostgreSQL tenants row INSERT
# ----------------------------------------------------------------------------
CURRENT_STEP="step2_tenants_insert"
echo "[Step 2] PostgreSQL tenants row INSERT..."
psql -X "$PG_ADMIN_DSN" -c "
  INSERT INTO tenants (domain_id, display_name, domain_type, embedding_model, status, modules, created_at)
  VALUES ('$TENANT_ID', '$DISPLAY_NAME', '$DOMAIN_TYPE', '$EMBEDDING_MODEL', 'active', ARRAY['$MODULES'], NOW());
"
add_rollback "psql -X \"$PG_ADMIN_DSN\" -c \"DELETE FROM tenants WHERE domain_id='$TENANT_ID';\""

# ----------------------------------------------------------------------------
# Step 2: configs/tenants/<id>/ 시드
# ----------------------------------------------------------------------------
CURRENT_STEP="step2_configs_seed"
echo "[Step 2] configs/tenants/$TENANT_ID/ 시드..."
TENANT_CONFIG_DIR="configs/tenants/$TENANT_ID"
mkdir -p "$TENANT_CONFIG_DIR/prompts"
if [[ -d "configs/tenant_templates/$DOMAIN_TYPE" ]]; then
  cp -rn "configs/tenant_templates/$DOMAIN_TYPE/." "$TENANT_CONFIG_DIR/"
fi

cat > "$TENANT_CONFIG_DIR/auth.yaml" <<EOF
client_id: client-$TENANT_ID
authfusion_client_id: client-$TENANT_ID
EOF

cat > "$TENANT_CONFIG_DIR/overrides.yaml" <<EOF
# tenant-level overrides (platform defaults에서 변경할 항목만 작성)
EOF

add_rollback "rm -rf \"$TENANT_CONFIG_DIR\""

# ----------------------------------------------------------------------------
# Step 3: Qdrant collection 생성
# ----------------------------------------------------------------------------
CURRENT_STEP="step3_qdrant_create"
echo "[Step 3] Qdrant collections 생성..."
curl -fsS -X PUT "$QDRANT_URL/collections/chunks_$TENANT_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "vectors": { "dense": { "size": 1024, "distance": "Cosine" } },
    "sparse_vectors": { "sparse": {} }
  }' > /dev/null
add_rollback "curl -fsS -X DELETE \"$QDRANT_URL/collections/chunks_$TENANT_ID\" || true"

if [[ "$MODULES" == *"assessment"* ]]; then
  curl -fsS -X PUT "$QDRANT_URL/collections/items_$TENANT_ID" \
    -H "Content-Type: application/json" \
    -d '{
      "vectors": { "dense": { "size": 1024, "distance": "Cosine" } },
      "sparse_vectors": { "sparse": {} }
    }' > /dev/null
  add_rollback "curl -fsS -X DELETE \"$QDRANT_URL/collections/items_$TENANT_ID\" || true"
fi

# ----------------------------------------------------------------------------
# Step 4: MinIO prefix 초기화
# ----------------------------------------------------------------------------
CURRENT_STEP="step4_minio_prefix"
echo "[Step 4] MinIO prefix 초기화..."
if command -v mc >/dev/null 2>&1; then
  mc alias set domainrag "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" 2>/dev/null || true
  # bucket 자체는 운영 환경에서 사전 생성. prefix는 첫 object 업로드 시 자동 생성.
  # 마커 파일로 prefix 존재 보장 (관제 시 시각화 용).
  TMPF=$(mktemp); echo "tenant=$TENANT_ID created_at=$(date -u +%FT%TZ)" > "$TMPF"
  mc cp "$TMPF" "domainrag/$MINIO_BUCKET/$TENANT_ID/.tenant_marker" >/dev/null
  rm -f "$TMPF"
  add_rollback "mc rm \"domainrag/$MINIO_BUCKET/$TENANT_ID/.tenant_marker\" || true"
else
  echo "  ⚠ mc(MinIO client) 미설치 — MinIO prefix 자동 생성 skip. 별도 운영 절차로 등록 필요."
fi

# ----------------------------------------------------------------------------
# Step 5: tenant_input_schemas 시드 (빈 schema, admin이 편집 시작)
# ----------------------------------------------------------------------------
CURRENT_STEP="step5_input_schema_seed"
echo "[Step 5] tenant_input_schemas 시드..."
psql -X "$PG_ADMIN_DSN" -c "
  INSERT INTO tenant_input_schemas (domain_id, schema_version, status, schema_yaml, created_at)
  VALUES ('$TENANT_ID', 1, 'active', '{}'::jsonb, NOW());
"
add_rollback "psql -X \"$PG_ADMIN_DSN\" -c \"DELETE FROM tenant_input_schemas WHERE domain_id='$TENANT_ID';\""

# ----------------------------------------------------------------------------
# Step 6: NOTIFY tenant_lifecycle_changed (ADR-021 §2)
# ----------------------------------------------------------------------------
CURRENT_STEP="step6_notify"
echo "[Step 6] pg_notify tenant_lifecycle_changed..."
psql -X "$PG_ADMIN_DSN" -c "
  SELECT pg_notify('tenant_lifecycle_changed',
    json_build_object('domain_id', '$TENANT_ID',
                      'old_status', NULL,
                      'new_status', 'active')::text);
"

# ----------------------------------------------------------------------------
# Step 7: tenant_lifecycle_logs INSERT 'tenant_registered'
# ----------------------------------------------------------------------------
CURRENT_STEP="step7_lifecycle_log"
echo "[Step 7] tenant_lifecycle_logs INSERT..."
psql -X "$PG_ADMIN_DSN" -c "
  INSERT INTO tenant_lifecycle_logs (
      domain_id, action, from_state, to_state, actor, reason, details
  ) VALUES (
      '$TENANT_ID', 'tenant_registered', NULL, 'active',
      'tenant_register.sh', 'new tenant',
      jsonb_build_object('display_name', '$DISPLAY_NAME',
                         'domain_type', '$DOMAIN_TYPE',
                         'modules', ARRAY['$MODULES'])
  );
"

# ----------------------------------------------------------------------------
# Step 8: AuthFusion 안내 (수동)
# ----------------------------------------------------------------------------
trap - ERR

cat <<EOF

[tenant_register] $TENANT_ID 등록 완료

다음 작업은 AuthFusion 운영자에게 직접 요청:
  client_id: client-$TENANT_ID
  redirect_uris: https://domainrag.local/auth/callback
  scopes: openid, profile, email
  grant_types: authorization_code, refresh_token

다음 확인 사항:
  - admin UI에서 input_schema 편집: /$TENANT_ID/admin/schema
  - users → user_tenant_membership 매핑은 admin UI 또는 직접 INSERT
EOF
