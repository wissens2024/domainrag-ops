# Tenant 등록 Runbook (ADR-008·018·021 §5 + Y5)

신규 tenant 등록을 안전하게 수행하기 위한 절차. ADR-021 §5 atomic rollback과 정합.

## 사전 준비

1. tenant_id 결정 (`security`, `legal`, `exam_bank` 같은 lowercase ascii).
2. AuthFusion 운영자에게 OAuth2 client 등록 사전 요청.
   ```text
   client_id: client-<tenant_id>
   redirect_uris: https://domainrag.local/auth/callback
   scopes: openid, profile, email
   grant_types: authorization_code, refresh_token
   ```
3. tenant template 디렉터리 확인: `configs/tenant_templates/<domain_type>/`

## 등록 실행

```bash
cd /opt/domainrag
./infra/scripts/tenant_register.sh security \
  --display-name "보안 도메인" \
  --domain-type security \
  --embedding-model bge-m3 \
  --modules rag
```

dry-run으로 status 검증만:
```bash
./infra/scripts/tenant_register.sh security --dry-run
```

## 스크립트 동작 (ADR-021 §5)

| Step | 동작 | Rollback |
|---|---|---|
| 0 | `SELECT count(*) FROM tenants` — 0이면 진행, 아니면 exit 2 | — |
| 1 | tenants INSERT | DELETE |
| 2 | `configs/tenants/<id>/` 시드 | rm -rf |
| 3 | Qdrant `chunks_<id>` (+ `items_<id>` if assessment) | DELETE collection |
| 4 | MinIO prefix `.tenant_marker` 업로드 | mc rm |
| 5 | `tenant_input_schemas` v1 active 시드 | DELETE |
| 6 | AuthFusion client 등록 안내 (수동) | — |

중간 실패 시 trap이 역순으로 롤백 + exit 3.

## 사후 작업

1. AuthFusion 운영자 확인:
   - `client-<tenant_id>` 가 IdP에 등록됐는지
   - 사용자 ↔ tenant 매핑 (user_tenant_membership) 추가
2. admin UI 첫 접속: `/<tenant_id>/admin/dashboard`
3. Schema Editor에서 input_schema 본격 작성: `/<tenant_id>/admin/schema`
4. 운영자 평가셋이 있으면 `data/eval/tenants/<tenant_id>/` 에 시드

## 흔한 실패 케이스

| 증상 | 원인 | 조치 |
|---|---|---|
| exit 2 `tenant already exists` | tenants 테이블에 이미 존재 | tenant_restore.sh 사용 (archived인 경우) |
| Step 3에서 Qdrant 503 | qdrant container down | `docker compose up -d qdrant` |
| Step 4에서 mc unknown | MinIO client 미설치 | `apt install minio-client` 또는 dev 환경은 skip 메시지 출력 후 진행 |
| Step 5 IntegrityError | tenants row INSERT는 됐는데 schema FK 위반 | Rollback 자동 — 로그 확인 후 tenants row 잔존 시 수동 삭제 |

## Related

- [tenant_register.sh](../../infra/scripts/tenant_register.sh)
- [tenant_restore.sh](../../infra/scripts/tenant_restore.sh) — archived/deleted/suspended 상태에서만
- [tenant_backup.sh](../../infra/scripts/tenant_backup.sh) — 매월/주기 운영
- [ADR-018 §10](../adr/018-sso-integration-authfusion.md) — AuthFusion client 등록 책임
- [ADR-021 §5](../adr/021-operational-bootstrap.md) — atomic rollback 결정
