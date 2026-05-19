# CLAUDE.md

이 프로젝트는 **DomainRAG Ops** — 폐쇄망 GPU 기반 **멀티테넌트 RAG 플랫폼**이다.

---

## 한 문장 정의

DomainRAG Ops는 **테넌트별 입력 스키마**로 도메인 지식을 구조화하고, **멀티 신호 RAG 검색**과 **SLM/LLM 라우팅**으로 답변하며, **4-type claim-level citation**으로 근거를 설명하는 멀티테넌트 RAG 플랫폼이다.

## 정체

- 단순 RAG 챗봇이 아님
- 단일 도메인 시스템이 아님
- MVP/Phase 분리 없음 — **완제품 단일 설계**
- "추후 검토" 처리 항목 없음 (모든 결정은 day 1)
- 테넌트별 지식 운영 플랫폼
- 4-type 설명 가능한 RAG (direct·종합·추론·충돌)
- SLM + LLM 하이브리드, LoRA per-tenant
- Assessment 도메인 별도 워크플로우 (시험 문제 출제·생성·검증)

---

## 핵심 사용자

| 역할 | scope | 주요 작업 |
|---|---|---|
| 일반 사용자 | tenant scope | 채팅, 답변·근거 확인, 피드백 |
| Tenant Admin | 자기 tenant | 문서·item 등록, 인덱싱, 로그, configs 편집, prompt·LoRA·routing 관리 |
| platform_admin | 모든 tenant | 테넌트 등록·hard delete, platform configs, 모델 endpoint 관리, cross-tenant 통계 |
| 운영자 | observability | 헬스·latency·실패율 모니터링 |
| 개발자 | 기술 | Adapter 추가, 모델 평가·승격, schema 진화 |

---

## 절대 원칙

1. **멀티테넌트 first-class**: 모든 데이터·정책·검색·로그에 `tenant_id` 1차 분기. cross-tenant 검색은 본 시스템에서 지원하지 않는다.
2. **격리 3중 방어**: collection-per-tenant + PostgreSQL Row-Level Security + JWT claim·URL path mirror (mismatch 시 403).
3. **4-type citation**: direct / synthesis / inference / conflict. 마커 syntax는 `[1]` / `[종합: 1,2,3]` / `[추론: 1,2,3] 🔍` / `[충돌: 1 vs 2]` (ADR-010).
4. **검증된 인용만 노출** (chat_structured 모드 한정): `support_level`/`verified` 필드는 ADR-010 산출 규칙으로 계산. weak는 응답에서 제거, medium은 ⚠ 호버. **chat_streaming 모드는 RAG 미사용 자유 대화 모드로 citation 책임 없음** (`citation_disabled: true`, ADR-013 §6).
5. **원칙 8 (재해석)**: 근거 없는 추측은 금지한다. 다만 (a) 직접 인용, (b) 검증된 종합, (c) **명시적 추론 + LLM-as-judge 통과 + 강한 caveat**, (d) 명시적 충돌 표시는 허용. 그 외는 "확인 불가" fallback.
6. **완제품 단일 설계**: ADR/코드에 "MVP", "Phase 2", "추후 검토", "MVP 한정" 같은 phasing 표현 금지. 기능을 *제외*하는 결정은 가능하지만 명시적 "본 시스템에서 X는 지원하지 않는다"로 못 박는다.
7. **Protocol/Adapter 패턴**: LLMClient · Embedder · Retriever · Reranker · VectorStore · DocumentParser · Chunker · AuthAdapter 모두 Protocol. 구현체 교체는 configs 변경.
8. **Configs 분리**: 모델·임계치·prompt·라우팅 모두 platform/tenants 계층 yaml + DB hybrid (ADR-009). 코드 하드코딩 금지.
9. **chat_logs는 운영의 토대**: 모든 응답이 `tenant_id`, `confidence`, `citation_types`, `routing_decision`, `model_failure_chain`, `verifier_metrics`, `ui_mode` 포함하여 저장.
10. **chat_logs.excerpt가 audit truth**: chunk 비활성화·archive·hard delete와 무관하게 답변 시점의 인용 원문은 보존된다.
11. **타입힌트 / docstring / 테스트 의무**: 핵심 로직은 pydantic·protocol·pytest 셋 다 충족.
12. **외부 API 금지**: OpenAI 등 외부 LLM API를 기본값으로 사용 금지. 폐쇄망 vLLM이 기본.

---

## 단일 진실 소스 — ADR Index

모든 의사결정은 ADR이 단일 진실 소스다. SPEC.md / IMPLEMENTATION_SPEC.md는 폐기되었다.

| ADR | 주제 | Status |
|---|---|---|
| [ADR-001](docs/adr/001-citation-metadata-design.md) | Citation 메타데이터 스키마 | Accepted (보완: tenant_id) |
| [ADR-002](docs/adr/002-protocol-adapter-pattern.md) | Protocol/Adapter 패턴 | Accepted |
| [ADR-003](docs/adr/003-langraph-orchestration.md) | LangGraph 오케스트레이션 | Accepted (보완: tenant_resolver, Gate 1/2) |
| [ADR-004](docs/adr/004-security-acl-model.md) | 보안 & ACL 모델 | Accepted (보완: tenant_id 1차) |
| [ADR-005](docs/adr/005-citation-trust-and-fallback.md) | Citation 신뢰 v1 | Superseded by ADR-010 |
| [ADR-006](docs/adr/006-hybrid-retrieval.md) | Hybrid Retrieval v1 | Superseded by ADR-011 |
| [ADR-007](docs/adr/007-document-chunk-lifecycle.md) | Lifecycle v1 | Superseded by ADR-012 |
| [ADR-008](docs/adr/008-multi-tenant-architecture.md) | Multi-Tenant Architecture | Accepted |
| [ADR-009](docs/adr/009-tenant-control-plane.md) | Tenant Control Plane | Accepted |
| [ADR-010](docs/adr/010-citation-trust-v2.md) | **Citation Trust v2** (4-type) | Accepted |
| [ADR-011](docs/adr/011-hybrid-retrieval-v2.md) | **Hybrid Retrieval v2** (multi-tenant) | Accepted |
| [ADR-012](docs/adr/012-lifecycle-v2.md) | **Lifecycle v2** (multi-tenant + complete) | Accepted |
| [ADR-013](docs/adr/013-model-routing-and-slm-llm-strategy.md) | Model Routing & SLM/LLM | Accepted |
| [ADR-014](docs/adr/014-assessment-workflow.md) | Assessment Workflow | Accepted |
| [ADR-015](docs/adr/015-tenant-input-schema.md) | Tenant Input Schema | Accepted |
| [ADR-016](docs/adr/016-ui-architecture.md) | UI Architecture | Accepted (SPEC §5 흡수) |
| [ADR-017](docs/adr/017-api-specification.md) | API Specification | Accepted (SPEC §8 흡수) |
| [ADR-018](docs/adr/018-sso-integration-authfusion.md) | SSO Integration with AuthFusion (OIDC, client_id≡tenant_id) | Accepted (ADR-008 §7 stub 완성) |
| [ADR-019](docs/adr/019-infrastructure-sharing.md) | Infrastructure Sharing & Resource Allocation | Accepted (RLS·임베딩 마이그레이션 절차 흡수) |
| [ADR-020](docs/adr/020-pii-and-audit-integration.md) | PII Detection & Audit Integration | Accepted (M1 PII 공백 해결) |
| [ADR-021](docs/adr/021-operational-bootstrap.md) | Operational Bootstrap (lifespan·LISTEN/NOTIFY·cron·Docker) | Accepted |

작업 시작 전에 **관련 ADR을 먼저 읽는다**. 다중 ADR이 관련되면 *최신·미-supersede ADR*을 단일 진실 소스로 따른다.

---

## 아키텍처 개요

```
[Frontend: Next.js]                  /{tenant_id}/chat, /{tenant_id}/admin/*, /platform/admin/*
       ↓
[API Layer: FastAPI]                 /api/{tenant_id}/..., /api/platform/admin/...
  - AuthAdapter (JWT verify, UserContext)
  - tenant_resolver (path mirror 검증, RLS context)
       ↓
[RAG Orchestration: LangGraph]
  classify_query → model_router → query_rewrite (조건부) → retrieve_context
    → Gate 1 → generate (sync/streaming) → parse_response
    → verify_tier1/tier2 → detect_unsupported → judge_inference (조건부)
    → assemble_response → Gate 2 → save_chat_log
       ↓
[Retrieval Layer]                    chunks_<tenant_id> + items_<tenant_id>
  - bge-m3 dense + sparse (named vectors)
  - Qdrant DBSF fusion (α=β=0.5 기본, configs)
  - per-tenant reranker override 옵션
       ↓
[Knowledge Layer]
  - PostgreSQL (tenant_id + RLS)
  - Qdrant (collection-per-tenant)
  - MinIO (prefix-per-tenant)
       ↓
[Inference Layer]
  - Tenant SLM (vLLM multi-tenant + LoRA per-request)
  - Shared LLM (vLLM 별도 인스턴스, fallback + inference judge)
  - Embedding model (per-tenant 가능)
  - Reranker (cross-encoder)
       ↓
[Tenant Control Plane]               configs/platform/* + configs/tenants/<id>/* + DB overrides
  - input_schema, prompts, model.yaml, retrieval.yaml, routing.yaml, citation.yaml, lifecycle.yaml
```

---

## 절대 금지사항

| # | 금지 | 위반 시 영향 |
|---|---|---|
| 1 | `tenant_id` 빠뜨림 (모든 layer 1차 분기) | cross-tenant data leak |
| 2 | citation을 chunk_id/item_id 없이 만들기 | audit replay 불가 |
| 3 | `support_level`/`verified` placeholder ("strong"/true 하드코딩) | 검증 의미 공허, 사용자 신뢰 깨짐 |
| 4 | inference citation을 LLM-as-judge 없이 노출 | 원칙 8 (재해석) 위반, hallucination 노출 |
| 5 | 모델명/endpoint/임계치를 코드에 하드코딩 | configs 분리 원칙 위반, 운영자 변경 불가 |
| 6 | ACL filter 없이 검색 (RLS는 안전망일 뿐) | 명시적 ACL 미적용 = 코드 리뷰 reject |
| 7 | RAG와 Assessment를 같은 데이터 모델로 | mismatched abstraction, ADR-014 위반 |
| 8 | LangGraph 노드에 service 로직 몰아넣기 | ADR-003 위반, 테스트·재사용 불가 |
| 9 | "MVP"/"Phase 2"/"추후 검토" 표현 사용 | 완제품 단일 설계 정책 위반 |
| 10 | 외부 LLM API를 기본값으로 사용 | 폐쇄망 정책 위반 |
| 11 | 테스트 없이 핵심 모듈 구현 | 코드 리뷰 reject |
| 12 | claim_text 없이 citation 객체 반환 | ADR-010 위반, Citation Inspector 동작 불가 |

---

## 완료 기준

### 사용자 관점
- `/{tenant_id}/chat`에서 질문·답변 가능
- 4-type citation 시각적 분리 (direct·종합·추론·충돌)
- `support_level=medium`에 호버 ⚠
- unsupported segment 인라인 ⚠
- citation 클릭 → CitationPanel에 `claim_text`·support·원문 발췌 표시
- chat_streaming / chat_structured 모드 선택 가능
- fallback 응답 시 near_misses + suggested_actions 노출
- 답변 복사·피드백 가능

### Tenant Admin 관점
- 문서·item 등록 (input_schema 기반 동적 폼)
- 인덱싱 모니터링 (chunk 단위 retry, 4-mode reindex)
- chat_logs 조회 + retrieved_chunks + verifier_metrics + routing_decision 모두 확인 가능
- Citation Inspector에서 4-type 분포·claim ↔ chunk 매핑 시각화
- Routing Rules Editor + LoRA Registry + Schema Editor + Prompt Studio + Evaluation Console + Tenant Configs 사용 가능
- Assessment Console (모듈 활성 시): Item Bank + Generation Workbench + Quality Review Queue

### platform_admin 관점
- Tenant Management (등록·status 전이·hard delete with cross-system 일관성)
- Cross-tenant 통계 (BYPASSRLS, 검색 결과 노출 아님)
- LoRA 등록·승인·archive
- platform configs (`configs/platform/*`) 편집

### 기술 관점
- `docker-compose up`으로 모든 서비스 기동
- 신규 tenant 등록 = collection 자동 생성·configs 시드·MinIO prefix 초기화 (IdP 등록은 SSO 운영자 수동)
- vLLM multi-LoRA per-request adapter 동작
- Gate 1/Gate 2 발동 시 fallback 응답 (chat_logs에 reason 기록)
- chat_logs에 `tenant_id`, `confidence`, `citation_types`, `routing_decision`, `inference_judge_results`, `conflict_groups`, `model_failure_chain`, `ui_mode` 모두 자동 기록
- backup/restore per tenant 스크립트 동작
- chunks archival 90일 자동 처리
- old collection 30일 hold 후 platform_admin 명시 삭제

---

## 자주 하는 실수

### 실수 1: tenant_id를 path에서만 검증하고 RLS context 생략

```python
# 나쁜 예
@app.get("/api/{tenant_id}/chunks")
def list_chunks(tenant_id: str):
    return db.query(Chunks).all()   # RLS 적용 안 됨

# 좋은 예
@app.get("/api/{tenant_id}/chunks")
def list_chunks(tenant_id: str, ctx: UserContext = Depends(...)):
    db.execute(f"SET LOCAL app.current_tenant = '{ctx.tenant_id}'")
    return db.query(Chunks).all()   # RLS 자동 적용
```

### 실수 2: citation을 단순 marker만으로 검증

```python
# 나쁜 예 — ADR-005 v1의 한계
verified_citations = [c for c in citations if c.marker_in_range]

# 좋은 예 — ADR-010 v2
for c in citations:
    sim = cosine(embed(c.claim_text), embed(c.chunk.content))
    c.support_level = classify_support_level(sim, configs)
    c.verified = c.support_level in {"strong", "medium"}
    if c.support_type == "inference":
        judge = await inference_judge(c.claim_text, c.cited_chunks)
        if not judge.valid or judge.confidence < 0.6:
            citations.remove(c)
```

### 실수 3: configs가 코드에 하드코딩

```python
# 나쁜 예
embedder = SentenceTransformer("bge-m3")
top_k = 10

# 좋은 예
config = TenantConfigService.load(tenant_id)
embedder = EmbeddingService.load(config.embedding.model)
top_k = config.retrieval.top_k.rerank
```

### 실수 4: assessment를 chunk처럼 처리

```python
# 나쁜 예
qdrant.upsert(collection=f"chunks_{tenant_id}", points=[item_as_chunk])

# 좋은 예 — ADR-014
db.insert("assessment_items", item_data)
qdrant.upsert(collection=f"items_{tenant_id}", points=[item_vector])
```

### 실수 5: phasing 표현 사용

```python
# 나쁜 예 (ADR/코드 모두)
# TODO: Phase 2에서 inference 추가
# MVP 한정으로 streaming 미지원

# 좋은 예
# inference는 ADR-010 §4 LLM-as-judge로 처리
# chat_streaming은 ADR-013 §6에 day 1 구현됨
# 본 시스템은 X를 지원하지 않는다 (별도 ADR 필요 시)
```

### 실수 6: LangGraph 노드에 모든 로직

```python
# 나쁜 예
def retrieve_node(state):
    chunks = qdrant.search(...)
    reranked = reranker.rank(...)
    filtered = [c for c in reranked if check_acl(c, state['user'])]
    return state

# 좋은 예
class RetrievalService:
    def retrieve(self, query, user_context, tenant_config) -> list[Chunk]: ...

def retrieve_context_node(state):
    state["retrieved_chunks"] = service.retrieve(
        state["rewritten_query"], state["user_context"], state["tenant_config"]
    )
    return state
```

---

## 일관성 규칙 (Y1~Y10 — 흩어진 ADR을 단일화)

| # | 규칙 | 출처 |
|---|---|---|
| Y1 | 비활성 상태 명명: documents = `archived`, assessment_items = `retired`. 같은 의미. UI는 둘 다 "비활성"으로 표시 | ADR-012/014 |
| Y2 | assessment 품질: 어느 하나 `valid=false` 또는 score<0.5 → `draft`. 모두 `valid=true`이고 모든 score≥0.5 → `reviewed` (운영자 명시 approve로만 `approved` 전이) | ADR-014 §5 |
| Y3 | configs merge: dict deep merge, list는 override가 base 교체, primitive는 override 우선. null은 explicit (missing key와 다름) | ADR-009 §4 (TenantConfigService 구현) |
| Y4 | timezone: `Asia/Seoul` 기본. 모든 date 비교는 KST 기준. timestamp는 PostgreSQL `TIMESTAMPTZ` 사용 | configs/platform/common_fields.yaml |
| Y5 | tenant restore vs new: `tenant_register.sh`는 신규만, `tenant_restore.sh`는 archived/미존재 상태에서만 (script가 status 검증) | infra/scripts |
| Y6 | indexing 큐: 기본 FIFO + tenant별 동시 실행 1개 제한. priority는 platform_admin override 가능 | ADR-019 후속 |
| Y7 | user 다중 tenant: frontend tenant selector → 다른 client_id로 OIDC re-login. 동시 token 보유 금지 | ADR-018 §8 |
| Y8 | Schema editor 동시 편집: optimistic locking — `tenant_input_schemas.schema_version`이 lock token. 충돌 시 409 + 운영자 merge | ADR-015 보강 |
| Y9 | 메뉴 권한: `USER` (chat), `ADMIN` (tenant admin 전체), `PLATFORM_ADMIN` (cross-tenant). 메뉴별 매핑은 frontend RBAC 미들웨어 책임 | ADR-016 §3 |
| Y10 | 수치 정밀도: similarity·confidence·score 모두 PostgreSQL `DOUBLE PRECISION` (FLOAT의 한국어 별명, 8byte). API 응답은 소수점 4자리 round | ADR-019 §2 보강 |

---

## 작업 시작 시 체크리스트

새 기능 추가 시:

1. 관련 ADR을 먼저 읽는다 (ADR Index에서 매핑)
2. 관련 ADR이 supersede 되었으면 *최신*을 따른다
3. 결정에 변경이 필요하면 새 ADR 작성 (`docs/adr/NNN-...`) — 기존 ADR 본문은 수정하지 않고 Status를 `Superseded by ADR-NNN`로 변경
4. 코드는 ADR을 참조 (`# see ADR-010 §4`)
5. configs를 platform 또는 tenant 계층에 두고 코드는 TenantConfigService로 읽음
6. tenant_id를 모든 호출에 명시
7. 테스트 — 단위 + RLS 통합 + 4-type citation 분기

---

## 외부 시스템 연동 가정

- **AuthFusion Platform** (OIDC SSO Provider, port 8081): Authorization Code + PKCE, JWKS RS256. **OAuth2 client ≡ tenant** 매핑 (ADR-018). DomainRAG는 `client_id` claim을 tenant_id로 활용. clearance/department/domain_groups는 DomainRAG `user_tenant_membership` 테이블에서 보강.
- **AuthFusion KeyHub** (port 8085): LoRA adapter weights·service account secrets·외부 API key 보관 (ADR-019).
- **AuthFusion Ledger** (port 8089): DomainRAG 보안 이벤트(auth_failure / tenant_mismatch / hard_delete / platform_admin_action) hash chain audit (ADR-020).
- **WiSentinel** (port 8080): 직접 호출 의존 0. `dlp-core` 룰을 라이브러리로 포팅(ADR-020). audit:capture Redis publish는 선택적.
- **Ollama** (174번 GPU 0, Qwen Vision): authfusion·WiSentinel과 공유. DomainRAG는 vision OCR·일부 fallback에 선택적 활용 (ADR-019).
- **vLLM**: 174번 GPU 1·2 = Shared LLM (Qwen 14B+), 115번 GPU 0·1 = Tenant SLM (Qwen 7B + multi-LoRA). 174번 GPU 3 = Embedding (bge-m3) + Reranker (ADR-019).
- **Qdrant 1.10+**: collection-per-tenant + named vectors (dense+sparse) + DBSF fusion.
- **PostgreSQL 16** (115번 기존 인스턴스): 별도 `domainrag` database, Row-Level Security + Alembic migration + LISTEN/NOTIFY. RLS 성능 가이드라인은 ADR-019 §2.
- **MinIO**: prefix-per-tenant.

---

## 참고

- 모든 설계 결정: `docs/adr/` (ADR Index 위 표 참고)
- 도메인 정책 configs: `configs/platform/` + `configs/tenants/<id>/`
- 변경 이력: ADR `Superseded` 체인 + git history
- 신규 결정 필요 시: 새 ADR 작성 후 본 CLAUDE.md ADR Index 갱신
