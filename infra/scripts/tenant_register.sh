#!/bin/bash
# ============================================================================
# DomainRAG Ops — 신규 Tenant 등록 자동화
# ============================================================================
# ADR-008·018 정합:
#   1) AuthFusion OAuth2 client 등록 (수동 — IdP 운영자)
#   2) DomainRAG tenants 테이블에 row 삽입
#   3) Qdrant collection 생성 (chunks_<tenant_id>, items_<tenant_id> if assessment 활성)
#   4) MinIO prefix 초기화
#   5) configs/tenants/<tenant_id>/ 시드
#   6) tenant_input_schemas 시드
#
# 사용법:
#   ./tenant_register.sh <tenant_id> --display-name "..." --domain-type security ...
# ============================================================================

set -euo pipefail

TENANT_ID="${1:?tenant_id 필수}"
shift

# 기본값
DISPLAY_NAME="$TENANT_ID"
DOMAIN_TYPE="security"
EMBEDDING_MODEL="bge-m3"
MODULES="rag"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --display-name) DISPLAY_NAME="$2"; shift 2 ;;
    --domain-type) DOMAIN_TYPE="$2"; shift 2 ;;
    --embedding-model) EMBEDDING_MODEL="$2"; shift 2 ;;
    --modules) MODULES="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

echo "[tenant_register] tenant_id=$TENANT_ID display=$DISPLAY_NAME domain=$DOMAIN_TYPE"

# Step 1: AuthFusion OAuth2 client 등록 안내 (자동화는 별도 admin token 필요)
cat <<EOF
[Step 1] AuthFusion 운영자에게 다음 client 등록 요청:
  client_id: client-$TENANT_ID
  redirect_uris: https://domainrag.local/auth/callback
  scopes: openid, profile, email
  grant_types: authorization_code, refresh_token
EOF

# Step 2: PostgreSQL tenants row 삽입
echo "[Step 2] PostgreSQL tenants row 삽입..."
psql "${POSTGRES_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}" <<-SQL
  INSERT INTO tenants (tenant_id, display_name, domain_type, embedding_model, status, modules, created_at)
  VALUES ('$TENANT_ID', '$DISPLAY_NAME', '$DOMAIN_TYPE', '$EMBEDDING_MODEL', 'active', ARRAY['$MODULES'], NOW())
  ON CONFLICT (tenant_id) DO NOTHING;
SQL

# Step 3: Qdrant collection 생성
echo "[Step 3] Qdrant collection 생성..."
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
curl -s -X PUT "$QDRANT_URL/collections/chunks_$TENANT_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "vectors": { "dense": { "size": 1024, "distance": "Cosine" } },
    "sparse_vectors": { "sparse": {} }
  }' > /dev/null

if [[ "$MODULES" == *"assessment"* ]]; then
  curl -s -X PUT "$QDRANT_URL/collections/items_$TENANT_ID" \
    -H "Content-Type: application/json" \
    -d '{
      "vectors": { "dense": { "size": 1024, "distance": "Cosine" } },
      "sparse_vectors": { "sparse": {} }
    }' > /dev/null
fi

# Step 4: MinIO prefix 초기화
echo "[Step 4] MinIO prefix 초기화..."
mc alias set domainrag "${MINIO_ENDPOINT:-http://localhost:9000}" \
  "${MINIO_ACCESS_KEY:-minioadmin}" "${MINIO_SECRET_KEY:-changeme_minio}" 2>/dev/null || true
mc mb --ignore-existing "domainrag/${MINIO_BUCKET:-domainrag}/$TENANT_ID/" 2>/dev/null || true

# Step 5: configs/tenants/<tenant_id>/ 시드
echo "[Step 5] configs/tenants/$TENANT_ID/ 시드..."
TENANT_CONFIG_DIR="configs/tenants/$TENANT_ID"
mkdir -p "$TENANT_CONFIG_DIR/prompts"

# 도메인별 시드 템플릿이 있으면 복사, 없으면 platform 기본값 상속
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

# Step 6: tenant_input_schemas 시드 (security 예시 스키마)
echo "[Step 6] input_schema 시드..."
psql "${POSTGRES_URL:-postgresql://domainrag_platform_admin:changeme_admin@localhost:5432/domainrag}" <<-SQL
  -- input_schema는 admin UI에서 편집 가능. 여기서는 빈 schema_version 1만 등록.
  INSERT INTO tenant_input_schemas (tenant_id, schema_version, status, schema_yaml, created_at)
  VALUES ('$TENANT_ID', 1, 'active', '{}', NOW())
  ON CONFLICT (tenant_id, schema_version) DO NOTHING;
SQL

echo ""
echo "[tenant_register] $TENANT_ID 등록 완료"
echo "  - 사용자 IdP 등록은 SSO 운영자가 수동으로 진행"
echo "  - admin UI에서 input_schema 편집: /$TENANT_ID/admin/schema"
