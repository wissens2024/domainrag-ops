# ADR-025: Assessment Item Multimodal Ingestion & Figure-Reuse Generation

## Status
**Accepted** (2026-06-18)

> ADR-014는 assessment item을 **text-only**(question_text·choices·answer·explanation)로 정의했고, ADR-015 exam-engineer 스키마는 시험 *문제 item*을 "Assessment Console에서 직접 등록"으로만 규정했다. 그러나 실제 기출(정보처리기사 등)은 **이진트리·ER다이어그램·플로차트·코드·표** 등 *그림 의존* 문항이 상당수이며, 텍스트 추출만으로는 "아래 Tree 구조에 대하여…" 같은 문항이 **그림 없이 풀 수도 검증할 수도 없다**. 또한 기출 PDF는 연도·회차별로 다수라 단발 적재가 아닌 **재사용 가능한 ingestion**이 필요하다. 본 ADR이 (1) 멀티모달 item 모델, (2) PDF ingestion 파이프라인, (3) 그림 문항 정책, (4) VLM 통합, (5) 대량 dedup·승인 게이팅, (6) 사용자 문제 검수 엔드포인트를 단일 진실 소스로 정의한다.

---

## Context

### 사실 (현 상태)
- `AssessmentItemRecord`(rag_core)·`assessment_items` ORM·`ItemUpsertRequest`(backend)는 모두 **text-only**. 이미지/그림 자산을 담을 필드가 없다.
- 텍스트 추출(PyMuPDF `get_text`)은 래스터 이미지·벡터 드로잉을 **버린다**. 실측: 2022년 2회 필기 PDF는 4페이지에 래스터 이미지 5개 + 전 페이지 벡터 드로잉을 포함하며, Q37(이진트리 후위순회)은 트리 그림이 추출에서 누락되어 **풀 수 없는 문항**으로 적재된다.
- generate(ADR-014 §3 Mode 2)·validator(§5)·similarity(§2)는 **text-only `LLMClient`/`Embedder`** 만 사용한다. 그림을 이해하는 경로가 없다.
- 인프라에는 **Qwen Vision(Ollama, 174 GPU0)** 이 존재하나(`OLLAMA_BASE_URL`, ADR-019) assessment 파이프라인에 배선되지 않았다.
- MinIO prefix-per-tenant 저장 + at-rest 암호화(ADR-024)는 이미 가동 가능하다 — 그림 bytes 저장의 토대.
- `POST /admin/assessment/items`(create)는 validator를 호출하지 않고 draft로 저장만 한다 → "사용자 입력 문제 검수" 경로 공백.

### 제약
- **OCR ≠ 그림 이해**: OCR(이미지→글자)은 코드·스캔 텍스트엔 유효하나 트리/다이어그램엔 무용. 그림 이해는 **VLM(이미지→이해)** 이 필요하다.
- **새 그림 합성 불가**: text LLM은 없던 다이어그램을 그려낼 수 없다. 따라서 그림 문항의 *맨땅 생성*은 본질적으로 불가하다.
- 인용·audit replay(절대원칙 10·11)와 답변 시점 보존을 위해, 그림 자산도 item 수명주기와 함께 보존돼야 한다.
- 외부 LLM/API 금지(원칙 12) — VLM도 폐쇄망 Ollama.
- 코드 하드코딩 금지(원칙 8) — 임계치·VLM 모델·게이팅 정책은 configs.
- 멀티테넌트 1차 분기(원칙 1·2) — 그림 자산도 prefix/RLS per domain.

---

## Decision

### 1. 멀티모달 item 모델 — 이미지 자산 first-class

`assessment_items`에 컬럼 2개를 추가한다(ADR-014 §1 확장):

```sql
ALTER TABLE assessment_items
  ADD COLUMN assets JSONB DEFAULT '[]',
  ADD COLUMN figure_dependent BOOLEAN DEFAULT FALSE;
```

`assets[i]` 구조 (순수 데이터):
```json
{
  "asset_id": "uuid",
  "kind": "image",
  "storage_key": "items/<domain_id>/<item_id>/<asset_id>.png",
  "content_hash": "sha256:...",
  "source_page": 4,
  "bbox": [x0, y0, x1, y1],
  "vlm_description": "루트 a, 좌측 b(좌 d), 우측 c(좌 e(좌 g,우 h), 우 f) … | null"
}
```

- **이미지 bytes는 MinIO** `items/<domain_id>/...`(prefix-per-tenant) — ADR-024 SSE-KMS 암호화를 그대로 상속(읽기 경로 무변경).
- `bbox`·`source_page`는 audit/재현용(어느 PDF 영역에서 왔는지).
- `vlm_description`은 **선택적 텍스트 보강** — reference·validator의 text 파이프라인이 그림을 간접 활용. 검색/임베딩의 1차 소스는 여전히 `question_text`.
- `figure_dependent=true`: 그림 없이는 못 푸는 문항. generate 진입·검증 분기에 사용.
- RLS·인덱스는 ADR-014 §1과 동일.

### 2. Ingestion 파이프라인 — PDF → items (제품 기능, 단발 스크립트 아님)

Protocol `AssessmentPdfImporter`(ADR-002 패턴). 단계:

```text
PDF
 ├─ 텍스트 추출 + 파싱(문항 경계 / 보기 ①②③④ / 정답표) → 후보 item
 ├─ 그림 탐지: 래스터(get_images) + 벡터 드로잉 bbox 클러스터링
 │     → 영역 crop/render(PNG) → content_hash → MinIO 저장 → asset
 ├─ 문항 ↔ 그림 연결: 그림 bbox가 속한 문항 번호 범위(다음 문항 시작 전)로 귀속
 ├─ (옵션) VLM 서술: figure asset → Qwen Vision → vlm_description
 ├─ dedup(§5) → draft item upsert(source='imported', figure_dependent 표식)
 └─ import job 기록 → Review Queue
```

- PDF 레이아웃은 출처별 편차가 있으므로 **포맷 어댑터** 구조로 둔다. 본 ADR은 표준 4지선다 + 말미 정답표 레이아웃(에듀온류)을 첫 어댑터로 한다.
- 정답표 매핑·그림-문항 귀속 등 휴리스틱은 **사람 검수 전제**(시험 도메인 오류는 치명적). 모든 import 결과는 draft에서 시작한다(§5 게이팅 예외 제외).
- admin API + UI(PDF 업로드 → import job → 검수)로 노출.

### 3. 그림 문항 정책 — 지원/비지원 명시

**지원한다:**
- (a) 그림 보존 + 표시 + **출제(extract)** — 기존 그림 문항을 원본 이미지째 서빙.
- (b) **기존 그림 재사용 + 새 질문 생성** — 같은 asset을 고정한 채 VLM이 그림을 이해해 신규 Q&A 생성(§4).
- (c) 사용자 그림 문항 **등록·검수**(§6).

**지원하지 않는다 (명시):**
- **자유형 새 그림의 자동 생성은 본 시스템에서 지원하지 않는다.** text/vision LLM은 신규 다이어그램을 합성하지 않는다.
- 구조형(트리·그래프 등) spec→렌더러 기반 새 그림 생성은 필요 시 별도 ADR로 다룬다(본 ADR 범위 밖).

generate 진입 규칙: `figure_dependent=true` reference는 **reuse 전략만** 적용하며, 신규 그림 합성은 금지한다.

### 4. VLM 통합 — Qwen Vision (폐쇄망)

Protocol `VisionLanguageClient`(ADR-002). 구현체는 Ollama Qwen Vision(`OLLAMA_BASE_URL`, ADR-019).

용도 3가지:
- **ingestion 서술**: figure asset → `vlm_description`
- **figure-reuse generate**: (그림 + 지시) → 새 question_text·choices·answer (그림 자산은 그대로 reference_item의 것을 승계)
- **figure validator**: 그림 문항의 정답·해설 정합성 판단

정책:
- 외부 API 금지(원칙 12) 준수 — Ollama 내부만.
- VLM 모델명·on/off·타임아웃은 configs(`configs/platform/assessment.yaml`, 코드 하드코딩 금지).
- **degrade(회복탄력성, ADR-023 정신)**: VLM 비가동 시 그림 문항은 **extract/표시까지만** 동작하고, 그림 기반 생성·VLM 검증은 skip(로그 기록). 텍스트 전용 경로는 영향 없음.

### 5. 대량 dedup + 승인 게이팅

기출 PDF가 다수 누적될 때(연도·회차) 단순 적재가 만드는 문제를 막는다.

- **import 시 dedup**: 동일 subject 내 `question_text` 유사도 ≥ `duplicate_threshold`(configs, 기본 ADR-014 §2의 0.85)면 신규 등록 대신 기존 item에 출처 tag를 추가하거나 skip하고 import job에 기록한다.
- **자동 승인 게이팅**: `source='imported'`이고 운영자가 신뢰 출처(기출 원본)로 표시한 경우에 한해 configs gate로 **자동 `approved`** 를 허용한다. `generated`·사용자 입력은 항상 `draft`→검수(Y2 규칙과 정합, 자동 approved 없음).
- **검색 전략 전환 임계**: item 수가 configs 임계를 초과하면 generate의 reference·dedup을 on-the-fly 임베딩에서 **Qdrant `items_<domain_id>` 사전 인덱싱**으로 전환한다(ADR-014 §1이 의도한 경로). 임계 이하에서는 현행 SQL 후보 + on-the-fly 임베딩을 유지한다.

### 6. 사용자 문제 검수 엔드포인트 (공백 해소)

```
POST /api/{domain_id}/assessment/validate
```
- 본문: 단건 또는 복수 item(question_text·choices·answer·explanation·assets?).
- 기존 `AssessmentValidator`(ADR-014 §5) 재사용 — figure 포함 시 §4 VLM validator 경로.
- 저장 없이 validator 결과(valid/score/reasoning/suggestions per validator + aggregate)를 반환하거나, draft 저장 후 `validator_results`를 채운다(요청 플래그).
- Workbench/Item Editor에 "검수" 액션으로 노출. ADR-014 §5 aggregate 규칙(Y2) 동일 적용.

---

## Consequences

### 좋아지는 점
- 그림 의존 문항(이진트리·ER·플로차트·코드·표)이 **온전히 적재·출제**된다 — Q37류 사용 가능.
- 기출 그림을 자산으로 **재사용 + 새 질문 생성**으로, 그림 문항도 생성형 워크플로우에 편입.
- 모든 기출 PDF에 재사용되는 ingestion으로 단발 스크립트 부채 제거.
- 사용자 입력 문제 검수 경로 확보 — 사용자 요구 충족.
- dedup·승인 게이팅으로 대량 누적 시 품질·운영 부담 통제.

### 비용·주의
- 멀티모달 모델·MinIO 자산·VLM 호출로 운영 표면적·비용·지연 증가.
- VLM 서술/검증의 정확도가 사용자 신뢰에 직결 — 검수 의무 유지.
- ingestion 휴리스틱(그림-문항 귀속·정답표 매핑)은 PDF 레이아웃 편차에 취약 → 어댑터별 검증 + 사람 검수 전제.
- 신규 그림 합성 비지원으로, 완전 신규 그림 문항은 사람 저작에 의존.

### 다른 ADR과의 관계
- **ADR-014 보완·확장**(supersede 아님): item 모델에 멀티모달 추가, generate에 figure-reuse 분기, validator에 VLM 경로, §6 검수 엔드포인트 추가.
- **ADR-015 보완**: 시험 문제 item의 그림 자산을 정의(학습자료 input_schema와 별개).
- **ADR-024 상속**: 그림 자산 at-rest 암호화.
- **ADR-019 활용**: Qwen Vision(Ollama).
- **ADR-023 정신 계승**: VLM 비가동 시 degrade.

---

## Alternatives Considered

1. **OCR로 그림을 텍스트화** — 트리/다이어그램은 글자가 아니라 OCR이 무의미. 기각.
2. **그림 문항을 아예 제외** — 기출의 상당수가 그림 의존이라 커버리지 손실 과다. 기각.
3. **그림을 question_text에 base64/링크로 인라인** — 데이터 모델 오염, 검색·검증 분기 복잡. 별도 `assets` 필드가 깨끗. 기각.
4. **신규 그림까지 합성(이미지 생성 모델)** — 폐쇄망 제약·시험 정확도·검증 불가 위험. 본 시스템 비지원으로 명시.
5. **VLM 없이 그림 문항은 표시/출제만** — 가능하나 "그림 재사용 + 새 질문"(사용자 핵심 요구)을 못함. VLM을 선택적으로 도입하되 비가동 시 이 수준으로 degrade.

---

## 참조 구현 (예정)
- 모델: `assessment_items.assets/figure_dependent`, `AssessmentItemRecord`, alembic migration
- ingestion: `AssessmentPdfImporter`(+포맷 어댑터), 그림 crop/저장, import job
- VLM: `VisionLanguageClient`(Ollama Qwen Vision) + configs
- generate: figure-reuse 분기 / validator: VLM 경로 / `POST /assessment/validate`
- 설정: `configs/platform/assessment.yaml`(dedup·게이팅·VLM·임계)
