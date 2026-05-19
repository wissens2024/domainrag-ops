# ADR-011: Hybrid Retrieval v2 — Multi-tenant + Complete Design

## Status
**Accepted** (2026-05-08)

> **Supersedes ADR-006**. ADR-006의 모든 결정(bge-m3 dense+sparse, Qdrant named vectors, DBSF fusion, top_k 단계)을 흡수하고 multi-tenant 운영(ADR-008) + deferred 항목 완전 설계(query rewriting 알고리즘, reranker 위치, 임베딩 이종화 운영, 가용성 실패 핸들링)를 추가한다. 본 ADR이 retrieval 단일 진실 소스.

---

## Context

### 배경 (사실)

- ADR-006이 단일 tenant 가정으로 작성되었고, ADR-008이 collection-per-tenant로 부분 supersede.
- ADR-006에 phasing 표현(`Phase 2: query rewriting 검토`, `weighted_sum 전환 여부`, `dynamic α/β`)이 다수 잔존 — 본 프로젝트 정책상(완제품 단일 설계, MVP/Phase 2 분리 금지) 모두 본 ADR에서 결정해야 함.
- 비전 §4 retrieval layer가 dense + sparse + ACL filter + reranker + context compressor를 명시.
- 도메인 특성: 보안 매뉴얼, 법무 계약·해석, 시험 문제, 설비 정비 — 도메인별 검색 패턴이 다르므로 per-tenant 튜닝 필요.

### 가정

- Qdrant 1.10+ 운영 — Query API + DBSF fusion + collection alias 지원
- bge-m3가 모든 tenant에 일반화 가능. 도메인 특화 임베딩이 필요한 tenant는 별도 모델로 collection 생성
- 한국어 reranker로 `bge-reranker-v2-m3`가 일반 도메인 충분
- HyDE/LLM expand 호출에 사용할 LLM endpoint(tenant_slm 또는 shared_llm)는 ADR-013에서 결정

가정 중 하나라도 깨지면 재검토. 특히 Qdrant 1.10+ 미만이면 DBSF 회피 application-level fusion 필요.

---

## Decision

### 1. Vector 구조 (ADR-006 + ADR-008 통합)

- 각 tenant마다 collection: `chunks_<tenant_id>`
- 같은 point에 두 named vector:
  - `dense`: bge-m3 dense (또는 tenant 지정 모델, 1024차원 등)
  - `sparse`: bge-m3 sparse
- payload에 chunk metadata + `tenant_id` 중복 보관 (RLS 일관성·교차 검증용)

### 2. Fusion — Qdrant DBSF

```python
client.query_points(
    collection_name=f"chunks_{tenant_id}",
    prefetch=[
        {"using": "dense",  "query": dense_q,  "limit": top_k.dense},
        {"using": "sparse", "query": sparse_q, "limit": top_k.sparse},
    ],
    query=FusionQuery(fusion=Fusion.DBSF),
    query_filter=acl_filter,
    limit=top_k.fused,
)
```

- 가중치 시작값 `α(dense) = β(sparse) = 0.5` (per-tenant `configs/tenants/<id>/retrieval.yaml`로 튜닝)
- Fusion 알고리즘 선택은 configs로 유연화: `dbsf | rrf | weighted_sum`. 기본 `dbsf`. weighted_sum 사용 시 application-level 정규화(min-max) + 가중합.

### 3. top_k 단계 (per-tenant configs)

| 단계 | 기본값 | 비고 |
|---|---|---|
| dense_top_k | 30 | DBSF prefetch |
| sparse_top_k | 30 | DBSF prefetch |
| fused_top_k | 50 | DBSF 결과 |
| rerank_top_k | 10 | cross-encoder 재정렬 후 |
| context_top_k | 5 | LLM에 전달 |

각 단계 값은 tenant별 평가셋(ADR-009 promotion_gate)으로 튜닝.

### 4. Reranker — Shared default + Per-tenant override

day 1 구현 모두 포함:
- **Platform default**: `bge-reranker-v2-m3`, `configs/platform/retrieval.yaml`
- **Per-tenant override**: `configs/tenants/<id>/retrieval.yaml`의 `reranker.model_override` (null이면 default 사용)
- **RerankerService**: tenant_id 기반 모델 lookup + LRU cache (TTL 1시간)
- **모델 weights 배치 절차**: `models/reranker/<model_name>/weights/...`. tenant 등록 시 override 모델 spec이 있으면 weights 사전 배치 검증.

활성화 결정은 평가셋으로 비교 후 운영자 명시 — *운영 절차*이지 코드 변경 아님.

### 5. Query Rewriting — 완전 구현

day 1에 모든 알고리즘과 메커니즘 구현, tenant별 configs로 활성화·비활성화·전략 선택.

#### Configs 스키마

```yaml
# configs/tenants/<id>/retrieval.yaml
query_rewriting:
  enable: false                # 기본 false (도메인별 평가 후 활성화)
  strategy: hyde               # hyde | llm_expand | none
  llm:
    endpoint: tenant_slm       # tenant_slm | shared_llm
    max_tokens: 256
    temperature: 0.3
  hyde:
    instruction: "이 질문에 답할 때 적절한 가상 문서를 한 문단으로 작성하시오."
  llm_expand:
    instruction: "이 질문의 핵심 키워드와 동의어·관련어를 추출하시오."
    max_terms: 8
```

#### LangGraph 노드

`query_rewrite` 노드 추가, 조건부 실행 (`config.query_rewriting.enable=true`일 때만):

```text
tenant_resolver → tenant_health_check → 
  ├─ enable=true → query_rewrite (HyDE 또는 llm_expand) → state.rewritten_query
  └─ enable=false → state.rewritten_query = state.question
→ retrieve_context (state.rewritten_query 사용)
```

`chat_logs.rewritten_query` 컬럼이 자동 채워짐 → 평가 분석 가능.

### 6. Cross-tenant Retrieval — 명시적 미지원

> 본 시스템은 cross-tenant retrieval을 지원하지 않는다. 사용자·admin·서비스 호출 모두 정확히 한 tenant scope. 향후 요구가 생기면 ACL 의미를 재정의하는 별도 ADR로 진행.

`tenant_resolver`가 token claim과 URL path를 검증해 단일 tenant 컨텍스트를 강제. Cross-tenant 통계는 admin 대시보드의 BYPASSRLS PostgreSQL 쿼리로만 노출 (검색 결과 노출 아님).

### 7. 임베딩 이종화 운영 — 완전 설계

#### Source of Truth
- `tenants` 테이블의 `embedding_model`, `embedding_dim` 컬럼이 단일 진실 소스
- 같은 tenant의 chunk와 query는 *동일* 모델로 임베딩되어야 함 — mismatched 호출 차단

#### EmbeddingService

```python
class EmbeddingService:
    _model_cache: LRU[str, EmbeddingModel]   # model_name → 로드된 모델
    _tenant_model_map: Dict[str, str]         # tenant_id → model_name (캐시, LISTEN/NOTIFY 무효화)
    
    def embed(self, tenant_id: str, text: str) -> Tuple[Dense, Sparse]:
        model_name = self._tenant_model_map[tenant_id]
        model = self._model_cache.get_or_load(model_name)
        return model.encode_dense_sparse(text)
```

#### 모델 weights 배치 절차

```text
models/
└── embedding/
    ├── bge-m3/                      # platform default
    │   ├── weights/
    │   ├── tokenizer/
    │   └── manifest.yaml            # model_name, dimension, sparse_support
    └── korean-legal-bert/           # 특정 tenant용
        └── ...
```

신규 모델 도입 = 디스크 배치 + manifest 검증 + `tenants.embedding_model` 갱신 + 기존 collection은 ADR-012 alias swap 절차로 재인덱싱.

### 8. Collection 가용성 실패 — 완전 설계

`tenant_resolver` 직후 `tenant_health_check` 노드:

```text
tenant_health_check:
  - tenants.status 확인
  - chunks_<tenant_id> collection 존재·점수 확인 (Qdrant info)
  - 다음 분기:
    ├─ status=archived → 403 Forbidden (admin이 archived tenant 진입 시도)
    ├─ status=migrating → fallback(reason="tenant_migrating", retry_after=60s)
    ├─ chunk count = 0 → fallback(reason="empty_tenant", "문서가 등록되지 않은 테넌트입니다")
    ├─ Qdrant 503/timeout → 재시도 1회 후 fallback(reason="retrieval_unavailable")
    └─ 정상 → 다음 노드
```

`chat_logs.fallback_reason` enum 확장 (ADR-010 통합):
- `empty_tenant`
- `tenant_migrating`
- `retrieval_unavailable`

### 9. retrieve_context 노드 사양 (LangGraph)

```text
retrieve_context (per-tenant):
  1. config := TenantConfigService.load(state.tenant_id)
  2. embedding := EmbeddingService.embed(state.tenant_id, state.rewritten_query)
  3. acl_filter := build_qdrant_filter(state.user_context)  # ADR-004 + tenant_id 자동 포함 (RLS 정신)
  4. result := QdrantClient.query_points(
       collection=f"chunks_{state.tenant_id}",
       prefetch=[dense, sparse],
       query=Fusion(config.hybrid.fusion),  # dbsf | rrf | weighted_sum
       query_filter=acl_filter,
       limit=config.top_k.fused,
     )
  5. state.retrieved_chunks := transform(result, includes=[dense_score, sparse_score, fused_score])
```

### 10. Reranker 노드 사양 (LangGraph)

```text
rerank_context:
  1. reranker := RerankerService.load(state.tenant_id)  # default 또는 override
  2. scored := reranker.score(state.question, state.retrieved_chunks)
  3. state.reranked_chunks := top_k(scored, config.top_k.rerank)
  4. ADR-010 Gate 1 입력: rerank_score 분포
```

### 11. configs/retrieval.yaml v2 (전체 schema)

```yaml
# configs/platform/retrieval.yaml (모든 tenant 기본값)
hybrid:
  fusion: dbsf                 # dbsf | rrf | weighted_sum
  weights:
    dense: 0.5
    sparse: 0.5

top_k:
  dense: 30
  sparse: 30
  fused: 50
  rerank: 10
  context: 5

reranker:
  model: bge-reranker-v2-m3
  device: gpu
  batch_size: 32

query_rewriting:
  enable: false
  strategy: none               # none | hyde | llm_expand
  llm:
    endpoint: tenant_slm
    max_tokens: 256
  hyde:
    instruction: "이 질문에 답할 때 적절한 가상 문서를 한 문단으로 작성하시오."
  llm_expand:
    instruction: "이 질문의 핵심 키워드와 동의어·관련어를 추출하시오."
    max_terms: 8

embedding:                     # 참고용 — actual은 tenants.embedding_model
  default_model: bge-m3
  default_dim: 1024

# tenant override 예시: configs/tenants/legal/overrides.yaml
# retrieval:
#   top_k: { rerank: 15 }
#   reranker: { model_override: "korean-legal-reranker" }
#   query_rewriting: { enable: true, strategy: hyde }
```

### 12. Reranker per-domain bypass 옵션

특정 도메인(시험 문제 등 ADR-014 대상)은 reranker 의미 약함 — `reranker.bypass: true` configs로 reranking 단계 자체를 건너뜀:
```yaml
# configs/tenants/exam_bank/overrides.yaml
retrieval:
  reranker:
    bypass: true
  top_k:
    fused: 20    # bypass 시 fused 결과를 바로 context로
    context: 10
```

bypass=true이면 LangGraph가 `rerank_context` 노드 skip + Gate 1은 fused_score 기준으로 평가.

#### 한국어 RAG 운영 우선순위 (memory `feedback_korean_rag_priority` 정합)

한국어 도메인의 검색 품질은 다음 순으로 결정된다. **reranker는 8순위**이며 *품질 보정 장치*이지 *입력 보완 마법*이 아니다:

1. 입력 스키마 (ADR-015)
2. 메타데이터 (ADR-001) — 도메인 filter의 핵심
3. 문서 버전/유효기간 (ADR-012 §3)
4. 정확한 chunking (ADR-007/012)
5. dense+sparse hybrid (본 ADR §1·§3)
6. ACL/tenant filter (ADR-004/008 + 본 ADR §2)
7. citation verifier (ADR-010)
8. **reranker (본 절 bypass)**

운영 결정: **현재 가용한 한국어 cross-encoder 다수(`bge-reranker-v2-m3`/`base`/`large`)가 모두 XLM-RoBERTa 기반**이라 TEI Candle FlashBert backend와 비호환이다. 따라서 *플랫폼 기본값을 `reranker.bypass: true`로 운영*하고 dense+sparse fusion + 위 1~7번을 견고히 한 뒤, 다음 중 하나가 가용해질 때 별도 작업으로 reranker 결선한다:

- vLLM 0.7+ cross-encoder 지원 (현재 0.6.3은 generation only)
- [Infinity](https://github.com/michaelfeil/infinity) 같은 XLM-RoBERTa 지원 inference engine (별도 ADR로 외부 의존성 정식화)
- BERT 기반 한국어 cross-encoder 신규 모델 등장

`configs/platform/retrieval.yaml`의 platform default는 `bypass: false`로 두되, 실제 운영 환경의 인프라가 reranker 모델을 지원하지 않으면 platform_admin이 `bypass: true`로 전환. tenant override로 도메인별 토글 가능.

---

## Consequences

### 긍정적 영향

- ADR-006의 모든 결정 + ADR-008의 multi-tenant + 이전 deferred 항목들이 한 ADR에 정리되어 단일 진실 소스
- Per-tenant 자유도 (reranker, query rewriting, fusion 알고리즘, top_k 단계, embedding model, reranker bypass)가 day 1에 모두 구현
- 코드 변경 없이 운영자가 configs로 도메인 튜닝 가능
- collection 가용성 시나리오 모두 explicit 처리 — silent failure 차단
- ADR-007 lifecycle alias swap과 자연 정합

### 부정적 영향 / 부채

- 구현 표면적 ↑ — query_rewriting 두 알고리즘, reranker LRU cache, embedding 이종화, fusion 3종 모두 day 1 구현
- 평가셋 없는 tenant는 default 값으로 시작 — 도메인 특화 튜닝까지 시간 소요 (운영 부담 ↑)
- LangGraph 노드 수 증가 (tenant_resolver, tenant_health_check, query_rewrite 조건부) — 그래프 복잡도 ↑
- 모델 weights 디스크 배치 절차가 운영 워크플로우의 일부가 됨

### 후속 작업

- `configs/platform/retrieval.yaml` 기본값 시드
- TenantConfigService에 retrieval 카테고리 schema 검증 (ADR-009 pydantic)
- LangGraph 노드 구현: `tenant_resolver`, `tenant_health_check`, `query_rewrite`, `retrieve_context`, `rerank_context`
- EmbeddingService, RerankerService 구현 (LRU cache + tenant_id lookup)
- Qdrant collection 생성 시 dense+sparse named vectors + payload schema
- chat_logs `rewritten_query` 컬럼이 query rewriting 결과 자동 보관하도록 LangGraph 후크
- ADR-006에 본 ADR로 supersede 노트 추가
- ADR-014(Assessment)가 reranker bypass 활용 — 본 ADR 정합 확인

---

## Alternatives Considered

### 1. ADR-006/008 amendment로 처리, 별도 ADR 미작성
- **장점**: ADR 수 절감
- **기각 사유**: deferred 항목 다수 + multi-tenant 통합 필요로 amendment가 아닌 별도 ADR이 가독성·추적성 ↑

### 2. Reranker per-tenant 강제 (모든 tenant 별도)
- **장점**: 도메인 fit 극대화
- **기각 사유**: 모든 tenant가 도메인 reranker 가치 입증 안 됨. shared default + override가 균형.

### 3. Reranker 제거 (DBSF fused 결과 바로 사용)
- **장점**: 메모리·latency 절감
- **기각 사유**: cross-encoder rerank 가치 도메인별 입증됨. tenant별 bypass 옵션이 절충안.

### 4. Application-level fusion (DBSF 미사용)
- **장점**: 정규화 알고리즘 자유, debugging trace 쉬움
- **기각 사유**: Qdrant DBSF가 분포 정규화 자동 + 한 호출로 처리. configs로 weighted_sum 전환 가능하므로 자유도 동시 확보.

### 5. Query rewriting을 configs 없이 항상 활성
- **장점**: 단순
- **기각 사유**: 도메인별 효과 차이 큼. 보안 매뉴얼은 짧은 직접 질문 위주라 rewriting 효과 미미. opt-in이 비용·정확도 균형.

### 6. Cross-tenant retrieval 허용
- **장점**: admin 다중 도메인 검색 편의
- **기각 사유**: ACL 의미 흐려짐. PostgreSQL RLS와 Qdrant collection 격리의 보안 가치 훼손. 별도 ADR로 검토 가능하나 본 ADR은 명시적 미지원.

### 7. 임베딩 모델 단일화 (모든 tenant bge-m3 강제)
- **장점**: 운영 단순
- **기각 사유**: 비전 §3·§4가 도메인 특화 강조. 법무·보안·시험 문제가 동일 모델로 최적이라는 가정 불성립. 이종화 메커니즘이 본 ADR의 핵심.

---

## Related

- [ADR-002: Protocol/Adapter](./002-protocol-adapter-pattern.md) — Retriever, Reranker, Embedder 모두 Protocol
- [ADR-004: 보안 & ACL](./004-security-acl-model.md) — ACL filter는 prefetch + fusion 단계에 적용
- [ADR-006: Hybrid Retrieval v1](./006-hybrid-retrieval.md) — **superseded by 본 ADR**
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — 격리 경계 정의
- [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md) — retrieval.yaml configs 위치
- [ADR-010: Citation Trust v2](./010-citation-trust-v2.md) — Gate 1이 rerank_score 기준
- ADR-012 (예정): Lifecycle v2 — collection 생성·alias swap·archival
- ADR-013 (예정): Model Routing — query rewriting LLM 결정
- ADR-014 (예정): Assessment Workflow — reranker bypass 활용
- SPEC.md §4, §6.2, §10.2 — 폐기 (본 ADR이 흡수)

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted (supersedes ADR-006)
