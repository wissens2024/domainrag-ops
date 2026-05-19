# ADR-010: Citation Trust v2 — Multi-type Citation & 원칙 8 재해석

## Status
**Accepted** (2026-05-08)

> **Supersedes ADR-005**. ADR-005의 direct citation verifier(Tier 1/2/3, Gate 1/2, confidence)는 본 ADR에 흡수·확장되었으며, 본 ADR이 단일 진실 소스가 된다. ADR-005는 historical reference로 보존되며 Status는 Superseded.

---

## Context

### 배경 (사실)

- 비전 §7은 citation을 **4가지 타입**으로 분류 — direct / synthesis / inference / conflict.
- ADR-005는 direct citation 한 가지만 다루며, similarity ≥ 0.55면 medium 이상으로 분류. **inference는 정의상 single chunk와 직접 매칭 ≪ 0.55 → 자동 차단됨**.
- CLAUDE.md 원칙 8: *"문서에 없는 내용은 추측하지 말고 확인 불가로 답변한다"* — 비전 §7.3의 inference citation과 정면 충돌.
- ADR-008 multi-tenant: citation 정책이 tenant별 가변 (보안 도메인은 inference 보수적, 법무 도메인은 합리적 추론 허용 등)
- ADR-009: citation 정책은 `configs/tenants/<tenant_id>/citation.yaml` + DB override

### 가정

- 폐쇄망 vLLM이 별도 LLM-as-judge 호출 가능 (Shared LLM endpoint 재사용)
- 한국어 NLI 모델 도입은 Phase 2로 미룸 (정밀도는 LLM-as-judge가 대체 가능)
- 답변 응답에 inference type이 자주 등장하지 않을 것이라는 가정 (대부분 direct, inference는 복잡 질문에 한정)
- 사용자가 [추론] 라벨을 보면 caveat을 인식할 만큼 UX 학습 가능

가정 중 하나라도 깨지면 재검토 — 특히 inference 빈도가 높으면 LLM-as-judge 비용 폭증.

---

## Decision

### 1. CLAUDE.md 원칙 8 재해석

**Before**:
> 문서에 없는 내용은 추측하지 말고 확인 불가로 답변한다.

**After**:
> **근거 없는 추측은 금지한다.** 단,
> - **direct**: 문서에 직접 적힌 내용은 그대로 인용한다.
> - **synthesis**: 복수 문서의 내용을 합쳐 결론을 낼 수 있고, 모든 구성 근거가 검증되면 인용한다.
> - **inference**: 복수 근거에서 *명시적으로 추론된 결론*만 허용한다. 단 (1) 답변에 [추론] 라벨이 시각적으로 분리되고 (2) LLM-as-judge가 추론 타당성을 검증하며 (3) 강한 caveat 문구가 자동 추가될 때만.
> - **conflict**: 문서 간 기준이 다르면 충돌을 명시하고 양측을 모두 보여준다. 결론은 사용자/담당 부서에 위임한다.
> - 위 4가지 어디에도 해당하지 않으면 "확인 불가" fallback 응답.

CLAUDE.md 본문은 이 ADR의 후속 작업으로 갱신.

### 2. Citation 4-type 분류

```python
class CitationType(str, Enum):
    DIRECT     = "direct"      # 단일 chunk 직접 인용
    SYNTHESIS  = "synthesis"   # 복수 chunk 결합 결론
    INFERENCE  = "inference"   # 복수 근거 추론 결론 (LLM-judge 필수)
    CONFLICT   = "conflict"    # 두 측 충돌 명시
```

### 3. Marker Syntax (한국어 라벨)

| 타입 | 본문 표기 |
|---|---|
| direct | `절차이다. [1]` |
| synthesis | `필요합니다. [종합: 1,2,3]` |
| inference | `해석됩니다. [추론: 1,2,3] 🔍` |
| conflict | `기준이 다릅니다. [충돌: 1 vs 2]` |

답변 본문에 이 syntax 그대로 들어감. 프론트가 파싱·렌더.

### 4. answer_segments JSON Schema (ADR-005 schema 확장)

```json
{
  "type": "object",
  "required": ["answer_segments"],
  "properties": {
    "answer_segments": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["text", "citation_type", "citations"],
        "properties": {
          "text": { "type": "string" },
          "citation_type": {
            "enum": ["direct", "synthesis", "inference", "conflict", "none"]
          },
          "citations": { "type": "array", "items": { "type": "integer" } },
          "conflict_groups": {
            "type": "array",
            "items": { "type": "array", "items": { "type": "integer" } }
          },
          "inference_chain": { "type": ["string", "null"] }
        }
      }
    },
    "limitations": { "type": ["string", "null"] }
  }
}
```

`citation_type=none`은 unsupported segment (Tier 3, ADR-005 그대로).
`conflict_groups`은 conflict일 때 양측 marker 배열의 배열 (예: `[[1], [2]]`).
`inference_chain`은 inference일 때 LLM이 자체 추론 요약.

### 5. Verifier per type

```text
parse_response (ADR-005 hybrid: structured primary + heuristic fallback)
  ↓
[Tier 1] 마커 정합성 (모든 type 공통, ADR-005 그대로)
  ↓
[per-type verification]
  
  direct:
    Tier 2: similarity(claim, chunk[k]) → support_level
            (ADR-005 그대로: ≥0.75 strong, ≥0.55 medium, <0.55 weak→제거)
  
  synthesis:
    Tier 2: 모든 cited chunks 각각 verify
    group_support_level = min(individual support_levels)
    weak가 하나라도 있으면 segment 자체 제거 + caveat 추가
  
  inference:
    Tier 2: 각 cited chunk verify (medium 이상 요구)
    LLM-as-judge: shared_llm 호출
      input: claim + cited chunks contents + inference_chain
      output: { valid: bool, confidence: 0~1, reasoning: str }
    valid=false 또는 confidence<0.6 → segment 제거 (자동 fallback)
    valid=true → support_level cap at "medium" (절대 strong 안 됨)
  
  conflict:
    Tier 2: 두 측 grouping 각각 verify
    양측 모두 strong/medium 요구
    한쪽이라도 weak이면 conflict 인정 안 됨 → segment 제거
    인정되면 support_level = "medium" (충돌은 본질적으로 결정 보류)

[Tier 3] unsupported segment 탐지 (citation_type=none, ADR-005 그대로)
  ↓
assemble_response (type별 다른 syntax + caveat 자동 추가)
```

### 6. Inference LLM-as-judge 호출

```python
async def judge_inference(claim: str, cited_chunks: list[Chunk], inference_chain: str) -> JudgeResult:
    prompt = INFERENCE_JUDGE_PROMPT.format(
        claim=claim,
        chunks=format_chunks(cited_chunks),
        chain=inference_chain,
    )
    response = await shared_llm.generate(prompt, schema=JUDGE_SCHEMA)
    return JudgeResult(**response)
```

`INFERENCE_JUDGE_PROMPT` (configs/platform/prompts/inference_judge.yaml):
```yaml
system: |
  당신은 RAG 답변의 추론 타당성을 평가하는 검증자입니다.

  주어진 claim이 cited chunks와 inference chain으로부터 정당하게 도출되었는지 판단하세요.

  판단 기준:
  - 모든 cited chunk에 결론과 직접 모순되는 내용이 없어야 한다
  - inference chain의 각 단계가 cited chunk로 뒷받침되어야 한다
  - 결론은 cited chunk에 명시되지 않은 *명시적 도출*이어야 한다 (단순 복사면 direct여야 함)
  - 외부 지식(상식·공통 지식)에 의존하면 invalid로 판정

  결과는 JSON으로 반환:
  { "valid": true|false, "confidence": 0~1, "reasoning": "..." }
```

비용·latency: inference 응답 1건당 LLM 호출 1회 추가. 응답 빈도가 적으면 부담 작음. 빈도 높으면 ADR-013 model routing이 inference judge에 더 작은 SLM 사용 결정 가능.

### 7. Conflict 자동 탐지

- **Primary**: LLM이 답변 생성 시 명시적 식별 (prompt에 "여러 기준이 있으면 [충돌: ...] 표시" 지시)
- **Secondary**: 휴리스틱 후처리 — 날짜·수치·규정 번호가 cited chunks 사이에 다르면 자동 conflict 후보로 마킹 (ADR-013 routing config로 활성화)
- NLI 모델 기반 자동 탐지는 Phase 2

### 8. configs/citation.yaml v2 (per-tenant 확장)

```yaml
# configs/platform/citation.yaml (모든 tenant 기본값)
verification:
  tier2:
    embedding_model: bge-m3
    similarity_metric: cosine
    thresholds: { strong: 0.75, medium: 0.55 }
  tier3:
    enable: true
    unsupported_action: annotate
  inference_judge:
    enable: true
    model: shared_llm
    confidence_threshold: 0.6
    schema_path: configs/platform/prompts/inference_judge.yaml
  conflict_detection:
    primary: llm_explicit
    heuristic:
      enable: true
      patterns: [date_diff, numeric_diff, rule_id_diff]

allowed_citation_types:
  - direct
  - synthesis
  - inference
  - conflict

gates:
  retrieval: { min_top1_rerank: 0.6, min_strong_chunks: 2 }
  generation: { min_verified_count: 1, max_unsupported_ratio: 0.5, min_confidence: 0.5 }

confidence_weights:
  retrieval: 0.30
  verified:  0.30
  supported: 0.20
  coverage:  0.20

# tenant override 예시: configs/tenants/security/overrides.yaml
# citation.allowed_citation_types: [direct, synthesis, conflict]   # inference 거부 (보안 도메인 보수)
# citation.verification.inference_judge.confidence_threshold: 0.8
```

`allowed_citation_types`로 tenant가 특정 type을 거부 가능. 거부된 type은 답변 생성 prompt에서도 차단됨.

### 9. chat_logs 스키마 확장

```sql
ALTER TABLE chat_logs ADD COLUMN citation_types JSONB DEFAULT '[]';
-- 예: ["direct", "direct", "synthesis", "inference"]

ALTER TABLE chat_logs ADD COLUMN inference_judge_results JSONB DEFAULT '[]';
-- 예: [{"segment_index": 3, "valid": true, "confidence": 0.78, "reasoning": "..."}]

ALTER TABLE chat_logs ADD COLUMN conflict_groups JSONB DEFAULT '[]';
```

### 10. UI 렌더링 (Citation Inspector)

답변 카드 (SPEC §5.2 갱신):
- direct/synthesis: ADR-005 컨벤션 그대로 (`[1]` / `[종합: 1,2]`)
- inference: `[추론: 1,2,3]` + 🔍 아이콘, hover에 LLM judge reasoning 요약 + caveat 자동 첨부 ("이 결론은 문서에 직접 적혀있지 않습니다. 담당 부서 확인 권장.")
- conflict: 별도 박스 — "두 기준이 충돌합니다" + 양측 인용 + "담당 부서 확인 필요" 안내

CitationPanel (SPEC §5.3 갱신):
- citation_type 배지 표시
- inference: LLM judge result 패널 (valid/confidence/reasoning)
- conflict: 양측 비교 뷰

신규 메뉴 — Citation Inspector (SPEC §5 신규):
- 답변별 4-type 분포 차트 (관리자용)
- 각 segment → cited chunks 매핑 테이블
- inference 답변의 LLM-as-judge 출력 표시
- conflict 답변의 양측 비교 뷰
- type별 fallback 비율 트렌드

### 11. Fallback 분류 확장

기존 ADR-005 fallback_reason:
- `low_retrieval`
- `low_generation_quality`

추가:
- `inference_judge_rejected` — LLM judge가 invalid 판정
- `conflict_unverifiable` — 충돌 인정 불가 (한쪽이 weak)
- `disallowed_type` — tenant policy가 해당 citation type 거부

---

## Consequences

### 긍정적 영향

- 비전 §7 4-type 정합 + CLAUDE.md 원칙 8 재해석으로 두 문서 일관
- direct/synthesis는 ADR-005 verifier 그대로 활용 (재구현 비용 0)
- inference에 LLM-as-judge 안전망 — 추론 답변의 hallucination 위험 감소
- per-tenant `allowed_citation_types`로 도메인별 보수성 조정 가능
- chat_logs 확장으로 type별 운영 분석 가능 (어떤 도메인이 inference를 많이 쓰나, judge 거절률은 등)

### 부정적 영향 / 부채

- LLM-as-judge 호출 추가로 inference 응답 latency +1~3초
- inference 빈도가 높은 도메인(법무·해석)에서 비용 부담
- Conflict 자동 탐지가 LLM·휴리스틱 의존 → false negative 위험. NLI(자연어 추론) 기반 탐지는 본 시스템에서 지원하지 않는다 — false negative가 운영에서 문제로 부각되면 별도 ADR 작성.
- 4-type marker syntax 파싱 로직 복잡 (frontend·backend 양쪽)
- CLAUDE.md 원칙 8 wording 변경 — 기존 코드 리뷰 가이드 갱신 필요
- ADR-005 retire — 외부 참조(과거 PR·이슈)가 ADR-005 가리키면 본 ADR로 redirect 필요

### 후속 작업

- CLAUDE.md 원칙 8 재작성 (별도 todo: "CLAUDE.md 갱신")
- ADR-005 Status: Superseded by ADR-010 + 본 ADR로 redirect 노트
- `configs/platform/citation.yaml` v2 시드 + tenant overrides 예시
- `configs/platform/prompts/inference_judge.yaml` 신설
- chat_logs 마이그레이션 SQL (citation_types, inference_judge_results, conflict_groups)
- LangGraph 노드: `verify_citations` 분리 — `verify_per_type`, `judge_inference`(조건부), `detect_conflict_heuristic`
- SPEC §5.2/§5.3/§6.3/§9 본 ADR로 갱신 (마지막 단계 일괄)
- Citation Inspector 메뉴 사양 (ADR-016 §3.6에 흡수, day 1 구현 범위)

---

## Alternatives Considered

### 1. 원칙 8 강한 유지 — inference 제거
- **장점**: hallucination 위험 0, CLAUDE.md 그대로
- **기각 사유**: 비전 §7.3 후퇴. 법무·정책 해석 도메인에서 사용자 가치 ↓. 운영 시 "왜 추론을 못 하나" 불만.

### 2. inference를 fallback의 한 종류로 ("추론 필요, 확인 불가" 식 거부)
- **장점**: 가장 안전
- **기각 사유**: 사용자 경험 차가움. 답변할 수 있는 추론 질문에도 fallback 비율 ↑. 비전 의도와 어긋남.

### 3. NLI 모델 도입 (multi-NLI for synthesis/inference, contradiction NLI for conflict)
- **장점**: 정밀도 ↑, 비용 0 추가 호출
- **기각 사유**: 한국어 NLI 모델 라이선스·평가 부담. 폐쇄망 모델 추가 도입. Phase 2 후보.

### 4. inference에 LLM-as-judge 대신 휴리스틱
- **장점**: 비용 0
- **기각 사유**: 추론 타당성은 휴리스틱으로 못 판단. false positive 위험. CLAUDE.md 원칙 8 신뢰 보장 불가.

### 5. citation_type을 marker syntax 없이 metadata로만
- **장점**: 답변 본문 가독성 ↑
- **기각 사유**: 사용자가 "이게 추론인지 모름" → caveat 의식 불가 → CLAUDE.md 신뢰 약속 깨짐. 시각적 분리 필수.

---

## Related

- [ADR-001: Citation 메타데이터](./001-citation-metadata-design.md) — 필드 스키마, citation_type 필드 추가 보완
- [ADR-005: Citation 신뢰 모델 v1](./005-citation-trust-and-fallback.md) — **superseded by 본 ADR**
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — per-tenant citation policy
- [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md) — citation.yaml configs 위치
- ADR-013 (예정): Model Routing — inference_judge에 어느 모델 사용
- SPEC.md §5/§6.3/§9 — 폐기 (본 ADR이 흡수)
- [CLAUDE.md 원칙 8](../../CLAUDE.md) — 본 ADR 후속 작업으로 wording 갱신

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted (supersedes ADR-005)
