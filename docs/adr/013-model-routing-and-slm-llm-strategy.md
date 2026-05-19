# ADR-013: Model Routing & SLM/LLM Strategy

## Status
**Accepted** (2026-05-08)

> ADR-009가 model.yaml 카테고리 schema를 선언만 했고, ADR-010이 inference judge LLM 사용을 명시했으며, ADR-011이 query rewriting LLM endpoint를 호출 대상으로 박았다. 본 ADR이 라우팅·LoRA·streaming·fallback chain·query classifier·endpoint 토폴로지를 단일 진실 소스로 정의한다.

---

## Context

### 배경 (사실)

- 비전 §11은 Router SLM, Tenant SLM, Tenant SLM + LoRA, Shared LLM fallback 4계층을 명시.
- 비전 §12 routing.yaml sketch는 매우 간략 — 실제 구현 가능 schema 부재.
- ADR-010이 inference judge LLM 호출을 명시했으나 어느 모델 endpoint 사용할지 미정.
- ADR-011이 query rewriting을 day 1 완전 구현하기로 했고 LLM endpoint 결정은 본 ADR 책임.
- ADR-005/010이 streaming을 deferred 처리했으나 본 프로젝트 정책상 본 ADR에서 결정 필요.
- vLLM 0.6+이 multi-LoRA per-request adapter switching 지원 (검증된 사실).
- 도메인 특성: 보안·법무·시험문제·설비 — query 패턴이 도메인 내 비교적 정형. 즉 rule-based 분류로 충분.

### 가정

- vLLM 0.6+ 폐쇄망 운영 가능
- LoRA 학습 인프라(GPU 워커)가 운영 환경 별도로 존재 (학습은 본 시스템 외부, 배포만 본 시스템 책임)
- Shared LLM은 단일 vLLM 인스턴스로 충분 (~수백 동시 요청 처리)
- Tenant SLM은 multi-tenant single vLLM 인스턴스로 시작, 도메인 대형화 시 dedicated 분리

가정이 깨지면 재검토 — 특히 Shared LLM이 동시성 한계에 부딪히면 horizontal scaling 또는 별도 endpoint 분기 ADR.

---

## Decision

### 1. Router — Rule-based YAML (학습 분류기 미사용)

- LangGraph `model_router` 노드가 yaml 규칙 평가
- `configs/platform/routing.yaml` (기본) + `configs/tenants/<id>/routing.yaml` (override)
- 결정론적·설명 가능·즉시 운영
- 본 시스템은 학습된 SLM router를 지원하지 않는다. 도입이 필요해지면 새 ADR을 작성한다.

### 2. Routing rule schema

```yaml
# configs/tenants/<id>/routing.yaml
default_route:
  model: tenant_slm
  use_lora: true
  use_rag: true
  ui_mode: chat_structured     # chat_structured | chat_streaming
  fallback_chain: [tenant_slm_no_lora, shared_llm]

rules:
  - name: simple_document_qa
    when:
      query_type: document_qa
      complexity: low
    route:
      model: tenant_slm
      use_lora: true
      use_rag: true
      ui_mode: chat_structured

  - name: synthesis_answer
    when:
      support_type: synthesis
      complexity: [medium, high]
    route:
      model: tenant_slm
      use_lora: true
      use_rag: true
      ui_mode: chat_structured

  - name: inference_answer
    when:
      support_type: inference
    route:
      model: shared_llm        # 추론은 큰 모델로
      use_lora: false
      use_rag: true
      ui_mode: chat_structured
      require_inference_judge: true  # ADR-010

  - name: assessment_generation
    when:
      query_type: assessment_generation
    route:
      model: shared_llm
      use_lora: false
      use_rag: true
      ui_mode: chat_structured
      require_similarity_check: true  # ADR-014

  - name: free_chat_streaming
    when:
      query_type: free_chat
      explicit_user_request: streaming
    route:
      model: tenant_slm
      use_lora: true
      use_rag: false
      ui_mode: chat_streaming
      citation_disabled: true   # ADR-010 citation은 streaming에 비활성
```

**평가 순서**: rules 배열 순서대로 첫 매치되는 룰 적용. 매치 없으면 default_route. **충돌 없는 결정론적 동작 보장**.

### 3. Query Classifier — 2-tier

LangGraph `classify_query` 노드 (라우팅 직전 실행). state에 `query_type`, `complexity`, `support_type` 채움.

#### Tier 1 — Rule (regex/keyword)
```yaml
# configs/platform/query_classifier.yaml
tier1:
  rules:
    - name: assessment_generation
      patterns:
        - "(?i)문제\\s*(생성|출제|만들)"
        - "(?i)([0-9]+)\\s*문제"
      assign:
        query_type: assessment_generation

    - name: document_qa
      patterns:
        - "(?i)(절차|방법|기준|조항|규정)"
      assign:
        query_type: document_qa
        complexity: low      # 단순 사실 질의

    - name: synthesis_pattern
      patterns:
        - "(?i)(종합|정리|요약|비교)"
      assign:
        query_type: document_qa
        support_type: synthesis
        complexity: medium

    - name: inference_pattern
      patterns:
        - "(?i)(허용되는가|가능한가|해석되는가|판단)"
      assign:
        query_type: document_qa
        support_type: inference
        complexity: high
```

#### Tier 2 — LLM (Tier 1이 모호 판정 시)
- Tier 1 매치 없으면 tenant_slm 호출, JSON 응답 강제
- prompt: `query`를 받아 `{query_type, complexity, support_type}` 반환
- schema: `configs/platform/prompts/query_classify_schema.json`

#### Classifier 출력 enum (공식 정의)

routing rule (`when:`), citation metadata, chat_logs, frontend type 모두 다음 enum을 단일 진실 소스로 본다. **신규 값을 추가하려면 본 ADR 보완 + frontend `lib/types.ts` + ADR-010 `support_type` 동시 갱신**.

| 필드 | 허용 값 | 의미 |
|---|---|---|
| `query_type` | `document_qa` | 문서 검색 + 답변 |
| | `assessment_extract` | Assessment 추출 모드 (ADR-014 §3) |
| | `assessment_generate` | Assessment 생성 모드 (ADR-014 §4) |
| | `assessment_hybrid` | Assessment 혼합 모드 |
| | `free_chat` | RAG 비활성 자유 대화 (chat_streaming) |
| | `meta` | 시스템·도움말 질의 |
| `complexity` | `low` / `medium` / `high` | 추론 단계 수 추정 |
| `support_type` | `direct` | ADR-010 §2 직접 인용 |
| | `synthesis` | ADR-010 §3 종합 |
| | `inference` | ADR-010 §4 추론 (LLM-as-judge 필수) |
| | `conflict` | ADR-010 §7 충돌 |
| | `unknown` | 분류 모호 — 기본 정책으로 fallback (Tier 2가 결정 못 한 경우) |

**Naming**: 본 ADR과 ADR-010·ADR-014·frontend는 모두 snake_case lowercase 사용. Python `Enum` 정의를 사용한다면 value(`'direct'`)와 member name(`DIRECT`)을 분리하고 직렬화는 value로 통일.

### 4. Endpoint 구성 (Topology)

> **2026-05-12 보완 노트 ([ADR-019](./019-infrastructure-sharing.md) §3·§4)**: 본 절은 두 vLLM instance(tenant + shared)를 별도 호스트로 가정했으나, GPU 자원 재산정 결과(174 RTX 3080 4장만 가용, WiSentinel과 분리 협의 2026-05-12) **단일 vLLM instance를 alias로 공유**하는 구조로 통합됐다. `tenant_slm`과 `shared_llm`은 같은 endpoint URL을 가리키고 LoRA 활성 여부(`multi_lora`)와 system prompt로만 분기한다. 본 §4 본문은 historical 설계로 보존하되 운영 진실 소스는 ADR-019 + `configs/platform/model.yaml`이다.

```text
[vLLM-tenant-shared]   → Tenant SLM 멀티테넌트 (LoRA per-request adapter)
                          base model: e.g., qwen3-7b-instruct
                          host: vllm-tenant:8000

[vLLM-shared]          → Shared LLM (큰 모델, fallback + inference judge)
                          base model: e.g., qwen3-14b
                          host: vllm-shared:8000

[vLLM-tenant-dedicated-X]  → 도메인 대형화로 dedicated 필요한 tenant만 (configs로)
                              host: vllm-tenant-X:8000
```

> **운영 현황 (ADR-019 §3·§4 결정)**: 위 topology는 2026-05-12 다음과 같이 통합됐다.
> - `tenant_slm` / `shared_llm`이 동일 vLLM instance를 가리키는 **alias** (`SHARED_LLM_BASE_URL == TENANT_SLM_BASE_URL`).
> - base_model: `Qwen2.5-7B-Instruct-AWQ` (4-bit 양자화 — 10GB GPU 제약).
> - tensor parallel size 2 (GPU 두 장 묶음). 14B 분리 instance는 GPU 부족으로 미운영.
> - LoRA는 multi-LoRA per-request(`max_loras=8`)로 같은 instance에서 분기. `shared_llm`은 `multi_lora: false`로 baseline 강제.
> - dedicated tenant 옵션은 ADR-019 §4 후속 결정 대기 (현재 미운영).

`configs/platform/model.yaml` (현 진실 소스):
```yaml
endpoints:
  tenant_slm:
    base_url_env: TENANT_SLM_BASE_URL   # 운영 환경: http://174.local:8000/v1
    base_model: Qwen2.5-7B-Instruct-AWQ
    quantization: awq
    multi_lora: true
    max_loras: 8
    tensor_parallel_size: 2

  shared_llm:
    base_url_env: SHARED_LLM_BASE_URL   # tenant_slm과 동일 endpoint (alias)
    base_model: Qwen2.5-7B-Instruct-AWQ
    multi_lora: false                   # judge·rewrite는 baseline
    tensor_parallel_size: 2
    note: "tenant_slm alias — 별도 vLLM instance 아님 (ADR-019 §4)"
```

`configs/tenants/<id>/model.yaml`:
```yaml
tenant_slm:
  endpoint: vllm_tenant_shared    # 또는 vllm_tenant_dedicated_security
  lora_adapter: security-policy-v1  # null이면 base model
shared_llm:
  endpoint: vllm_shared
  lora_adapter: null
inference_judge:                  # ADR-010
  endpoint: vllm_shared
  lora_adapter: null
```

### 5. LoRA Serving

#### vLLM 측 활성화
- vLLM 0.6+ `--enable-lora --lora-modules` 플래그
- 런타임 추가는 vLLM admin API로 등록

#### Adapter 학습·배포 워크플로우 (day 1)

```text
1. 학습 (본 시스템 외부 GPU 워커)
   - tenant 도메인 데이터(평가셋 + 추가 corpus)로 LoRA 학습
   - HuggingFace PEFT 표준 형식으로 export

2. 배포 (본 시스템 admin API)
   POST /api/admin/lora/upload
     - multipart upload (adapter weights)
     - metadata: tenant_id, version, base_model, training_meta
     - 검증: base_model 일치, weights shape, 안전성 스캔

3. 등록 (Adapter Registry)
   adapter_registry 테이블에 row 추가
   vLLM admin API로 LoRA 모듈 등록 (--lora-modules 추가)

4. 활성화 (LoRAOrchestrator.activate)
   - KeyHub에서 weights 가져오기 (ADR-019 §8 KeyHubAdapter)
   - VLLM_SHARED_LORA_PATH/<tenant>/<adapter_id>/adapter_model.bin 으로 저장
   - vLLM POST /v1/load_lora_adapter (lora_name + lora_path)
   - adapter_registry.status='active' 전이
   tenant configs/model.yaml의 lora_adapter 갱신
   TenantConfigService LISTEN/NOTIFY로 즉시 반영 (ADR-021 §2)

5. Rollback
   configs lora_adapter를 이전 버전으로 변경 (또는 null로 base 회귀)
```

#### 환경변수 명세

| 환경변수 | 의미 | 기본값 |
|---|---|---|
| `VLLM_SHARED_LORA_PATH` | backend ↔ vLLM 공유 디렉터리. KeyHub에서 fetch한 weights를 LoRAOrchestrator가 저장하고 vLLM이 `lora_path`로 읽는다 | `./var/lora` |

운영 환경(k8s/멀티노드)에서는 NFS·PersistentVolume(ReadWriteMany)로 backend pod와 vLLM pod 모두 같은 경로 마운트. dev compose는 단일 호스트 hostPath bind mount로 충분.

KeyHub 운영 모드 환경변수는 [ADR-019 §8](./019-infrastructure-sharing.md)을 참조.

#### Adapter Registry 스키마
```sql
CREATE TABLE adapter_registry (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    adapter_name VARCHAR(255) NOT NULL,    -- "security-policy-v1"
    version VARCHAR(50) NOT NULL,
    base_model VARCHAR(255) NOT NULL,      -- 호환성 검증용
    weights_path TEXT NOT NULL,            -- "models/lora/security/security-policy-v1/"
    training_metadata JSONB,
    uploaded_by VARCHAR(255),
    uploaded_at TIMESTAMP DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'registered',  -- registered | active | retired
    UNIQUE (tenant_id, adapter_name, version)
);
```

#### LoRA 미적용 tenant
`tenant_slm.lora_adapter: null`이면 vLLM이 base model로 응답. 학습 데이터 없는 tenant·일반 도메인은 base만 사용 가능.

### 6. UI Mode — Streaming vs Structured (두 모드 day 1 구현)

#### chat_structured (sync, citation 포함)
- ADR-010 기반: structured JSON answer_segments
- POST /api/{tenant_id}/chat
- spinner(1-3초) 후 완성 답변
- citation·support_level·verifier 모두 활성

#### chat_streaming (SSE, citation 비활성)
- 자유 텍스트 token-by-token streaming
- POST /api/{tenant_id}/chat/stream (SSE)
- citation 마커 [1], [종합:...] 등 미사용
- 빠른 자유 대화·요약·학습용

#### 모드 선택
- 라우팅 룰의 `ui_mode`에 따라 결정
- 사용자가 명시 요청 가능: request body에 `ui_mode_request: "streaming"` (rule이 `explicit_user_request`로 매치)
- tenant configs `default_ui_mode`로 기본값 지정

#### chat_logs
streaming 응답도 chat_logs에 저장 (full text + 모델 + latency). citation 컬럼은 빈 배열.

### 7. Fallback Chain

각 모델 호출이 실패할 때 chain의 다음 모델 시도:

```yaml
# tenant model.yaml
tenant_slm:
  endpoint: vllm_tenant_shared
  lora_adapter: security-policy-v1
  timeout_seconds: 10
  on_failure: tenant_slm_no_lora    # null = no fallback (그냥 ADR-010 fallback 응답)

tenant_slm_no_lora:
  endpoint: vllm_tenant_shared
  lora_adapter: null                # base model로 폴백
  timeout_seconds: 10
  on_failure: shared_llm

shared_llm:
  endpoint: vllm_shared
  lora_adapter: null
  timeout_seconds: 30
  on_failure: null                  # 끝, ADR-010 fallback 응답
```

#### 실패 분류
- **timeout**: 설정된 timeout_seconds 초과
- **OOM**: vLLM이 OOM 응답 (HTTP 503)
- **refused**: vLLM safety filter 거부 (있을 경우)
- **connection_error**: 네트워크 단절

각 실패는 chat_logs에 `model_failure_chain` JSONB로 기록 (어느 모델에서 어떤 이유로 실패했는지 시계열).

#### Retry·Backoff·Timeout 정책

각 단계 호출 자체의 retry는 **동일 모델 내부**가 아니라 **다음 단계로 escalate**한다 — 같은 endpoint를 여러 번 두드리면 latency만 누적되고 성공률 변화 없음. 운영 결정:

| 단계 | connect_timeout | read_timeout | 단계 내부 retry | 다음 단계 escalate 조건 |
|---|---|---|---|---|
| `tenant_slm` (with LoRA) | 5s | 10s (configs `timeout_seconds`) | 0 | timeout / OOM / connection_error / refused / parse_error |
| `tenant_slm_no_lora` | 5s | 10s | 0 | timeout / OOM / connection_error / refused |
| `shared_llm` | 10s (cold start 여유) | 30s | **1회** (cold start 회복 윈도) | retry 후에도 동일 분류 실패면 chain 끝 |
| chain 전체 끝 | — | — | — | ADR-010 fallback 응답 (`status: fallback`, `reason: low_generation_quality`) |

**Exponential backoff은 단계 *간*에서 적용**: `tenant_slm` 실패 후 `tenant_slm_no_lora`로 곧장 escalate, `shared_llm` 단계 내부 retry는 0.5초 backoff 후 1회.

**Cold start 보호**: `shared_llm` 첫 호출이 vLLM cold start와 겹치면 connect_timeout이 길게 소요될 수 있음 — connect_timeout만 별도로 길게 두고 read_timeout은 짧게 유지.

**User-facing 표현**: chain 전체 실패 시 frontend로 200 OK + body `{status: "fallback", fallback: {reason: "low_generation_quality", model_failure_chain: [...], retry_after_seconds: 60}}` 반환. 503은 사용하지 않는다 (k8s가 backend pod를 unhealthy 처리할 위험). ADR-017 §3 응답 schema와 정합.

**관제**: `chat_logs.model_failure_chain` 누적이 24h 윈도에서 X% 이상이면 platform_admin 알림(별도 metric 추적은 ADR-021 §6 health metrics 확장 후보).

### 8. Budget Tracking — 미지원

> 본 시스템은 라우팅 결정에 cost/latency budget을 사용하지 않는다. 라우팅은 query 신호(query_type, complexity, support_type) + tenant configs로만 결정. budget 추적이 필요해지면 별도 ADR로 도입.

### 9. LangGraph 흐름 통합 (전체 그래프)

```text
tenant_resolver (ADR-008) — *검증* 책임만
  · UserContext 빌드 (AuthFusionAdapter, ADR-018)
  · JWT.client_id ≡ URL path tenant_id 일치 검증, 불일치 시 403 (ADR-008 격리 3중 방어)
  · clearance/department/domain_groups를 user_tenant_membership에서 보강 (ADR-018 §4)
  · PostgreSQL `SET LOCAL app.current_tenant` *주입*은 FastAPI dependency 책임 (ADR-019 §2 진실 소스).
    tenant_resolver는 RLS context 주입을 *호출 안 함* — endpoint 진입 직후 session 획득 시점에 dependency가 처리.
  → tenant_health_check (ADR-011)
  → load_tenant_config (TenantConfigService, ADR-009)
       · platform defaults + tenant static + DB overrides 합성
  → build_acl_filter (ADR-004 + 보안등급 cumulative + tenant_id 자동 포함)
       · Qdrant payload filter 객체 생성: clearance ≥ chunk.security_level
                                          AND chunk.acl ∩ user.domain_groups ≠ ∅
                                          AND valid_from ≤ today AND (valid_until IS NULL OR valid_until ≥ today)
                                          AND approval_status = 'approved'
  → classify_query (Tier 1 + Tier 2 — 본 ADR §3)
  → model_router (yaml 규칙 평가 — 본 ADR §1·§2)
  → query_rewrite (조건부, ADR-011)
  → retrieve_context (ADR-011)
       · build_acl_filter 결과를 prefetch + fusion 단계에 모두 적용
  → [Gate 1] (ADR-010)
  → check_input_pii (ADR-020 Layer 1)
       · high severity → block fallback
  → generate_answer (선택된 model + lora_adapter — 본 ADR §5·§7)
       ├─ chat_structured: vLLM guided_json
       └─ chat_streaming: vLLM streaming
  → parse_response (chat_structured만, ADR-010)
  → verify_per_type (chat_structured만, ADR-010)
  → judge_inference (조건부, ADR-010 + 본 ADR §4 inference_judge endpoint)
  → assemble_response (chat_structured만)
  → mask_response_pii (ADR-020 Layer 4)
  → [Gate 2] (chat_structured만, ADR-010)
  → save_chat_log (모델 chain·실패 이력·PII 메타데이터 포함)
  → END
```

streaming 경로는 verify_per_type / parse_response / assemble_response / Gate 2를 skip — `state.ui_mode`로 LangGraph가 분기. ACL filter·PII Layer 1·4는 두 모드 모두 적용.

### 10. chat_logs 스키마 확장

```sql
ALTER TABLE chat_logs ADD COLUMN ui_mode VARCHAR(20);
-- "chat_structured" | "chat_streaming"

ALTER TABLE chat_logs ADD COLUMN routing_decision JSONB DEFAULT '{}';
-- { matched_rule: "synthesis_answer", model: "tenant_slm", lora_adapter: "security-v1", ... }

ALTER TABLE chat_logs ADD COLUMN model_failure_chain JSONB DEFAULT '[]';
-- [{model: "tenant_slm", failure: "timeout", at: "...", retried_with: "tenant_slm_no_lora"}, ...]

ALTER TABLE chat_logs ADD COLUMN classifier_decision JSONB DEFAULT '{}';
-- { tier1_matched: "synthesis_pattern", query_type: "...", complexity: "...", ... }
```

이 4개 컬럼이 운영 분석 토대 — 어떤 룰이 자주 트립되나, 어느 모델이 자주 timeout인가, 어떤 LoRA adapter가 활성인가, 분류기가 Tier 2까지 떨어지는 비율 등.

### 11. Admin Console — Model & Routing Console

비전 §13 신규 메뉴 (SPEC §5):
- **Routing Rules Editor**: yaml 편집 + dry-run (sample query로 룰 평가 시뮬레이션)
- **LoRA Adapter Registry**: 등록·활성화·archive·rollback
- **Endpoint Health Dashboard**: vLLM별 health·latency·동시성·실패율
- **Routing Analytics**: chat_logs 기반 routing decision 분포·Tier 2 비율·fallback chain 트리거 빈도

---

## Consequences

### 긍정적 영향

- 라우팅이 결정론적 yaml — 운영자가 변경·디버깅 즉시 가능
- LoRA 학습·배포 워크플로우 day 1 — tenant 도메인 특화 즉시 활용 가능
- streaming + structured 두 모드 day 1 — UX 자유도 ↑
- fallback chain 정합으로 모델 단일 실패가 사용자에게 노출되지 않음
- chat_logs 4 컬럼이 운영 분석 무한 — 어떤 라우팅 룰이 trip 되는지, 어떤 LoRA가 활성인지, 어디서 timeout인지

### 부정적 영향 / 부채

- 구현 표면적 매우 ↑ — yaml routing engine, classifier 2-tier, LoRA registry, streaming endpoint, fallback chain runtime, model & routing 콘솔
- vLLM multi-LoRA 메모리 (LoRA adapter당 GPU 메모리 일부) — 활성 LoRA 수 모니터링 필요
- 분류 정확도 책임이 yaml 규칙 작성자에게 — 도메인 평가셋으로 룰 검증 권장
- chat_streaming은 citation 비활성 — 사용자가 mode 선택의 trade-off 인식 필요 (UI에서 명시)

### 후속 작업

- `configs/platform/routing.yaml`, `query_classifier.yaml`, `model.yaml` 시드
- 마이그레이션: `adapter_registry` 테이블, chat_logs 4 컬럼 추가
- LangGraph 노드 구현: `classify_query`, `model_router`, streaming 분기
- vLLM 설정: --enable-lora, multi-tenant pooling
- LoRA upload admin API + 검증 (base_model 일치, weights shape)
- `/api/{tenant_id}/chat` (sync) + `/api/{tenant_id}/chat/stream` (SSE) 두 endpoint
- Admin console: Routing Rules Editor + LoRA Registry + Endpoint Health
- ADR-014에서 assessment_generation 룰의 require_similarity_check 정합 확인

---

## Alternatives Considered

### 1. SLM Router 학습
- **장점**: 동적 라우팅, 학습 데이터로 미세 조정 가능
- **기각 사유**: ML 프로젝트 별도 진행 필요, 학습 데이터 라벨링 부담, 운영 검증 비용. 도메인 query가 정형이라 rule-based로 충분. 추후 필요해지면 별도 ADR.

### 2. LoRA 미지원 (base model + prompt만)
- **장점**: 운영 단순, ML 인프라 0
- **기각 사유**: 비전 §11.2 "Tenant SLM + LoRA"가 도메인 차별화의 핵심. 보안·법무·시험 도메인 말투·용어 체화에 LoRA 가치 큼.

### 3. Streaming만 지원 (sync 제거)
- **장점**: 모든 응답 빠른 체감
- **기각 사유**: ADR-010 citation 검증·verify·judge가 sync 필요. 둘 다 지원이 자연스러움.

### 4. Sync만 지원 (streaming 제거)
- **장점**: 단순, citation 강조 일관
- **기각 사유**: 자유 대화·짧은 요약 use case에서 1-3초 spinner는 차가움. 모드 선택권이 UX 가치 ↑.

### 5. Endpoint per tenant (모든 tenant dedicated vLLM)
- **장점**: 격리 강함
- **기각 sayp**: GPU 비용 폭증. multi-tenant single vLLM이 메모리 효율적, 도메인 대형화 시점에만 dedicated 분리.

### 6. Inference judge에 별도 작은 모델 (qwen3-3b 등)
- **장점**: 비용 절감
- **기각 사유**: judge는 정확도가 사용자 신뢰에 직결. shared LLM 재사용이 안전. 호출 빈도 모니터링 후 비용 부담 시 작은 모델 도입 검토.

### 7. Routing rule을 코드로 (yaml 미사용)
- **장점**: 타입 안전, IDE 지원
- **기각 사유**: 운영자 변경 시 재배포 필요. yaml + LISTEN/NOTIFY 즉시 반영이 운영성 ↑.

### 8. Budget tracking 도입
- **장점**: 비용 통제
- **기각 사유**: cost 모델 정의·추적·UI 구현 부담 ↑. 도입 효과 미검증. 명시적 미지원.

---

## Related

- [ADR-002: Protocol/Adapter](./002-protocol-adapter-pattern.md) — Router, ModelClient 모두 Protocol
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — tenant scope
- [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md) — model.yaml, routing.yaml configs
- [ADR-010: Citation Trust v2](./010-citation-trust-v2.md) — inference_judge 호출 endpoint, structured/streaming 분리
- [ADR-011: Hybrid Retrieval v2](./011-hybrid-retrieval-v2.md) — query_rewriting LLM endpoint 결정
- [ADR-012: Lifecycle v2](./012-lifecycle-v2.md) — 임베딩 모델 교체와 별개로 LLM 모델 교체 절차 적용
- ADR-014 (예정): Assessment Workflow — assessment_generation 라우팅 룰 정합
- SPEC.md §5, §11~12, §14 — 폐기 (본 ADR이 흡수)

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted
