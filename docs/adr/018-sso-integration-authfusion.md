# ADR-018: SSO Integration with AuthFusion (OIDC, client_id ≡ tenant_id)

## Status
**Accepted** (2026-05-08)

> 본 ADR은 ADR-008 §7의 AuthAdapter Protocol stub을 **외부 시스템 AuthFusion Platform과의 구체 OIDC 통합으로 완성**한다. ADR-008이 정의한 multi-tenant 격리·tenant_id 1차 분기 정책을 AuthFusion의 OAuth2 client 모델로 구현한다.

---

## Context

### 배경 (사실)

- **ADR-008 §7**은 AuthAdapter Protocol과 UserContext stub만 정의. 구체 SSO 구현은 외부 시스템 사양 확정 시 별도 ADR로 미룸.
- 외부 시스템 **AuthFusion Platform** (`C:\dev\workspace\authfusion-platform`)이 **자체 OIDC Provider** (Spring Boot, port 8081)를 운영. CC 인증 목표 폐쇄망 IAM.
- AuthFusion은 RFC 표준 OIDC: Authorization Code + PKCE, JWKS 공개 (`/.well-known/jwks.json`), JWT RS256.
- **AuthFusion JWT claim에 `tenant_id`가 없음** (단일 테넌트 가정으로 설계됨). 표준 claims: `iss`, `sub`, `aud`, `client_id`, `roles`, `preferred_username`, `email`.
- DomainRAG는 **multi-tenant first-class** (ADR-008 원칙 1) — 모든 호출에 `tenant_id` 1차 분기 필요.
- **사용자 결정**: "service와 테넌트를 동일하게 매핑한다" — AuthFusion OAuth2 *client* 1개를 DomainRAG *tenant* 1개로 대응.
- AuthFusion `TokenClaims.java` 수정은 **TOE 범위 변경**으로 CC 인증 영향 우려 — 가능하면 회피.

### 가정

- AuthFusion이 본 시스템과 동일한 폐쇄망에 있고 JWKS endpoint에 도달 가능
- AuthFusion이 OAuth2 client를 tenant 단위로 별도 등록 가능 (예: `client-security`, `client-legal`)
- Discovery endpoint (`/.well-known/openid-configuration`)가 정상 노출
- 시스템 호출(평가 러너, indexing worker)을 위한 service account를 AuthFusion이 client_credentials grant로 지원
- 한 사람이 여러 tenant 소속 시 tenant별 client에 별도 등록·로그인 행위 수용 가능

가정 깨지면 재검토 — 특히 client_credentials 미지원 또는 JWKS 미공개 시 ADR-008 path mirror 정합 깨짐.

---

## Decision

### 1. AuthFusion OAuth2 client ≡ DomainRAG tenant

```text
AuthFusion OAuth2 clients         DomainRAG tenants
  client-security             ↔     tenant_id="security"
  client-legal                ↔     tenant_id="legal"
  client-exam_bank            ↔     tenant_id="exam_bank"
  ...
```

- 신규 tenant 등록 자동화(ADR-008 §4)에 **AuthFusion OAuth2 client 등록 단계 포함** (자동 가능 시 admin API, 아니면 수동 절차 명문화).
- `client_id` 명명 규칙: `client-<tenant_id>` (또는 그대로 tenant_id). 매핑은 platform-level config(`configs/platform/auth.yaml`)로 관리.
- 매핑 함수:

```python
def client_id_to_tenant_id(client_id: str) -> str:
    # configs/platform/auth.yaml의 mapping table 우선
    # fallback: prefix "client-" 제거
    return AUTH_CONFIG.client_tenant_map.get(client_id) \
        or client_id.removeprefix("client-")
```

### 2. 인증 흐름 — Authorization Code + PKCE

```text
1) 사용자 → /{tenant_id}/chat 진입 (DomainRAG Frontend)
2) AuthGuard: 토큰 없으면 AuthFusion authorize endpoint로 redirect
   - client_id = configs.tenants.<tenant_id>.auth.client_id
   - redirect_uri = https://domainrag.local/auth/callback
   - scope = "openid profile email"
   - code_challenge / code_challenge_method=S256
3) 사용자 AuthFusion에서 인증
4) AuthFusion → /auth/callback?code=...
5) DomainRAG Backend: POST AuthFusion /oauth2/token (code + code_verifier)
   - 응답: access_token (JWT), refresh_token, expires_in
6) Frontend는 access_token을 httpOnly Secure 쿠키로 보관 (XSS 방어)
7) 이후 모든 API 호출은 Authorization: Bearer <access_token>
```

### 3. JWT 검증 (AuthFusionAdapter 구체 구현)

```python
class AuthFusionAdapter(AuthAdapter):  # ADR-008 §7 protocol
    def __init__(self, config: AuthConfig):
        self.issuer = config.issuer  # https://sso.aines.kr
        self.jwks_client = JWKSClient(f"{self.issuer}/.well-known/jwks.json", cache_ttl=3600)
        self.client_tenant_map = config.client_tenant_map

    async def verify_and_extract(self, bearer_token: str, expected_tenant_id: str) -> UserContext:
        # 1) JWKS로 RS256 서명 검증 + iss/exp 검증
        claims = jwt.decode(bearer_token, self.jwks_client.get_key(...), algorithms=["RS256"], issuer=self.issuer)
        # 2) claim에서 tenant_id 추출 (client_id 매핑)
        token_tenant_id = self.client_id_to_tenant_id(claims["client_id"])
        # 3) URL path tenant_id와 일치 검증 (ADR-008 path mirror)
        if token_tenant_id != expected_tenant_id:
            raise HTTPException(403, "tenant_mismatch")
        # 4) UserContext 빌드
        return UserContext(
            user_id=claims["sub"],
            tenant_id=token_tenant_id,
            roles=claims.get("roles", []),
            preferred_username=claims.get("preferred_username"),
            email=claims.get("email"),
            raw_claims=claims,
        )
```

JWKS는 in-memory 캐시 (TTL=1h). 키 rotation 시 `kid` 매칭 안 되면 자동 refetch.

### 4. clearance / department / 도메인 권한 — DomainRAG 자체 보강

AuthFusion claim에 없음. DomainRAG에 신설 테이블:

```sql
-- domainrag database
CREATE TABLE user_tenant_membership (
    id UUID PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,           -- AuthFusion sub claim
    tenant_id VARCHAR(64) NOT NULL,          -- DomainRAG tenant
    clearance VARCHAR(50) NOT NULL,          -- public|internal|confidential|secret (ADR-004)
    department VARCHAR(100),
    domain_groups JSONB DEFAULT '[]',        -- 도메인 ACL용 그룹 (예: ["group:security", "group:it-admin"])
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (user_id, tenant_id)
);
CREATE INDEX idx_user_tenant ON user_tenant_membership (user_id, tenant_id) WHERE is_active=TRUE;
```

`AuthFusionAdapter.verify_and_extract`가 JWT 검증 후 본 테이블 조회로 `clearance`/`department`/`domain_groups`를 UserContext에 보강.

이 테이블의 RLS는 적용하지 않음 (테이블 자체가 cross-tenant 매핑 정의이므로). 변경은 platform_admin만 가능.

### 5. UserContext 최종 형태 (ADR-008 §7 구체화)

```python
@dataclass(frozen=True)
class UserContext:
    user_id: str                    # AuthFusion sub
    tenant_id: str                  # DomainRAG tenant
    roles: list[str]                # AuthFusion roles claim (e.g., ["USER", "ADMIN"])
    clearance: str                  # DomainRAG user_tenant_membership
    department: str | None
    domain_groups: list[str]        # ACL용 (ADR-004)
    preferred_username: str
    email: str | None
    raw_claims: dict                # 원본 JWT claims (감사용)
```

`roles`는 ADR-016 메뉴 권한·platform_admin 판정에 사용. `domain_groups`는 ADR-004 ACL filter에 사용.

### 6. Token Refresh / Logout

```text
POST /api/auth/refresh
  Body: { "refresh_token": "..." }
  → AuthFusion /oauth2/token (grant_type=refresh_token) 위임
  → 응답: 새 access_token, refresh_token

POST /api/auth/logout
  → AuthFusion /oauth2/revoke 호출 + httpOnly 쿠키 삭제
  → AuthFusion의 token_version (FCS_COP.1)으로 즉시 무효화
```

### 7. Service Account (시스템 호출)

평가 러너·indexing worker·cron 작업 등 사람 없는 호출:

```text
client_credentials grant:
  POST AuthFusion /oauth2/token
    grant_type=client_credentials
    client_id=service-domainrag-eval
    client_secret=<KeyHub로 보관, ADR-019>
  → access_token (sub은 service account, roles=["SERVICE"])

AuthFusionAdapter는 roles에 "SERVICE" 포함 시 user_tenant_membership 조회 우회하고
  configs/platform/auth.yaml의 service_accounts 매핑으로 tenant_id/clearance 결정.
```

### 8. 한 user 다수 tenant 소속

- AuthFusion에 같은 user가 여러 OAuth2 client에 등록될 수 있음
- DomainRAG는 한 번에 하나의 active tenant만 인지 (URL path가 결정)
- 다른 tenant로 전환은 **새 OIDC login flow** (다른 client_id로 authorize) — frontend의 tenant selector가 client_id를 바꿔 재로그인 트리거
- 이중 token을 frontend에 동시 보관하지 않음 — 단순성·보안 우선

### 9. Dev / Mock 모드

`configs/platform/auth.yaml`:

```yaml
auth_mode: authfusion              # authfusion | mock
authfusion:
  issuer: https://sso.aines.kr
  jwks_uri: https://sso.aines.kr/.well-known/jwks.json
  token_endpoint: https://sso.aines.kr/oauth2/token
  revoke_endpoint: https://sso.aines.kr/oauth2/revoke
  client_tenant_map:
    client-security: security
    client-legal: legal
mock:
  default_user:
    user_id: dev-user-001
    tenant_id: security
    roles: ["USER", "ADMIN"]
    clearance: confidential
    department: security
    domain_groups: ["group:security"]
```

`auth_mode: mock`일 때 `MockAuthAdapter`가 토큰 검증을 우회하고 default_user를 주입. 운영 환경에서는 절대 사용 금지 (config validator가 prod 환경에서 mock 거부).

### 10. AuthFusion 등록 자동화 (tenant 신규 등록)

ADR-008 §4의 tenant 등록 자동화 단계에 추가:

```text
0) tenant_id 결정·검증
1) AuthFusion OAuth2 client 등록 (Admin API: POST /api/v1/clients)  ← 신규
2) configs/tenants/<tenant_id>/auth.yaml 생성
3) Qdrant collection 자동 생성
4) MinIO prefix 초기화
5) configs 시드 + RLS context 검증
6) (수동) IdP 사용자 → 새 client에 등록
```

step 1 자동화는 AuthFusion Admin API 호출. 실패 시 rollback.

---

## Consequences

### 긍정적 영향

- AuthFusion `TokenClaims.java` 수정 0 — CC 인증 영향 회피
- ADR-008 path mirror와 자연 정합 (`/{tenant_id}/...` ↔ `client-<tenant_id>`)
- Multi-tenant user는 명시적 tenant 선택(=다른 client 로그인)으로 격리 명확
- AuthFusionAdapter Protocol 구현으로 Mock/AuthFusion 둘 다 동일 인터페이스 — 테스트·dev 단순
- clearance/department를 DomainRAG에 격리 — 도메인 권한 변경 시 즉시 반영 (AuthFusion token 갱신 대기 없음)

### 부정적 영향 / 부채

- AuthFusion 가용성 직접 의존 — JWKS 캐시·refresh token으로 일부 완화하나 장애 시 새 로그인 차단
- `user_tenant_membership` 테이블 운영 필요 (platform_admin이 관리)
- 한 user의 tenant 전환이 매번 OIDC re-login → UX 다소 무거움 (단, 보안 정책상 수용)
- service account secret(KeyHub 보관) 운영 부담

### 후속 작업

- `configs/platform/auth.yaml` 신설 (위 schema)
- `configs/tenants/<tenant_id>/auth.yaml` 시드 (등록 자동화에 포함)
- backend `AuthFusionAdapter` 구현 + `MockAuthAdapter` 구현
- `user_tenant_membership` 테이블 Alembic migration
- Tenant 등록 자동화 스크립트(`infra/scripts/tenant_register.sh`)에 AuthFusion client 등록 step 추가
- ADR-017 `/api/auth/*` endpoint 본 ADR 구현으로 채움
- platform_admin 판정 로직 — `roles` claim에 `PLATFORM_ADMIN` 포함 여부

---

## Alternatives Considered

### 1. AuthFusion에 `tenant_id` custom claim 추가 요청
- **장점**: token 한 번에 모든 정보, lookup 불필요
- **기각 사유**: TokenClaims.java 수정 = TOE 범위 변경, CC 인증 영향 우려, AuthFusion의 다른 클라이언트도 영향. 사용자 directive("service와 테넌트 동일 매핑")로 회피.

### 2. DomainRAG 자체 user→tenant 매핑 테이블 (client_id 미사용)
- **장점**: AuthFusion 변경 0
- **기각 사유**: tenant 등록 시 AuthFusion에 client 등록은 어차피 필요. `client_id` claim을 활용하면 source of truth가 자연스럽게 일치. 매핑 불일치 위험 ↓.

### 3. KeyHub 또는 별도 서비스에서 매 요청마다 lookup
- **장점**: 모든 도메인 권한이 외부 시스템에 위임
- **기각 사유**: 매 요청 latency·외부 가용성 의존. clearance 같은 도메인 정책은 DomainRAG 측 컨트롤이 합리.

### 4. SAML 또는 자체 JWT 발급
- **장점**: 더 단순한 일부 케이스
- **기각 사유**: AuthFusion이 이미 OIDC 표준 운영 중 — 표준 활용이 운영·감사 측면에서 우수.

### 5. Mock 모드를 운영에서도 허용 (config flag)
- **장점**: 디버깅 편의
- **기각 사유**: 보안 사고 위험. config validator가 prod 환경에서 mock 거부.

---

## Related

- [ADR-002: Protocol/Adapter 패턴](./002-protocol-adapter-pattern.md) — AuthAdapter Protocol
- [ADR-004: 보안 & ACL 모델](./004-security-acl-model.md) — clearance/domain_groups 활용
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — §7 stub의 구체 구현
- [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md) — tenant config로 client_id 매핑
- [ADR-016: UI Architecture](./016-ui-architecture.md) — login redirect / tenant selector
- [ADR-017: API Specification](./017-api-specification.md) — `/api/auth/*` endpoint 본 ADR로 구현
- [ADR-019: Infrastructure Sharing](./019-infrastructure-sharing.md) — AuthFusion·KeyHub·PostgreSQL 공유 가정
- AuthFusion `docs/integration/OIDC-INTEGRATION-GUIDE.md` (외부)

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted (ADR-008 §7 stub 완성)
