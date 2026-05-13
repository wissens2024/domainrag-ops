# ADR-008: Multi-Tenant Architecture

## Status
**Accepted** (2026-05-08)

> 본 ADR은 ADR-006의 단일 Qdrant collection 가정을 **부분 supersede** 한다 (collection 내부 구조는 유지, 단일 collection 가정만 폐기).

---

## Context

### 배경 (사실)

- DomainRAG Ops는 단순 단일 도메인 RAG가 아니라 **테넌트별 도메인 지식 운영 플랫폼**으로 정의됨 (제품 비전 §1, §3).
- SPEC.md v1, ADR-001~007, CLAUDE.md는 모두 **단일 테넌트 가정**으로 작성되어 `tenant_id`가 어디에도 없음.
- 데이터 민감도 높음 (보안 정책, 법무, 시험 문제, 설비 정비) — cross-tenant leak이 사고 수준 위험.
- 폐쇄망 환경, 외부 SSO 연동 (사양 추후 결정).
- 예상 tenant 수: 수십~수백 (조직 단위 다국어 가능).
- Per-tenant 임베딩 모델·프롬프트·라우팅·평가 정책이 다를 수 있음 (비전 §4·§11·§12).

### 가정

- Qdrant가 수백 단위 collection을 안정적으로 운영 (검증된 사실)
- PostgreSQL Row-Level Security(RLS) 활용 가능 (PG 9.5+ 기본 제공)
- 외부 SSO가 JWT 발급하며 token에 `tenant_id` 또는 그에 준하는 claim이 포함됨 (또는 user→tenant 매핑 lookup 가능)
- 관리자 콘솔이 tenant 등록 자동화 작업(collection 생성·configs 시드·bucket 초기화)을 수행 가능

가정 중 하나라도 깨지면 재검토. 특히 SSO claim에 tenant_id가 부재하면 별도 user→tenant 매핑 메타 도입 필요.

---

## Decision

### 1. Vector DB — Collection per Tenant

- Qdrant collection 명명 컨벤션: `chunks_<tenant_id>` (예: `chunks_security`, `chunks_legal`, `chunks_exam_bank`)
- Collection 내부 구조는 ADR-006 그대로: dense(1024) + sparse named vectors, payload에 chunk metadata
- **임베딩 모델·차원이 per-tenant 자유** (보안 도메인 = bge-m3, 법무 도메인 = Korean legal model 가능)
- 새 tenant 등록 시 자동 collection 생성, archive 시 collection drop (admin API)

### 2. PostgreSQL — tenant_id 컬럼 + Row-Level Security (RLS)

- 모든 핵심 테이블(`documents`, `chunks`, `conversations`, `messages`, `chat_logs`, `indexing_jobs`, `feedbacks` 등)에 `tenant_id VARCHAR(64) NOT NULL` 추가
- 모든 인덱스 prefix에 tenant_id 포함 (예: `(tenant_id, doc_id, doc_version, parser_version, chunk_index)` UNIQUE)
- RLS policy:
```sql
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chunks
  USING (tenant_id = current_setting('app.current_tenant')::text);
```
- 세션 시작 시 `SET LOCAL app.current_tenant = '<tenant_id>'`로 컨텍스트 주입
- App-level filter 누락이 cross-tenant leak으로 이어지는 위험을 RLS로 강제 차단
- Cross-tenant 통계가 필요한 admin 작업은 `BYPASSRLS` 권한 가진 별도 role이 명시 호출

### 3. Object Storage (MinIO) — Prefix per Tenant

- bucket: 단일 (`documents`)
- key prefix 컨벤션: `<tenant_id>/<doc_id>/<version>/original.<ext>`
- IAM/policy로 tenant prefix 격리 (요구 시점에 적용)

### 4. Tenant 식별 — JWT Claim + URL Path Mirror (3중 방어)

- **Source of truth**: JWT token의 `tenant_id` claim
- **URL mirror**: `/api/{tenant_id}/chat`, `/api/{tenant_id}/admin/...` 형태로 모든 endpoint
- **Mismatch handling**: token claim ≠ URL path → `403 Forbidden`
- **PostgreSQL 세션**: 인증 통과 시 `SET LOCAL app.current_tenant = '<tenant_id>'`
- **Qdrant 호출**: collection 이름을 `chunks_<tenant_id>`로 동적 결정

```
[Request /api/security/chat + JWT(tenant_id="security")]
        ↓
[AuthAdapter] verify_token → UserContext{user_id, tenant_id, groups, clearance, roles}
        ↓
[Tenant Resolver Node] JWT claim ≠ URL path? → 403
                       PostgreSQL session SET app.current_tenant
                       state.tenant_id = "security"
                       Qdrant collection = "chunks_security"
        ↓
... (이후 LangGraph 노드)
```

### 5. Tenant 등록/제거 운영 절차

#### 등록 (자동화)

새 tenant 추가 시:
1. `tenants` 테이블 row insert
2. Qdrant collection 생성 (`chunks_<tenant_id>`) — per-tenant 임베딩 모델 spec에 따라 dimension/sparse 설정
3. `configs/tenants/<tenant_id>/` 디렉터리 시드 (`input_schema.yaml`, `prompts.yaml`, `model.yaml`, `retrieval.yaml`, `citation.yaml`)
4. MinIO prefix 초기화 (`<tenant_id>/`)
5. IdP에 tenant role/group 등록 — **이 단계만 수동** (SSO 운영자 책임)

#### 제거

- **Soft archive 기본** (ADR-007 패턴): `tenants.status='archived'` + 검색 차단 (chat_logs 보존)
- **Hard delete admin API**: collection drop + tenant scope 모든 row 삭제 + chat_logs 처리 옵션(`keep / mask / delete`)

### 6. LangGraph state에 tenant_id 추가 + tenant_resolver 노드 신설

- `RAGState.tenant_id: str` 필수 필드 (엔트리 직후 채워짐)
- 신규 노드 `tenant_resolver`: LangGraph 진입점 직후 실행, JWT claim → URL 검증 → PostgreSQL/Qdrant 컨텍스트 주입
- 다른 모든 노드는 `state.tenant_id`를 통해 격리된 리소스에 접근

### 7. AuthAdapter Protocol stub

외부 SSO 사양은 추후 결정. 추상 인터페이스만 본 ADR에서 박음:

```python
from typing import Protocol

class AuthAdapter(Protocol):
    def verify_token(self, token: str) -> UserContext: ...

@dataclass
class UserContext:
    user_id: str
    tenant_id: str          # 본 ADR이 의무화
    groups: list[str]
    clearance: str
    roles: list[str]        # ["user", "admin", ...]
    raw_claims: dict        # IdP-specific 추가 데이터
```

구체 구현(SSO + KeyHub 연동)은 사양 받을 때 별도 ADR(가칭 ADR-X: SSO Integration)로 처리.

### 8. tenants 테이블 신설

```sql
CREATE TABLE tenants (
    tenant_id VARCHAR(64) PRIMARY KEY,
    display_name TEXT NOT NULL,
    domain_type VARCHAR(50),           -- security | legal | exam_bank | maintenance | ...
    status VARCHAR(20) DEFAULT 'active',  -- active | archived
    embedding_model VARCHAR(100),      -- per-tenant 모델 mapping
    embedding_dim INT,
    created_at TIMESTAMP DEFAULT NOW(),
    archived_at TIMESTAMP,
    config_path TEXT                   -- configs/tenants/<tenant_id>/
);
```

---

## Consequences

### 긍정적 영향

- 데이터 격리 3중 방어 (collection / RLS / token+path mirror)
- Per-tenant 임베딩 모델·프롬프트·평가 자유도 확보 — 비전 §3·§11·§12 정합
- A/B test, 평가 비교, 마이그레이션이 per-tenant 자연스러움
- ACL filter 누락 같은 코드 실수가 RLS로 차단됨
- AuthAdapter 추상화로 SSO 사양 변경 시 영향 범위 국소화

### 부정적 영향 / 부채

- Qdrant collection 수 = tenant 수 → 운영 모니터링 부담 (collection별 metrics, alerting)
- ADR-006 부분 supersede — 단일 collection 가정 폐기
- 모든 기존 SQL/Qdrant 호출 코드에 tenant 컨텍스트 주입 필요 (마이그레이션 비용 ↑)
- IdP/SSO 사양 미정 — token claim 형태 가정으로 시작, 구현 시 보완
- Cross-tenant 통계는 `BYPASSRLS` role 별도 운영 → 권한 분리 거버넌스 필요

### 후속 작업

- ADR-006에 부분 supersede 노트
- ADR-001/003/004/005/007에 tenant_id 보완 노트
- SPEC.md §6/§7/§8/§10/§11에 `tenant_id` 일괄 추가 (대규모 차이 적용 — 마지막 단계)
- `tenants` 테이블 + RLS policies 작성
- LangGraph `tenant_resolver` 노드 명세 (별도 docstring/구현 가이드)
- AuthAdapter Protocol 정의 (구체 구현은 SSO 사양 후 별도 ADR)
- 관리자 콘솔에 Tenant Management 메뉴 신설 (ADR-009 범위)

---

## Alternatives Considered

### 1. Single collection + tenant_id payload filter
- **장점**: 운영 단순, 1만+ tenant 확장
- **기각 사유**: filter 누락 시 cross-tenant leak. 보안 민감 도메인에 부적합. Per-tenant 임베딩 모델 자유도 0.

### 2. Cluster per tenant (Qdrant 인스턴스 자체 분리)
- **장점**: OS-level 완전 격리
- **기각 사유**: tenant 수 수십~수백에서 인프라 비용 폭증. 고보안 월드급 tenant 한정 Phase 2 검토.

### 3. PostgreSQL schema-per-tenant
- **장점**: 중간 격리, schema-level backup
- **기각 사유**: Migration이 tenant마다 N번. Cross-tenant admin 통계 UNION 부담. RLS가 충분한 대안.

### 4. PostgreSQL database-per-tenant
- **장점**: 강한 격리
- **기각 사유**: Connection pool tenant당 분리. Migration·운영 부담 ↑.

### 5. Tenant 식별을 Header 단독 (X-Tenant-Id)
- **장점**: 단순
- **기각 사유**: 위변조 쉬움. 폐쇄망 보안 도메인 부적합.

### 6. Tenant 식별을 URL subdomain 단독
- **장점**: DNS 격리 가시화
- **기각 사유**: 폐쇄망 DNS 운영 부담 + 다국어 지원 시 복잡. URL path mirror로 가시성은 충분.

---

## Related

- [ADR-001: Citation 메타데이터 설계](./001-citation-metadata-design.md) — chunk/citation 모델에 tenant_id 보완 (보완 노트 추가)
- [ADR-002: Protocol/Adapter 패턴](./002-protocol-adapter-pattern.md) — AuthAdapter Protocol 신규 추가
- [ADR-003: LangGraph 오케스트레이션](./003-langraph-orchestration.md) — `tenant_resolver` 노드 추가 (보완 노트)
- [ADR-004: 보안 & ACL 모델](./004-security-acl-model.md) — tenant_id가 ACL 1차 필터 (보완 노트)
- [ADR-005: Citation 신뢰 모델](./005-citation-trust-and-fallback.md) — tenant 단위 verifier 격리 (보완 노트)
- [ADR-006: Hybrid Retrieval](./006-hybrid-retrieval.md) — **부분 supersede** (단일 collection 가정 폐기, 내부 구조 유지)
- [ADR-007: Document/Chunk Lifecycle](./007-document-chunk-lifecycle.md) — tenant 단위 lifecycle (보완 노트)
- ADR-009 (예정): Tenant Control Plane — 본 ADR 위에 build
- [SPEC.md §6/§7/§8/§10/§11](../../SPEC.md) — 본 ADR로 광범위 갱신 예정 (마지막 단계 일괄)

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted
