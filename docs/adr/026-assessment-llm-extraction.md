# ADR-026: Assessment Item LLM/VLM Extraction — 포맷 무관 ingestion

## Status
**Accepted** (2026-06-18)

> ADR-025 §2는 기출 PDF ingestion을 **규칙 기반 파서**(`ExamPaperParser` — `【N과목】`·`①②③④`·말미 정답표 블록 가정)로 정의했다. 그러나 실측 결과 이 방식은 **포맷 1종(2020년 개편 + eduon 2022)에만** 동작한다: 정보처리기사 기출 56개 PDF(2004~2022)를 돌리면 **단 2개만** 완전 정확(2019-2·2022-2)하고, ~20개는 정답표 미인식(정답 0), ~13개는 구 교육과정이라 과목 미매핑, 다수는 문항 분할이 깨진다. 규칙 기반 파서는 레이아웃 가정을 코드에 박으므로 **포맷이 바뀔 때마다 코드를 다시 짜야 하는** 잘못된 추상화다. 본 ADR은 추출 계층을 **LLM/VLM 기반 범용 추출 + 정답표 교차검증 + 신뢰도 게이팅**으로 재정의해, 포맷이 달라도 코드 수정 없이 흡수하고 정확도를 보장한다.

---

## Context

### 사실
- `ExamPaperParser`(ADR-025 §2)는 regex로 과목 헤더·보기 마커·정답표 블록 위치를 가정한다 → 가정 위반 시 파싱 실패.
- 56-PDF 검증(로컬, 운영 무변경): 완전정상 2 / 정답표 미인식 ~20 / 과목 미매핑 ~13 / 문항분할 깨짐 ~21. 일부는 hwp 변환·스캔본.
- 구 교육과정(2019 이전)은 과목 체계·문항 레이아웃이 현행 5과목과 전혀 다르다. 정답표 형식도 연도·출처마다 상이.
- 폐쇄망에 **shared LLM(vLLM Qwen)** 과 **Qwen Vision(Ollama, 174 GPU0)** 이 가동 가능(ADR-013·019). 구조화 추출은 LLM이 잘하는 작업.
- ADR-025가 정의한 멀티모달 item 모델(`assets`/`figure_dependent`)·MinIO 저장·dedup/승인 게이팅·검수 엔드포인트는 그대로 유효.

### 제약
- **정확도 우선**: 시험 도메인은 오답·정답없음·깨진 문항을 approved로 넣으면 안 된다(절대원칙 8 재해석). LLM hallucination을 반드시 검증으로 거른다.
- **외부 API 금지**(원칙 12): 추출도 폐쇄망 vLLM/Ollama만.
- **코드 하드코딩 금지**(원칙 8): 모델·프롬프트·임계치는 configs.
- **audit/재현**(원칙 10·11): 추출 출처(원본 페이지·정답표 근거)를 보존.
- **offline 경로**: ingestion은 질의 경로와 분리된 배치 작업 — latency보다 정확도·견고성이 우선.

---

## Decision

### 1. 추출은 Protocol/Adapter — LLM 추출기를 범용 기본으로

`AssessmentItemExtractor` Protocol(ADR-002)을 둔다. 추출 산출물은 ADR-025의 `ParsedExamItem`(번호·지문·보기·정답·과목·그림참조·플래그)과 동일 형태.

| 어댑터 | 용도 |
|---|---|
| `RuleBasedExamExtractor` (= 기존 `ExamPaperParser`) | **고정 포맷 1종** 고속·결정적 추출 (예: eduon 2022). 비용 0. |
| `LlmItemExtractor` (**기본**) | **포맷 무관** 범용 추출. 레이아웃 차이를 모델이 흡수. |

ingestion 파이프라인(ADR-025 §2 `AssessmentImportService`)은 어댑터를 주입받아 동작하며, **기본값은 `LlmItemExtractor`**. 포맷이 달라져도 어댑터 교체·코드 수정 없이 동작한다.

### 2. LLM 추출 흐름

```text
PDF
 ├─ 페이지 텍스트 추출 (fitz) + 그림 영역(VLM 대상, §5)
 ├─ 문항 페이지: 텍스트(+그림)를 LLM에 전달
 │     → JSON 스키마 강제: {items:[{number, question_text, choices:[..], answer,
 │        subject, figure_ref?}]}
 │     → 페이지 경계 문항은 청크 오버랩/재조합으로 처리
 ├─ 정답표 페이지: 정답을 별도 추출(§3)
 └─ 후보 item 병합
```

- subject는 모델이 **현행 5과목 enum으로 매핑**하거나, 매핑 불가(구 교육과정)면 `legacy:<원문과목>` 라벨 + `tags`에 보존.
- 출력은 **StructuredOutput(JSON 스키마) 강제** — 자유 텍스트 파싱 금지.

### 3. 정답표 교차검증 (정확도 핵심)

LLM이 뽑은 정답을 **그대로 신뢰하지 않는다.** PDF에 인쇄된 정답표(있으면)를 독립적으로 추출해 대조한다.

- 정답표 추출: 텍스트/표 구조면 규칙, 불규칙·스캔이면 VLM.
- 각 문항: `llm_answer == answer_key[number]` →
  - **일치** → `answer_verified=true`
  - **불일치/정답표 없음** → `answer_verified=false` + 사유 기록 → 검수 대상
- 정답표가 아예 없는 출처는 모든 문항 `answer_verified=false`(자동승인 불가, draft).

### 4. 신뢰도 게이팅 / 자동 승인 (ADR-025 §5 정합)

```text
item이 (a) answer_verified=true AND (b) choices 4개 정상 AND (c) 4-validator 통과
   AND (d) 신뢰 출처(기출 원본, 운영자 표시)
  → 자동 approved 후보
그 외 (불일치·정답없음·저신뢰·과목 legacy 등)
  → draft (Review Queue)
```

- 자동 approved는 configs gate(`auto_approve.enabled`)로 명시 활성. 기본은 보수적(검수).
- "직접 다 approved" 요구는 본 게이팅을 통과한 문항에 한해 충족된다 — **검증 통과분만 자동 승인**, 나머지는 draft로 분리(품질 보호).

### 5. 그림 — VLM (ADR-025 §4 재사용)

- 그림 영역 탐지·crop·MinIO 저장은 ADR-025 §1·§2. `figure_dependent`는 **실제 그림 링크 유무**로 확정(텍스트 키워드 아님).
- 그림 의존 문항은 VLM이 지문·정답 정합을 함께 판단(§3 검증에 활용).

### 6. 모델·configs (폐쇄망)

```yaml
# configs/platform/assessment.yaml (발췌)
extraction:
  default_extractor: llm            # llm | rule_based
  llm:
    model: shared_llm               # ADR-013 — vLLM Qwen
    vision_model: qwen-vision        # ADR-019 — Ollama
    max_chars_per_call: 8000
    answer_cross_check: true
  auto_approve:
    enabled: false                  # 기본 검수. 신뢰출처 일괄 적재 시 운영자가 활성.
    require_answer_verified: true
    require_validators_pass: true
```
모델명·프롬프트·임계치는 configs. 외부 API 미사용(원칙 12).

### 7. 비용·성능

- 페이지당 LLM 호출 → 규칙 파서보다 느리고 토큰 소비. **offline ingestion**이라 질의 latency와 무관.
- 동시성 제한 + 페이지 청크 + 결과 캐시(동일 PDF 재처리 회피)로 비용 통제. import job에 처리 시간·토큰 기록.

### 8. 지원하지 않음 (명시)

- **외부 LLM/API**: 추출에도 사용하지 않는다(폐쇄망).
- **자유형 새 그림 합성**: ADR-025 §3 유지(비지원).
- LLM 추출이 저신뢰일 때 **자동 approved 강행**: 하지 않는다(draft 검수).

---

## Consequences

### 좋아지는 점
- **포맷이 바뀌어도 코드 무수정** — 2004~2022 및 미래 출처를 동일 경로로 흡수.
- 정답표 교차검증으로 hallucination을 거르고, 검증 통과분만 자동 승인 → 정확도 + 대량 처리 양립.
- 규칙 파서는 고정포맷 고속 어댑터로 보존(둘 다 활용).
- 멀티모달·dedup·게이팅·검수(ADR-025) 위에 자연스럽게 얹힘.

### 비용·주의
- LLM 호출 비용·시간(배치). 캐시·동시성으로 완화.
- 정답표가 없는 출처는 자동 승인 불가 → 검수 부담(불가피, 정확도 대가).
- 추출 프롬프트·스키마 튜닝 필요. 모델 교체 시 회귀 검증.
- 모델 출력 비결정성 → 재현을 위해 (모델·프롬프트 버전, 입력 해시)을 import job에 기록.

### 다른 ADR과의 관계
- **ADR-025 §2 보완·대체**: extraction 단계를 규칙 기반에서 LLM/VLM 범용으로. ADR-025의 데이터 모델·저장·dedup·게이팅·검수 엔드포인트는 유지.
- **ADR-013 활용**: shared LLM. **ADR-019**: Qwen Vision. **ADR-014 §5**: validator 재사용.

---

## Alternatives Considered
1. **포맷별 규칙 어댑터 N종 추가** — 출처·연도마다 어댑터를 짜야 함(현 문제의 연장). 유지보수 폭증. 기각(범용성 없음).
2. **OCR 후 규칙 파싱** — OCR은 레이아웃·정답표 구조를 복원하지 못함. 그림 이해 불가. 기각.
3. **외부 문서 파싱 API/LLM** — 폐쇄망 원칙 위반. 기각.
4. **LLM 추출 + 정답표 교차검증 없이 자동 승인** — hallucination이 approved로 유입. 기각(시험 도메인 부적합).
5. **현행 규칙 파서 유지, 2개만 사용** — 커버리지 2/56. 기각(목적 미달).

---

## 참조 구현 (예정)
- Protocol: `AssessmentItemExtractor` (rag_core). 어댑터: `RuleBasedExamExtractor`(기존), `LlmItemExtractor`(신규).
- 교차검증: `AnswerKeyVerifier`. 게이팅: `AssessmentImportService`에 auto-approve 분기(ADR-025 §5).
- VLM: `VisionLanguageClient`(ADR-025 §4). configs: `configs/platform/assessment.yaml`.
