# ADR-022: Domain 용어 통일 + User/Domain 접근 모델 재정립

## Status
**Accepted** (2026-05-28)

> 본 ADR은 ADR-018 §1(client≡tenant)·§8(tenant 전환=재로그인)을 **supersede**하고, 운영 중 드러난 개념·코드 불일치를 단일 진실 소스로 재확정한다. ADR-008 격리 원칙은 유지하되 3번째 방어선을 재정의한다.

---

## Context

### 운영에서 드러난 사실 (E2E 검증, 2026-05-28)

- `configs/tenants/security/auth.yaml`·`exam-engineer/auth.yaml` **둘 다 동일 client UUID `6cb9d56c-…`를 공유**. 주석: "DomainRAG Ops single-client". → ADR-018 §1 "client ≡ tenant" 는 **이미 깨져 있다**.
- IdP(AuthFusion)는 **사람당 단일 세션**. tenant 개념을 모른다. tenant 구분은 **SP(DomainRAG)에만** 존재.
- 공용 client이므로 토큰의 `client_id`로는 tenant를 구별할 수 없다 → ADR-008 §2 / ADR-018 §3의 "JWT claim·URL path mirror(client_id 기반 tenant_mismatch)" 방어선이 **실효(死)**.
- 실제 접근 차단은 `user_tenant_membership` 조회(없으면 403 `no_tenant_membership`)가 담당하고 있었다. 그런데 **admin(`domainrag-ops-admin`) 계정도 membership row가 없으면 403** — 콘솔이 "껍데기는 ADMIN인데 안은 전부 403"이 되는 원인.

### 결정 동기

- 위 불일치 때문에 세션이 바뀐 작업자(사람·AI)가 ADR을 믿고 작업하면 매번 어긋났다. **문서와 실제를 일치**시켜 재발을 막는다.
- "도메인별 전문화"가 제품의 본질인데, 사용자에게 `tenant`라는 인프라 용어가 노출되어 있었다.

---

## Decision

### 1. 용어: 사용자에게는 "도메인", 내부에는 `tenant_id`

| 단어 | 청중 | 의미 |
|---|---|---|
| **도메인 (domain)** | 사용자·관리자·UI·운영 문서 | 전문화된 지식 분야 (보안·법무·시험 등). |
| **tenant / `tenant_id`** | 코드·DB·인프라·ADR 내부 | 격리 단위. PostgreSQL RLS·Qdrant collection·MinIO prefix의 1차 분기 키. |

> **용어집(단일 진실 소스): `tenant`(격리 단위) = `domain`(사용자 표현). 본 시스템에서 1:1 동일.**

- **사용자-facing 표면(UI 문구·라벨·운영 매뉴얼)은 전부 "도메인"으로 쓴다.**
- 내부 식별자 `tenant_id`는 당장 유지하고, **물리 개명(tenant_id→domain_id)은 별도의 통제된 마이그레이션**으로 수행한다(§7). 개념·이름 결정은 본 ADR로 확정됐으므로, 개명은 기계적 작업이다.

### 2. 원칙: IdP=인증, SP=인가

- **IdP(AuthFusion)**: 정체성·인증·기본 역할(`domainrag-ops-admin/auditor/user`)의 system of record. tenant/도메인을 모른다. 토큰은 `sub` + 역할만 담는 얇은 토큰.
- **SP(DomainRAG)**: "누가 어느 도메인에서 무엇을 할 수 있나"(인가)의 system of record. `user_tenant_membership`이 그 저장소.
- SP는 사용자 **정체성을 소유하지 않는다**. `sub`을 외래키로 가리킬 뿐, 자격증명·프로필 원본은 IdP에 둔다. 관리 UI의 사용자 목록은 AuthFusion users API로 **조회**해 표시하고, 선택 결과 `sub`만 membership에 기록한다(표시용 thin projection 캐시는 복제본일 뿐 원본 아님).

### 3. 역할 모델 — admin/auditor는 전역, user는 도메인-스코프

| 역할 | 출처 | 접근 범위 | membership 필요? |
|---|---|---|---|
| **admin** (`domainrag-ops-admin`→ADMIN) | IdP 토큰 | **모든 도메인** 조회·관리(read-write) | ❌ 전역 |
| **auditor** (`domainrag-ops-auditor`→AUDITOR) | IdP 토큰 | **모든 도메인** 조회·감사(**read-only**) | ❌ 전역 |
| **user** (`domainrag-ops-user`→USER) | IdP 토큰 + membership | 기본 도메인 자동 + 배정된 도메인 | ✅ (assigned 도메인 한정) |
| **platform_admin** (`PLATFORM_ADMIN`) | IdP 토큰 | 모든 도메인 + `/platform/admin/*` | ❌ 전역 |

- **전역 역할(admin·auditor·platform_admin)은 `user_tenant_membership` 게이트를 우회**한다. membership row가 없어도 통과한다. 이것이 "콘솔 진입 시 403 없이 전 도메인이 보인다"의 구현.
- 전역 역할의 기본 clearance: **admin/platform_admin = `secret`(전체 문서 가시), auditor = `secret`(read-only)**. membership row가 존재하면 그 값을 우선한다(더 구체적).
- **auditor는 read-only**: 쓰기(관리) 엔드포인트는 `require_admin`이 막는다(auditor는 `is_admin=false`). 조회 엔드포인트는 별도 read 권한으로 허용한다.
- **per-도메인 역할은 도입하지 않는다.** "security엔 admin, legal엔 user" 같은 도메인별 역할 차등은 본 시스템 범위가 아니다. 따라서 ADR 검토 과정에서 거론된 `membership.role` 컬럼 추가는 **채택하지 않는다**.

### 4. 도메인 enrollment 정책 — open vs assigned

- `tenants`에 `enrollment_policy ∈ {open, assigned}` 추가.
- **open**: 인증된 모든 user가 자동 접근. 첫 진입 시 user membership을 **JIT 생성**(role 개념상 USER, clearance `internal`). 모든 접근이 단일 membership 경로로 흐르고 감사·revoke가 동일하게 동작(우회 코드 경로 금지).
- **assigned**: 관리자가 명시 배정한 user만 접근. 폐쇄·전문 도메인 기본값.
- **기본 도메인(첫번째) = `general`, open 정책.** 모든 user가 최소 여기엔 착지한다. `security` 등 특수 도메인은 `assigned`.

### 5. 부트스트랩 — 빈손 사용자 & 최초 관리자

- 로그인(인증)은 누구나 성공한다. membership/도메인 접근(인가)과 분리한다.
- membership이 하나도 없고 전역 역할도 아닌 user는, raw 403 대신 **"아직 배정된 도메인 없음 — 관리자에게 접근 요청"** 빈 상태 화면으로 착지(프론트 처리).
- 닭-달걀 해소: **`platform_admin`은 membership 없이 통과**하므로 최초 platform_admin을 seed로 심고, 그가 나머지를 배정/관리한다.

### 6. 격리 모델 갱신 (절대원칙 2 재정의)

ADR-008 "격리 3중 방어"의 3번째 다리를 교체한다.

| | 기존 (ADR-008) | 본 ADR |
|---|---|---|
| 1 | collection-per-tenant | (유지) |
| 2 | PostgreSQL RLS | (유지) |
| 3 | **JWT client_id ≡ tenant + URL path mirror (mismatch 403)** | **membership/path 게이트 (URL path가 활성 도메인 결정 + 전역역할 우회 / 일반 user는 membership 없으면 403)** |

- 한 요청은 **항상 단일 도메인**(URL path가 결정 → RLS `app.current_tenant` 세팅). cross-도메인 검색은 그대로 미지원(원칙 1).
- 공용 client 기반의 `tenant_mismatch`(client_id↔path) 검사는 **거짓 보안**이므로 제거한다. (전역역할 우회 + 일반 user membership 게이트가 대체)

### 7. 멀티 도메인 사용자 UX

- 멀티 소속 = membership row 여러 개(또는 전역 역할). **활성 도메인 = URL path.** 서버에 숨은 "현재 도메인" 상태 없음.
- `GET /api/me/domains`: 내가 접근 가능한 도메인 목록(admin/auditor/platform_admin=전체, user=기본+배정). 프론트 switcher가 이를 그린다.
- 전환은 **재로그인 없이** `/{다른도메인}/`로 이동. 착지 규칙: 1개면 바로, 여러 개면 last-used→없으면 기본(`general`).
- **활성 도메인은 화면 상단에 항상 보이는 1급 요소**(칩 + switcher). 멀티도메인 RAG에서 "어느 도메인에 묻는가"는 답의 의미 자체이므로 숨기지 않는다.
- 권한(메뉴·admin 진입)은 **활성 도메인 기준으로 재해석**한다. 현 `/api/auth/me`의 전역 단일 `is_admin` 의존은 멀티도메인에서 틀리므로 메뉴 후보 판단용으로만 쓴다.
- 시스템은 질문을 보고 도메인을 **추측하지 않는다**(cross-domain 추론 = 원칙 1 위반). 도메인 선택은 항상 사용자의 명시적 행위.

### 8. tenant_id → domain_id 물리 개명 (별도 마이그레이션)

- 범위: DB 컬럼·RLS 정책·`app.current_tenant` GUC·Qdrant collection명·MinIO prefix·코드·configs·전 ADR·CLAUDE.md.
- 위험: `tenant_id`는 격리 키 → 오류 시 cross-domain 누출(1순위 금지사항). repo 최대 blast radius + 115 실데이터 마이그레이션.
- 통제: **testcontainers RLS 통합테스트를 "누출 0" 가드**로 두고, 단일 마이그레이션으로 수행. 다른 Phase 안정화 후 별도 진행.

### 9. SaaS posture

- 현 격리 primitive(collection/RLS/prefix-per-X)는 SaaS가 요구하는 구조와 동일. SaaS 전환 시 **위에 "고객사/조직(Org)" 층을 추가**하는 것이지 재설계가 아니다. 그 Org가 진짜 의미의 tenant가 된다.
- 본 ADR은 그 Org 층을 **지금 구현하지 않는다**(완제품 단일 설계·추측 설계 금지, 원칙 6). 다만 사용자-facing 명칭을 "도메인"으로 비워둠으로써 미래 Org 층과 이름 충돌을 막아 **forward-compatible**하게만 둔다.

---

## Consequences

### 긍정적

- 문서(ADR)와 실제 코드가 일치 → 세션 교체 시 재발하던 혼선 제거.
- admin/auditor가 membership 없이 전 도메인 운영 가능 → 콘솔 403 해소(임시 SQL seed 불필요).
- 사용자에게 "도메인"이라는 의미 있는 용어 노출, 멀티도메인 전환이 재로그인 없이 자연스러움.
- 인가가 SP 단일 소스 → 도메인 권한 변경 즉시 반영.

### 부정적 / 부채

- 전역 admin/auditor는 "전부 보임" — 도메인별 관리자 분리가 필요해지면 별도 ADR로 per-도메인 역할 재도입해야 함.
- 전역 역할에 기본 `secret` clearance 부여 = 광범위 권한. 운영상 admin/auditor 발급을 신중히.
- §8 물리 개명 전까지 코드의 `tenant_id`와 UI "도메인" 용어가 병존(용어집으로 완화).

### 후속 작업

- backend: `verify_and_extract` 전역역할 membership 우회 + `UserContext.is_auditor` + DIAG 제거 (Phase1).
- `tenants.enrollment_policy` 마이그레이션 + `general` 기본 도메인 + JIT + `GET /api/me/domains` (Phase2).
- frontend: 도메인 칩+switcher, 활성 도메인별 RBAC, 빈손 착지 (Phase2).
- admin 도메인 관리 화면 + 사용자 배정(AuthFusion users 조회) (Phase3).
- §8 물리 개명 (Phase4).
- CLAUDE.md: ADR Index에 ADR-022 추가, 절대원칙 2 갱신, ADR-018 Status를 `Superseded(부분: §1·§8) by ADR-022`로.

---

## Alternatives Considered

1. **tenant_id를 그대로 유지, 용어도 tenant** — 기각: SaaS 시 "고객사" 단어와 충돌, 사용자에게 무의미한 jargon.
2. **per-도메인 역할(membership.role)** — 기각: 사용자 결정으로 admin/auditor는 전역. 불필요한 복잡도.
3. **로그인 자체를 membership으로 차단** — 기각: 인증/인가 혼합, 얇은 IdP 원칙 위반.
4. **open 도메인에서 membership 검사 우회(JIT 없이)** — 기각: 별도 코드 경로 = 감사·revoke 불가, 일관성 깨짐.
5. **client≡tenant로 되돌려 tenant당 client 재등록** — 기각: AuthFusion에 도메인마다 client 등록 + 전환마다 재로그인, UX·운영 부담.

---

## Related

- [ADR-004: 보안 & ACL 모델](./004-security-acl-model.md) — clearance 레벨
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — 격리 원칙(3번째 다리 본 ADR로 갱신)
- [ADR-018: SSO Integration](./018-sso-integration-authfusion.md) — §1·§8 본 ADR로 supersede
- [ADR-019: Infrastructure Sharing](./019-infrastructure-sharing.md) — RLS·collection·prefix

---

**작성자**: AI Assistant + Project Owner
**최종 승인**: 2026-05-28
**변경 이력**: 초안 작성, 즉시 Accepted (운영 E2E 검증 기반 개념 재확정)
