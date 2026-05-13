#!/bin/sh
# ============================================================================
# MinIO bucket 초기화 (dev compose)
# ============================================================================
# 운영에서도 동일하게 사용 가능. tenant prefix는 tenant_register.sh가 별도 생성.

set -e

mc alias set domainrag "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY"
mc mb --ignore-existing domainrag/"$MINIO_BUCKET"
mc anonymous set none domainrag/"$MINIO_BUCKET"

# 디렉터리 prefix 시드 (실제 tenant prefix는 tenant_register.sh로 생성)
echo "[init_minio] bucket=$MINIO_BUCKET 초기화 완료"
