# ADR-005: Citation 신뢰 모델 및 Fallback 정량 기준

## Status
**Superseded by ADR-010** (2026-05-08)

> **본 ADR은 [ADR-010: Citation Trust v2](./010-citation-trust-v2.md)에 의해 supersede됨.** ADR-010이 본 ADR의 모든 결정(3-tier verifier, hybrid claim 추출, 2-게이트 fallback, confidence 산출, 응답 스키마 분기, UI 컨벤션)을 흡수하고 4-type citation(direct/synthesis/inference/conflict) + LLM-as-judge + 원칙 8 재해석을 추가한다. **단일 진실 소스는 ADR-010**.
>
> 본 ADR은 historical reference로 보존되며, direct citation 한정 verifier 결정의 원본 근거 자료로만 참고. 신규 코드/SPEC 갱신은 ADR-010을 인용해야 한다.

---

## Context

### 배경 (사실)

- **ADR-001**은 `Citation.support_level: "weak" / "medium" / "strong"`과 `verified: bool` 필드를 선언했으나 **산출 규칙은 비어 있음**.
- **SPEC.md §9.3**은 MVP citation 검증을 "마커 번호 매칭"만으로 정의 — 포맷 정리 수준.
- **ADR-003**이 정의한 LangGraph `should_generate` 분기는 `len(reranked_chunks) > 0` 단일 조건. fallback 진입 정량 기준 없음.
- **CLAUDE.md 원칙 8**: "문서에 없는 내용은 추측하지 말고 확인 불가로 답변한다"

### 문제

이 상태에서는 다음 세 종류 오류가 모두 통과한다.

- **Hallucinated citation**: LLM이 [1]을 붙였지만 실제로는 chunk 1과 무관
- **Misattributed citation**: 답변 문장이 chunk 2에서 왔는데 [1]이 붙음
- **Unsupported claim**: 답변 본문에 마커 없는 사실 주장

또한 fallback이 어떤 신호로 발동할지 정량 기준이 0이다. 설명 가능성과 fallback이 제품 정체성과 직결되므로, 검증 모델과 정량 게이트를 MVP 계약 수준으로 확정해야 한다.

### 가정

- vLLM이 `guided_json` 또는 `outlines`/`xgrammar` 중 하나로 schema-constrained 생성을 지원한다
- 폐쇄망에 한국어 문장 분리기(Kiwi 또는 KSS)를 라이선스/배포 측면에서 도입할 수 있다
- bge-m3 임베딩이 50~300자 claim_text에서 의미 유사도를 안정적으로 산출한다
- 평가 셋(`data/eval/domain_qa.jsonl`)이 confidence 가중치/임계치 튜닝에 사용 가능한 규모로 구축된다 (SPEC §13 Prompt 18 기준)

위 가정 중 하나라도 깨지면 본 ADR 재검토 대상.

---

## Decision

### 1. 3-tier Citation 검증 — MVP 전체 포함

- **Tier 1 — 마커 정합성**: LLM 출력의 [k] 마커가 context 번호 범위 내인지 확인, 외부면 제거
- **Tier 2 — claim ↔ chunk 의미 유사도**: bge-m3 cosine similarity 산출 → `support_level` 분류, 임계치 미달 마커 제거
- **Tier 3 — unsupported claim 탐지**: 인용 마커가 없는 factual segment 식별 → annotate

### 2. Claim 추출 — Hybrid (Structured primary + Heuristic fallback)

- **Primary**: vLLM `guided_json`으로 `{answer_segments: [{text, citations: [int]}], limitations}` 강제. 각 `segment.text`가 `claim_text`가 됨
- **Fallback**: JSON 파싱 실패 시 자유 텍스트 응답을 Kiwi로 분해, 각 마커 [k]에 대해 직전 1문장을 `claim_text`로 사용
- 두 경로 모두 동일한 verifier에 입력

### 3. Support Level 산출 (configs/citation.yaml로 분리)

```
similarity = cosine(embed(claim_text), embed(chunk.content))   # bge-m3 재사용

s ≥ 0.75       → support_level="strong",  verified=true
0.55 ≤ s < 0.75 → support_level="medium", verified=true (UI 회색 경고)
s < 0.55       → support_level="weak",   verified=false → 응답에서 marker 제거
```

시작 임계치이며 평가 셋으로 튜닝.

### 4. Unsupported Claim 처리 — Annotate 단독

응답에 그대로 노출하되 `unsupported: true` 플래그를 부여하여 프론트가 `⚠` 인라인 표시.
Strict(거절)/Regenerate(재생성)는 MVP에서 도입하지 않음. 평가에서 unsupported 비율이 높으면 Phase 2에서 도입 검토.

### 5. Fallback 2-게이트 (ADR-003 `should_generate` 강화·대체)

- **Gate 1 (LLM 호출 전)**:
  - `rerank_score > 0.5`인 chunk 수 < 2
  - 또는 `top1 rerank_score < 0.6`
  - 위 조건 하나라도 만족 → `fallback_reason="low_retrieval"`

- **Gate 2 (verify 후)**:
  - `verified citation 수 = 0`
  - 또는 `unsupported_claim_ratio > 0.5`
  - 또는 `confidence < 0.5`
  - 위 조건 하나라도 만족 → `fallback_reason="low_generation_quality"`

### 6. Confidence 산출

```
confidence = 0.30 * normalize(top1_rerank_score)
           + 0.30 * (verified_count / max(marker_count, 1))
           + 0.20 * (1 - unsupported_claim_ratio)
           + 0.20 * coverage_score
```

가중치 시작값. 평가 러너(SPEC §13 Prompt 18)가 가중치 튜닝 결과를 출력하도록 책임을 추가.

### 7. 응답 스키마 분기

- **Success**: `answer`, `citations[]`, `metadata.confidence`, `metadata.verifier {tier1, tier2_avg_similarity, tier3}`
- **Fallback**: `answer`(확인 불가 안내문), `fallback {reason, near_misses[], suggested_actions[]}`, `citations: []`
- chat_logs 스키마에 `confidence FLOAT`, `fallback_reason VARCHAR(50)`, `verifier_metrics JSONB`, `unsupported_ratio FLOAT` 컬럼 추가

### 8. Streaming — MVP 미도입

JSON 부분 파싱 복잡도와 verifier 의존을 피하기 위함. Phase 2에서 streaming 도입 여부 재검토.

### 9. UI 렌더링 컨벤션 (SPEC §5.2 명시)

- `citations: [1, 3]` segment → 답변 본문 `[1][3]` **분리 표기**
- `support_level="medium"` 마커: 그대로 표시 + hover `⚠`
- `support_level="weak"`: 응답 단계에서 제거되어 미등장
- Tier 3 unsupported segment: `⚠` 인라인

---

## Consequences

### 긍정적 영향

- ADR-001이 선언만 했던 `support_level`, `verified` 필드가 산출 규칙을 갖춘다
- `confidence`, `fallback_reason`, `unsupported_ratio` 신규 신호가 관리자 로그·대시보드·평가 러너의 토대
- 추가 모델 도입 0 — bge-m3 재활용. 검증 비용 +50~100ms/응답
- structured/heuristic 이중 경로로 vLLM JSON 안정성과 무관하게 동작 보장

### 부정적 영향 / 부채

- 답변 합성 책임이 backend로 이동(LLM segments → 자연 텍스트 join). LangGraph에 `parse_response`, `assemble_response` 노드 추가 필요
- MVP는 streaming 미제공 — 1~3초 spinner 압력
- vLLM `guided_json` 미숙 시 휴리스틱이 사실상 primary로 떨어져 정밀도 저하 위험 — 가정 모니터링 필요
- `configs/citation.yaml`의 임계치는 평가 셋 없이 적정값 미상 — 시작값은 휴리스틱

### 후속 작업

- `configs/citation.yaml` + `configs/prompts/answer_schema.json` 신설
- chat_logs 마이그레이션 SQL 추가
- SPEC.md 차이 적용: §5.2 / §5.3 / §6.3 / §7.5 / §8.1 / §9.3 / §9.4 / §10.2 / §14.1 + 금지사항 11
- ADR-003의 `should_generate`에 Gate 1 정량 조건 반영(보완 노트)
- 평가 러너(SPEC Prompt 18) 출력에 confidence weight 튜닝 결과 포함
- Phase 2 후보 ADR: NLI 모델 도입 / Streaming / Strict·Regenerate 모드

---

## Alternatives Considered

### 1. Marker 매칭만 (SPEC v1 §9.3 그대로)

- **장점**: 비용 0, 결정론적
- **기각 사유**: hallucinated/misattributed citation을 못 잡음. ADR-001이 선언한 `support_level`/`verified` 필드가 의미를 가질 수 없음. CLAUDE.md 원칙 8 위반.

### 2. Structured output 단독 (휴리스틱 fallback 없음)

- **장점**: 검증 정확도 최고, 코드 단순
- **기각 사유**: vLLM JSON 생성 실패 시 답변 자체를 못 내고 fallback로 떨어져 사용자 체감 품질 악화. 한국어 LLM의 JSON 안정성이 평가 전이라 단독 의존은 위험.

### 3. Strict reject (unsupported 1개라도 발견 시 fallback)

- **장점**: CLAUDE.md 원칙 8과 가장 정렬
- **기각 사유**: 평가 데이터 없이 적용하면 fallback 비율이 높아질 위험. 운영 도입 후 사용자 거부감 클 것으로 예상. annotate로 시작해 평가 후 strict로 전환할 수 있게 `configs/citation.yaml`에서 전략 분리 가능하게 둠.

### 4. Streaming 응답 + dual call(검증용 별도 호출)

- **장점**: UX 우수
- **기각 사유**: LLM 호출 비용 2배, partial JSON parsing 복잡도, sync 단일 호출과 응답 일관성 보장 어려움. Phase 2 검토.

### 5. NLI 모델 도입 (Tier 2 정밀도 향상)

- **장점**: claim entailment 판정이 cosine similarity보다 정확
- **기각 사유**: 추가 모델 1개 폐쇄망 반입·유지 부담. MVP에서는 bge-m3 재활용으로 시작하고 평가 결과에 따라 Phase 2 도입.

---

## Related

- [ADR-001: Citation 메타데이터 설계](./001-citation-metadata-design.md) — 필드 스키마 (본 ADR이 산출 규칙 보완)
- [ADR-003: LangGraph 오케스트레이션](./003-langraph-orchestration.md) — `should_generate` 강화
- [ADR-004: 보안 & ACL 모델](./004-security-acl-model.md) — 직접 의존 없음
- [SPEC.md §9 Citation 생성 및 검증 로직](../../SPEC.md) — 본 ADR로 대폭 개정

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted
