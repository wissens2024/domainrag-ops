# ADR-014: Assessment Workflow — 시험 문제 도메인 (Item·Generation·Validation)

## Status
**Accepted** (2026-05-08)

> 시험 문제 도메인은 RAG와 본질적으로 다른 *structured query + similarity check + generation* 워크플로우다. 본 ADR은 별도 데이터 모델·별도 LangGraph subflow·별도 검증 파이프라인을 정의하며, ADR-013 routing이 진입점을 제공한다.

---

## Context

### 배경 (사실)

- 비전 §9·§10이 시험 문제 도메인을 정의 — 1000문제 RAG에 넣고 조건 매칭 추출 또는 신규 생성.
- 비전 §10은 "Assessment Bank Layer / Generator / Similarity Checker / Answer Validator / Citation"의 5개 모듈을 명시.
- ADR-013의 `assessment_generation` routing 룰이 `require_similarity_check: true`로 본 ADR 호출.
- 비전 §3의 입력 예시 §5.3에 `assessment_item` 구조 명시 (question_id, subject, chapter, difficulty, question_type, question_text, choices, answer, explanation, tags, used_count, last_used_at, quality_status).
- 시험 문제는 chunking 대상이 *아님* — 한 문제 = 한 structured item. RAG의 chunk 가정이 맞지 않음.
- 비전 §9.3 출제 모드: 기존 추출 / 신규 생성 / 혼합.

### 가정

- 한 tenant가 RAG와 Assessment를 동시 활성화 가능 (단일 tenant_id, 두 모듈)
- bge-m3가 question_text 유사도 검사에 충분 (한국어·다국어)
- LLM-as-judge가 정답·해설·보기 검증에 활용 가능 (ADR-013 shared_llm 재사용)
- assessment 도메인의 문제 수: tenant당 수만~수십만 (영구 누적)

가정이 깨지면 재검토 — 특히 100만 이상 items 누적 시 storage·검색 전략 별도 검토.

---

## Decision

### 1. Storage — 별도 데이터 모델

#### PostgreSQL `assessment_items` 테이블

```sql
CREATE TABLE assessment_items (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    item_id VARCHAR(255) NOT NULL,             -- "Q-2026-000123"
    
    -- 핵심 콘텐츠
    subject VARCHAR(100),                       -- "정보보안"
    chapter VARCHAR(255),                       -- "접근통제"
    difficulty VARCHAR(20),                     -- "easy" | "medium" | "hard"
    question_type VARCHAR(50),                  -- "multiple_choice" | "true_false" | "short_answer" | "essay"
    question_text TEXT NOT NULL,
    choices JSONB DEFAULT '[]',                 -- multiple_choice일 때
    answer TEXT NOT NULL,
    explanation TEXT,
    
    -- 메타
    tags JSONB DEFAULT '[]',
    source_doc_id VARCHAR(255),                 -- 원천 문서 (있으면)
    learning_objective TEXT,
    
    -- 품질
    quality_status VARCHAR(50) DEFAULT 'draft', -- draft | reviewed | approved | retired
    quality_score FLOAT,                        -- LLM-as-judge 점수
    validator_results JSONB DEFAULT '{}',       -- { answer_valid, explanation_valid, choices_valid, difficulty_match }
    
    -- 사용 이력
    used_count INT DEFAULT 0,
    last_used_at TIMESTAMP,
    
    -- 생성 메타
    generation_mode VARCHAR(20),                -- "imported" | "generated" | "hybrid"
    reference_item_ids JSONB DEFAULT '[]',      -- generated 모드에서 참고한 items
    
    -- 임베딩 메타 (실제 vector는 Qdrant)
    embedding_model VARCHAR(100),
    embedding_version VARCHAR(50),
    vector_id VARCHAR(255),                     -- items_<tenant> collection의 point ID
    
    created_by VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE (tenant_id, item_id)
);

ALTER TABLE assessment_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON assessment_items
  USING (tenant_id = current_setting('app.current_tenant')::text);

CREATE INDEX idx_items_tenant_subject ON assessment_items (tenant_id, subject, chapter);
CREATE INDEX idx_items_difficulty ON assessment_items (tenant_id, difficulty);
CREATE INDEX idx_items_quality ON assessment_items (tenant_id, quality_status);
```

#### Qdrant `items_<tenant_id>` collection

- dense vector만 (sparse 불필요 — similarity check가 의미 유사도 중심)
- payload: tenant_id, item_id, subject, chapter, difficulty (검색 필터용 최소 메타)
- 전체 필드는 PostgreSQL이 책임, Qdrant는 vector + 식별자만

ADR-008의 collection-per-tenant 패턴이 RAG와 Assessment 양쪽에 동일 적용 — `chunks_<tenant>` (RAG) + `items_<tenant>` (Assessment).

#### 분리의 의미

| 작업 | 데이터 소스 |
|---|---|
| Item 등록·메타 조회 | PostgreSQL (RLS) |
| 조건 매칭 추출 | PostgreSQL SQL (subject/chapter/difficulty/quality_status WHERE) |
| Similarity check | Qdrant `items_<tenant>` |
| Item 검색 (admin) | PostgreSQL full-text 또는 ILIKE |

### 2. Similarity Check

```yaml
# configs/platform/assessment.yaml
similarity:
  embedding_model: bge-m3                # tenants.embedding_model 재사용
  threshold_duplicate: 0.85              # 이상이면 중복으로 reject
  threshold_similar: 0.65                # 이상이면 유사 candidate (참고 표시)
  scope:
    same_tenant: true                    # 항상 true (cross-tenant 검색 금지)
    same_subject: true                   # subject 동일한 items만 비교
    same_chapter: false                  # chapter는 자유

# tenant overrides 가능
```

#### Workflow
```python
def check_similarity(tenant_id: str, new_question_text: str, subject: str) -> SimilarityResult:
    embedding = EmbeddingService.embed(tenant_id, new_question_text)[0]  # dense
    filter = {"tenant_id": tenant_id, "subject": subject}
    results = qdrant.search(
        collection=f"items_{tenant_id}",
        query_vector=embedding,
        filter=filter,
        limit=10,
    )
    return {
        "max_similarity": results[0].score if results else 0,
        "is_duplicate": results[0].score >= 0.85 if results else False,
        "similar_candidates": [r for r in results if r.score >= 0.65],
    }
```

### 3. 출제 모드 — 3가지 LangGraph Subflow

#### Mode 1: Extract (기존 문제은행에서 추출)

```text
Request: { mode: "extract", criteria: {subject, chapter, difficulty_distribution: {easy: 3, medium: 5, hard: 2}, exclude_recent_days: 90} }
  ↓
extract_subflow:
  1. SQL 조건 매칭 — quality_status='approved' AND last_used_at < NOW() - INTERVAL '90 days'
  2. 난이도 분포 만족하도록 random sampling
  3. used_count 증가 + last_used_at 갱신
  4. Citation: item_id 그대로
```

#### Mode 2: Generate (신규 생성)

```text
Request: { mode: "generate", criteria: {subject, chapter, difficulty, count: 5}, reference_strategy: "topk_similar" }
  ↓
generate_subflow:
  1. Reference 수집 — subject·chapter 동일 + quality_status='approved'에서 top-k similar (질문 키워드 기반)
  2. LLM 호출 (shared_llm, ADR-013) — references를 prompt에 주고 신규 item count개 생성
     output schema: { items: [{question_text, choices, answer, explanation, difficulty, ...}] }
  3. 각 신규 item에 대해 similarity_check
     - duplicate(>=0.85)면 reject + retry up to 3회
     - similar candidate(0.65~0.85)는 reference로 표기
  4. Quality validation pipeline (§5)
  5. assessment_items에 INSERT (quality_status='draft', generation_mode='generated', reference_item_ids 채움)
  6. Citation: 신규 item_id + reference_item_ids
```

#### Mode 3: Hybrid (혼합 출제)

```text
Request: { mode: "hybrid", extract: {count: 7}, generate: {count: 3}, criteria: {...} }
  ↓
hybrid_subflow:
  1. extract_subflow로 7문제 추출
  2. generate_subflow로 3문제 신규 생성
  3. 합쳐서 반환
```

### 4. Citation — Item-level

ADR-010 4-type citation 스키마에 `direct` type을 그대로 활용하되 chunk_id 대신 `item_id` 사용:

```json
{
  "citation_id": "cite-...",
  "marker": "[1]",
  "tenant_id": "exam_bank",
  "support_type": "direct",
  "item_id": "Q-2026-000123",
  "subject": "정보보안",
  "chapter": "접근통제",
  "question_text_excerpt": "다음 중 RBAC에 대한 설명으로 옳은 것은?",
  "verified": true
}
```

generate 모드에서는 reference_item_ids도 함께 노출 ("이 문제는 Q-000123, Q-000344, Q-000812를 참고하여 생성됨").

### 5. Quality Validation Pipeline (LLM-as-judge)

신규 생성 item에 4개 validator 순차 실행:

```yaml
# configs/platform/prompts/assessment_validators/
├── answer_validator.yaml      # 정답이 logical/factual하게 옳은가
├── explanation_validator.yaml # 해설이 정답을 정합 설명하는가
├── choices_validator.yaml     # 오답 보기가 plausible한가
└── difficulty_validator.yaml  # 난이도 라벨이 실제와 일치하는가
```

각 validator는 shared_llm 호출(ADR-013), JSON 응답 강제:
```json
{
  "valid": true|false,
  "score": 0~1,
  "reasoning": "...",
  "suggestions": ["..."]
}
```

#### Aggregate
- 모든 validator의 score 가중 평균 → `quality_score`
- 모든 valid=true이고 score >= 0.7 → `quality_status='reviewed'`
- 어느 하나 valid=false 또는 score < 0.5 → `quality_status='draft'` + 운영자 review 필요
- validator_results JSONB에 4개 결과 저장

#### 검증 비활성 옵션
운영 비용 ↑ 우려 시 tenant configs로 일부 validator 비활성:
```yaml
# configs/tenants/exam_bank/assessment.yaml
validators:
  answer: { enable: true }
  explanation: { enable: true }
  choices: { enable: true }
  difficulty: { enable: false }    # 비활성 — 운영자 수동 검토
```

### 6. ADR-013 Routing 정합 (진입점)

```yaml
# configs/tenants/exam_bank/routing.yaml
rules:
  - name: assessment_extract
    when:
      query_type: assessment_extract
    route:
      domain_subflow: assessment.extract     # 본 ADR §3 mode 1

  - name: assessment_generate
    when:
      query_type: assessment_generate
    route:
      model: shared_llm
      domain_subflow: assessment.generate    # 본 ADR §3 mode 2
      require_similarity_check: true
      require_quality_validation: true

  - name: assessment_hybrid
    when:
      query_type: assessment_hybrid
    route:
      domain_subflow: assessment.hybrid      # 본 ADR §3 mode 3
```

`domain_subflow`는 LangGraph가 본 ADR의 subflow를 호출하는 키. `query_type`은 ADR-013의 query classifier가 판정.

#### Query classifier 추가 패턴

```yaml
# query_classifier.yaml에 추가
- name: assessment_extract
  patterns:
    - "(?i)(추출|선별|뽑아|골라).*문제"
  assign:
    query_type: assessment_extract

- name: assessment_generate
  patterns:
    - "(?i)(만들|생성|작성).*문제"
  assign:
    query_type: assessment_generate

- name: assessment_hybrid
  patterns:
    - "(?i)(추출.*생성|생성.*추출).*문제"
  assign:
    query_type: assessment_hybrid
```

### 7. Module 활성화 — tenant.domain_modules

`tenants` 테이블에 `domain_modules JSONB DEFAULT '[]'` 컬럼 추가:
```sql
ALTER TABLE tenants ADD COLUMN domain_modules JSONB DEFAULT '["rag"]';
-- ["rag"] | ["assessment"] | ["rag", "assessment"]
```

- 활성화: `["rag"]` = RAG만, `["assessment"]` = Assessment만, `["rag", "assessment"]` = 둘 다
- 활성화 모듈에 따라 다음이 결정:
  - 생성되는 collection: `chunks_<tenant>` (rag) / `items_<tenant>` (assessment) / 둘 다
  - 활성화되는 admin console 메뉴
  - 활성화되는 API endpoint

#### API endpoint 분리
```
POST /api/{tenant_id}/chat                    # RAG (활성 시)
POST /api/{tenant_id}/assessment/extract      # Assessment extract
POST /api/{tenant_id}/assessment/generate     # Assessment generate
POST /api/{tenant_id}/assessment/hybrid       # Assessment hybrid
GET  /api/{tenant_id}/assessment/items        # 관리자 — item 목록
PATCH /api/{tenant_id}/assessment/items/{id}  # 관리자 — item 수정
```

### 8. Admin Console — Assessment Console (신규 메뉴)

비전 §13 admin console에 신규:
- **Item Bank Browser**: 검색·필터·페이징, item 상세 보기
- **Item Editor**: 등록·수정 (input_schema 기반 폼, ADR-015 정합)
- **Quality Review Queue**: quality_status='draft' items 검토·승인
- **Generation Workbench**: criteria 입력 → generate 시뮬레이션 → 결과 검토 → 승인/폐기
- **Usage Analytics**: subject/chapter/difficulty별 used_count 분포, 미사용 items 제안

Module 활성 tenant만 메뉴 표시.

### 9. Item Lifecycle (ADR-012 정합)

assessment_items에도 lifecycle 적용:
- **draft**: 생성 직후 또는 검증 미통과
- **reviewed**: validator 통과, 운영자 검토 대기
- **approved**: 출제 가능
- **retired**: 더 이상 출제 안 함 (soft delete)

ADR-012 archival 정책 동일 적용 — retired 90일 후 `assessment_items_archive`로 이동. used_count·last_used_at는 audit 보존.

### 10. assessment_logs (사용 이력 추적)

```sql
CREATE TABLE assessment_logs (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255),
    mode VARCHAR(20),                       -- extract | generate | hybrid
    criteria JSONB,                          -- 요청 조건
    item_ids JSONB DEFAULT '[]',             -- 반환된 item_ids
    generated_count INT DEFAULT 0,
    extracted_count INT DEFAULT 0,
    similarity_results JSONB,                -- 생성된 items의 similarity check 결과
    validator_summary JSONB,                 -- aggregate quality scores
    latency_ms INT,
    created_at TIMESTAMP DEFAULT NOW()
);

ALTER TABLE assessment_logs ENABLE ROW LEVEL SECURITY;
```

chat_logs와 분리 — assessment 사용 패턴이 chat과 본질적으로 다름.

---

## Consequences

### 긍정적 영향

- 시험 문제와 RAG가 데이터 모델·workflow·UI 모두 깨끗히 분리 — mismatched abstraction 차단
- 3 모드(extract/generate/hybrid) day 1 구현 — 도메인 요구 충실 반영
- LLM-as-judge 4 validator 자동화로 신규 item 품질 즉시 측정
- 같은 tenant_id에서 RAG + Assessment 다중 모듈 운영 — 비전 §3 정합
- assessment_logs로 출제 패턴·중복률·생성 비율 운영 분석 가능

### 부정적 영향 / 부채

- 데이터 모델·테이블·collection이 RAG와 별도로 늘어남 (운영 표면적 ↑)
- LLM 호출이 generate 모드에서 5-10회/요청 (생성 1 + validator 4 + similarity check) → latency·비용 ↑
- Quality validation의 LLM-as-judge 정확도가 사용자 신뢰에 직결 — validator prompt tuning 부담
- Generate 모드는 본질적으로 일정 hallucination 위험 — quality_status='draft' 시작 + 운영자 review 의무 명시 필요
- ADR-015 (Tenant Input Schema)가 assessment_item type을 별도 정의해야 함

### 후속 작업

- 마이그레이션: `assessment_items`, `assessment_logs`, `tenants.domain_modules` 컬럼
- LangGraph 노드: assessment subflow (extract/generate/hybrid) 3 분기
- Qdrant `items_<tenant>` collection 자동 생성 (tenant 등록 시 domain_modules에 따라)
- API endpoint 4종 (extract/generate/hybrid/items 관리)
- LLM-as-judge prompts 4개 (`configs/platform/prompts/assessment_validators/`)
- Admin console: Assessment Console 메뉴 (Item Bank, Editor, Review Queue, Generation Workbench, Analytics)
- ADR-015에서 assessment_item input schema 정의

---

## Alternatives Considered

### 1. (가) 같은 chunks 테이블/collection에 chunk_index=0
- **장점**: 인프라 재사용, 운영 단순
- **기각 사유**: retrieve_context가 RAG chunk 가정. assessment의 SQL-style 조건 매칭이 부자연. mismatched abstraction.

### 2. (다) 별도 테이블 + 같은 chunks_<tenant> collection
- **장점**: PostgreSQL은 분리, vector는 공유
- **기각 사유**: chunks와 items가 같은 collection에 섞이면 retrieve filter에 type 분기 추가 필요. similarity check도 type filter. 복잡도 증가, 분리의 가치 손실.

### 3. Assessment를 별도 마이크로서비스로
- **장점**: 진정한 분리, 독립 배포
- **기각 사유**: 인증·tenant context·LLM endpoint 등 공유 인프라 중복 운영. tenant 단위 격리는 본 시스템 내부에서 충분.

### 4. Validation 미적용 (운영자 전적 review)
- **장점**: LLM 호출 비용 절감
- **기각 사유**: 100문제 생성 시 운영자 100문제 수동 검토는 운영 부담 ↑. LLM-as-judge가 1차 필터.

### 5. Validator를 작은 SLM으로
- **장점**: 비용 ↓
- **기각 사유**: validator 정확도가 사용자 신뢰 직결. shared_llm이 안전. 호출 빈도 모니터링 후 최적화 ADR.

### 6. Generate 모드에 RAG retrieval 활용 (chunks 참고)
- **장점**: chunk 콘텐츠도 reference에 포함
- **기각 사유**: 시험 문제 생성에 chunk 콘텐츠 직접 활용은 도메인 표절·저작권 위험. items만 reference. RAG와 Assessment 분리 원칙.

### 7. 3 모드를 단일 generate 모드로 (extract는 SQL 직접 호출)
- **장점**: API 단순
- **기각 사유**: 사용자 요구가 "추출" / "생성" / "혼합"으로 명확히 분기. 명시적 모드가 의도 보존.

### 8. domain_modules 컬럼 없이 모든 tenant가 RAG+Assessment
- **장점**: 컬럼·로직 단순
- **기각 사유**: assessment 미활용 tenant에 빈 collection·메뉴 노출 → 혼란. domain_modules로 명시적 활성화 필요.

---

## Related

- [ADR-002: Protocol/Adapter](./002-protocol-adapter-pattern.md) — AssessmentService도 Protocol
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — collection-per-tenant 패턴 동일
- [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md) — assessment.yaml configs
- [ADR-010: Citation Trust v2](./010-citation-trust-v2.md) — direct citation을 item_id로 변형
- [ADR-011: Hybrid Retrieval v2](./011-hybrid-retrieval-v2.md) — reranker bypass option (assessment에 활용)
- [ADR-012: Lifecycle v2](./012-lifecycle-v2.md) — assessment_items lifecycle 정합 (draft/reviewed/approved/retired/archive)
- [ADR-013: Model Routing](./013-model-routing-and-slm-llm-strategy.md) — assessment_generation 라우팅 룰
- ADR-015 (예정): Tenant Input Schema — assessment_item input type 정의
- [SPEC.md §5, §13 (관리자 콘솔), 신규 §16](../../SPEC.md) — 본 ADR로 갱신 예정

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted
