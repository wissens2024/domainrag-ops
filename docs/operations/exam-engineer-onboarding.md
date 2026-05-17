# exam-engineer Tenant 온보딩 절차

> **첫 번째 운영 tenant — 정보처리기사 문제은행.**
> 사용자: WiSentinel AI dev 부서 + DomainRAG 운영자(cs@wissensbaum.com).
> 기준일: 2026-05-18. AuthFusion `sso-integration-guide.md` + DomainRAG `ADR-018`.

| 항목 | 값 |
|---|---|
| tenant_id | `exam-engineer` |
| display_name | 정보처리기사 문제은행 |
| domain_type | assessment |
| modules | `rag`, `assessment` |
| RP slug (AuthFusion) | `domainrag-ops` |
| Issuer | `https://api.aines.kr` |

## 사전 결정 (이미 확정)

- WiSentinel 기존 admin(`admin@example.com`)은 그대로 유지. cs는 *신규 추가*.
- `cs@wissensbaum.com`은 두 RP(WiSentinel + DomainRAG) admin 동시 부여.
- WiSentinel 사용자(alice/bob/charlie@example.com 등 dev 부서)는 DomainRAG에 `domainrag-ops-user` role 부여 후 JIT으로 첫 SSO 시 자동 row 생성.
- AuthFusion 사용자/role 등록은 IdP 운영자가, DomainRAG tenant 등록은 DomainRAG 운영자가 수행.

---

## Phase 1 — AuthFusion 측 등록 (IdP 운영자)

AuthFusion Admin Console(`https://console.aines.kr`)에 SYSTEM_ADMIN으로 로그인 후 admin token 발급:

```bash
TOKEN=$(curl -sX POST https://console.aines.kr/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<admin>","password":"<pw>"}' | jq -r .accessToken)
```

### 1.1 DomainRAG SSO client 등록

```bash
curl -sX POST https://console.aines.kr/api/v1/clients \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "clientName": "DomainRAG Ops",
    "clientType": "CONFIDENTIAL",
    "redirectUris": [
      "https://rag.aines.kr/auth/callback",
      "http://localhost:3010/auth/callback"
    ],
    "allowedScopes": ["openid","profile","email","offline_access"],
    "allowedGrantTypes": ["authorization_code","refresh_token"],
    "requirePkce": true,
    "mfaRequired": true
  }' | tee /tmp/domainrag-client.json | jq '{id, clientId, clientSecret}'
```

응답 3 값 보존 (안전 채널로 DomainRAG 운영자에게 전달, `clientSecret`은 1회만 노출):

| 값 | 용도 |
|---|---|
| `id` | role `ownerClientId`용 (Phase 1.2) |
| `clientId` | DomainRAG `.env`의 `AUTHFUSION_CLIENT_ID` (UUID) |
| `clientSecret` | DomainRAG `.env`의 `AUTHFUSION_CLIENT_SECRET` (평문) |

### 1.2 Service-scoped role 3종 등록

```bash
CLIENT_ID=$(jq -r .id /tmp/domainrag-client.json)
for R in admin auditor user; do
  curl -sX POST https://console.aines.kr/api/v1/roles \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"name\":\"domainrag-ops-$R\",\"ownerClientId\":\"$CLIENT_ID\",\"description\":\"DomainRAG Ops $R\"}"
done
```

### 1.3 cs@wissensbaum.com 신규 사용자 등록

```bash
curl -sX POST https://console.aines.kr/api/v1/users \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "username": "cs",
    "email": "cs@wissensbaum.com",
    "fullName": "Customer Success Admin",
    "password": "<one-time-temp-pw>",
    "mustChangePassword": true,
    "emailVerified": true
  }' | tee /tmp/cs-user.json | jq '{id, username, email}'
```

응답의 `id`(user UUID)를 보존. 첫 로그인 시 MFA enrollment 강제(`mfaRequired=true` client 효과).

### 1.4 cs에 role 부여 (두 RP admin 동시)

```bash
CS_UUID=$(jq -r .id /tmp/cs-user.json)

# 1) wisentinel-admin (기존 WiSentinel RP — admin@example.com과 공존)
WISENTINEL_ADMIN_ROLE=$(curl -s "https://console.aines.kr/api/v1/roles?name=wisentinel-admin" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.[0].id')
curl -sX POST "https://console.aines.kr/api/v1/roles/assign/user/$CS_UUID/role/$WISENTINEL_ADMIN_ROLE" \
  -H "Authorization: Bearer $TOKEN"

# 2) domainrag-ops-admin (Phase 1.2에서 등록된 신규 role)
DR_ADMIN_ROLE=$(curl -s "https://console.aines.kr/api/v1/roles?name=domainrag-ops-admin&ownerClientId=$CLIENT_ID" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.[0].id')
curl -sX POST "https://console.aines.kr/api/v1/roles/assign/user/$CS_UUID/role/$DR_ADMIN_ROLE" \
  -H "Authorization: Bearer $TOKEN"
```

### 1.5 WiSentinel dev 사용자에 domainrag-ops-user role 부여

WiSentinel 사용자 중 dev 부서 사람들(예: alice/bob/charlie@example.com)에 `domainrag-ops-user` role 부여:

```bash
DR_USER_ROLE=$(curl -s "https://console.aines.kr/api/v1/roles?name=domainrag-ops-user&ownerClientId=$CLIENT_ID" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.[0].id')

for U in alice bob charlie; do
  UID=$(curl -s "https://console.aines.kr/api/v1/users?email=$U@example.com" \
    -H "Authorization: Bearer $TOKEN" | jq -r '.[0].id')
  curl -sX POST "https://console.aines.kr/api/v1/roles/assign/user/$UID/role/$DR_USER_ROLE" \
    -H "Authorization: Bearer $TOKEN"
done
```

JIT default도 USER로 잡혀있지만 명시 부여로 audit 흔적 남김. 신규 dev 입사 시 콘솔에서 동일 절차.

### 1.6 Phase 1 검증

```bash
# JWKS 살아있는지
curl -sf https://api.aines.kr/.well-known/openid-configuration | jq '{issuer, authorization_endpoint, jwks_uri}'

# cs 사용자가 두 role 보유하는지
curl -s "https://console.aines.kr/api/v1/users/$CS_UUID/roles" \
  -H "Authorization: Bearer $TOKEN" | jq '.[] | {name, ownerClientId}'
# 기대: wisentinel-admin + domainrag-ops-admin 2건
```

---

## Phase 2 — DomainRAG 측 등록 (DomainRAG 운영자)

### 2.1 .env에 AuthFusion 응답값 주입

Phase 1.1의 응답을 DomainRAG `.env`에 반영 (KeyHub 결선 전 inline, 결선 후엔 `keyhub://...` 참조로 전환):

```bash
# /opt/domainrag/.env
AUTH_MODE=oidc
AUTHFUSION_ISSUER=https://api.aines.kr
AUTHFUSION_DISCOVERY_URL=https://api.aines.kr/.well-known/openid-configuration
AUTHFUSION_CLIENT_ID=<Phase 1.1 응답의 clientId UUID>
AUTHFUSION_CLIENT_SECRET=<Phase 1.1 응답의 clientSecret>
OIDC_REQUIRE_VERIFIED_EMAIL=true
APP_BASE_URL=https://rag.aines.kr
```

### 2.2 configs/tenants/exam-engineer/auth.yaml의 client UUID 갱신

`configs/tenants/exam-engineer/auth.yaml`의 `TODO-authfusion-client-uuid-for-domainrag-ops` 두 곳을 Phase 1.1 응답의 `clientId`(UUID)로 교체:

```yaml
client_id: "00000000-1111-2222-3333-444455556666"          # Phase 1.1의 clientId
authfusion_client_id: "00000000-1111-2222-3333-444455556666"
```

### 2.3 tenant_register.sh로 인프라 셋업

```bash
cd /opt/domainrag
./infra/scripts/tenant_register.sh exam-engineer \
  --display-name "정보처리기사 문제은행" \
  --domain-type assessment \
  --embedding-model bge-m3 \
  --modules "rag,assessment"
```

이 스크립트가 자동 수행:
- PostgreSQL `tenants` row INSERT (status=active)
- Qdrant `chunks_exam-engineer` + `items_exam-engineer` collection 생성
- MinIO `domainrag/exam-engineer/.tenant_marker` 업로드
- `tenant_input_schemas` v1 active 시드
- `pg_notify('tenant_lifecycle_changed', ...)` — 다른 backend 인스턴스 캐시 invalidate
- `tenant_lifecycle_logs` 감사 row

### 2.4 user_tenant_membership SQL 시드

Phase 1.3·1.5의 user UUID들을 DomainRAG `user_tenant_membership`에 등록 (clearance/department/domain_groups 보강):

```sql
-- /opt/domainrag/infra/seeds/exam-engineer-membership.sql
-- DomainRAG `domainrag_platform_admin` role(BYPASSRLS)로 실행.

BEGIN;

-- 1) cs@wissensbaum.com (ADMIN — confidential clearance)
INSERT INTO user_tenant_membership (
    user_id, tenant_id, clearance, department, domain_groups,
    preferred_username, email, is_active, created_at
) VALUES (
    '<cs UUID from Phase 1.3>',
    'exam-engineer',
    'confidential',
    'customer-success',
    ARRAY['group:exam-engineer-admin', 'group:wissens-internal']::text[],
    'cs',
    'cs@wissensbaum.com',
    true,
    NOW()
);

-- 2) WiSentinel dev 부서 사용자 (USER — internal clearance)
-- AuthFusion에서 user UUID 받아서 채움. 첫 SSO 로그인 시 자동 생성도 가능하지만
-- 명시 seed로 clearance/domain_groups 미리 부여.
INSERT INTO user_tenant_membership (
    user_id, tenant_id, clearance, department, domain_groups,
    preferred_username, email, is_active, created_at
) VALUES
    ('<alice UUID>', 'exam-engineer', 'internal', 'ai-dev',
     ARRAY['group:wisentinel-dev']::text[], 'alice', 'alice@example.com', true, NOW()),
    ('<bob UUID>',   'exam-engineer', 'internal', 'ai-dev',
     ARRAY['group:wisentinel-dev']::text[], 'bob',   'bob@example.com',   true, NOW()),
    ('<charlie UUID>', 'exam-engineer', 'internal', 'ai-dev',
     ARRAY['group:wisentinel-dev']::text[], 'charlie', 'charlie@example.com', true, NOW());

COMMIT;
```

### 2.5 backend 재기동

```bash
docker compose restart backend
# 또는 (운영 systemd):
sudo systemctl restart domainrag-backend
```

`AuthConfigLoader.load()`가 OIDC discovery 자동 fetch + `configs/tenants/exam-engineer/auth.yaml`의 신규 client_id를 `client_tenant_map`에 등록.

### 2.6 Phase 2 검증

```bash
# 1) tenant row 존재
psql "$PG_ADMIN_DSN" -c "SELECT tenant_id, status, modules FROM tenants WHERE tenant_id='exam-engineer';"
#  exam-engineer | active | {rag,assessment}

# 2) Qdrant collection 살아있음
curl -s http://localhost:6333/collections | jq '.result.collections[] | select(.name|startswith("chunks_exam-engineer") or startswith("items_exam-engineer"))'

# 3) input_schema active
psql "$PG_ADMIN_DSN" -c "SELECT schema_version, status FROM tenant_input_schemas WHERE tenant_id='exam-engineer' AND status='active';"

# 4) user_tenant_membership
psql "$PG_ADMIN_DSN" -c "SELECT email, clearance, department FROM user_tenant_membership WHERE tenant_id='exam-engineer' AND is_active=true;"

# 5) DomainRAG가 OIDC discovery 정상 수신
curl -sf http://localhost:8000/api/health/ready | jq
```

---

## Phase 3 — End-to-End SSO 검증

브라우저에서:

1. `https://rag.aines.kr/exam-engineer/chat` 접근
2. 자동으로 AuthFusion `/oauth2/authorize` 로 redirect
3. cs@wissensbaum.com / 임시 비번 → 첫 로그인 시 비번 변경 + MFA enrollment(TOTP)
4. Callback → DomainRAG 쿠키 set → `/exam-engineer/chat` 화면 도착
5. JWT claim 검증:
   - `client_id` = Phase 1.1 응답의 UUID
   - `roles` = `["domainrag-ops-admin", "wisentinel-admin"]`
   - `email_verified` = true
6. DomainRAG `map_oidc_role`이 `domainrag-ops-admin` → DomainRAG ADMIN으로 매핑
7. `/exam-engineer/admin/dashboard` 접근 가능 (admin 메뉴 노출)

WiSentinel 사용자(alice@example.com 등)는:

1. `https://rag.aines.kr/exam-engineer/chat` 접근
2. AuthFusion 로그인 (이미 WiSentinel용 자격증명 보유)
3. JWT `roles`에 `domainrag-ops-user` + `wisentinel-user` 동시 포함
4. DomainRAG는 `domainrag-ops-user` 매칭 → USER로 처리
5. `/chat`만 사용 가능, `/admin/*` 접근 시 403 (insufficient_role)

---

## Phase 4 — 운영 cron

`docs/operations/tenant-register-runbook.md` §사후 작업과 동일. exam-engineer에 추가 cron 없음 — chunk archival·partition 일반 cron이 자동 적용된다.

---

## 트러블슈팅

| 증상 | 원인 | 수정 |
|---|---|---|
| backend 부팅 시 `client_id_not_configured` (tenant_id=exam-engineer) | `configs/tenants/exam-engineer/auth.yaml`의 client_id가 placeholder인 채로 두기 | Phase 2.2 — 실 UUID로 갱신 후 backend 재기동 |
| `tenant_mismatch` 403 (token_tenant=security, expected=exam-engineer) | cs@wissensbaum.com이 다른 RP의 client_id로 로그인 후 exam-engineer 접근 | AuthFusion에서 cs에 `domainrag-ops-admin` role 부여 확인 (Phase 1.4) |
| `no_tenant_membership` 403 | user_tenant_membership 미등록 | Phase 2.4 SQL 실행 또는 admin UI에서 row 추가 |
| `email_not_verified` 403 | AuthFusion 사용자 등록 시 `emailVerified=false` | Phase 1.3 재실행 또는 admin이 사용자 `emailVerified=true`로 갱신 |
| frontend `/login` 무한 redirect | discovery fetch 실패 (DNS / 방화벽) | `curl -sf https://api.aines.kr/.well-known/openid-configuration` 직접 호출 검증 |

---

## 후속 작업 (별도)

- assessment_items 시드 — 정보처리기사 기출 1회분(필기 100문항) 일괄 import (Assessment Console)
- 학습자료 업로드 — `textbook` / `past_exam_explanation` input_type으로 PDF 인덱싱
- KeyHub 결선 시 `.env`의 `AUTHFUSION_CLIENT_SECRET`을 `keyhub://domainrag/oidc-client-secret` 참조로 전환
- AuthFusion Ledger 결선 시 `LEDGER_ENABLE=true` + `LEDGER_API_KEY_REF=keyhub://domainrag/ledger-token`
