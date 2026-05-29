# ADR-023: Unified Conversational Pipeline — 근거 기반 자동 분기 (모드 토글 폐기)

## Status
**Accepted** (2026-05-29)

> ADR-013 §6은 `chat_structured`/`chat_streaming`을 **사용자가 선택하는 두 모드**로 정의했고, ADR-016 §6은 이를 `ModeSelector` UI로 노출했다. 운영에서 이 설계가 사용자에게 모드 판단·책임을 떠넘기고, 분류기가 사용자의 명시 선택을 덮어쓰는 모순(아래 Context)을 일으켰다. 본 ADR이 **모드 토글을 폐기**하고 **항상 검색 → 근거 유무로 시스템이 자동 분기**하는 단일 대화 파이프라인을 단일 진실 소스로 정의한다.

---

## Context

### 발견된 모순 (사실)

- 프론트 `ModeSelector`(ADR-016 §6)가 `structured`/`streaming`을 사용자가 미리 고르게 했다.
- `chat_structured` 경로(`/chat`)는 진입 시 `ui_mode="chat_structured"`를 고정하고도, 내부 `classify_query`가 입력을 `free_chat`으로 분류하면 `model_router`가 `ui_mode=chat_streaming`을 산출 → `fallback_reason="ui_mode_streaming_required"` → `ui_mode_router`가 `fallback` 노드로 단축 → 사용자에게 *"이 질문은 streaming 모드로 라우팅되었습니다. /chat/stream 엔드포인트로 다시 요청해 주세요."* 안내문이 나갔다.
- 즉 **사용자가 명시적으로 "문서 검색"을 골라도, "안녕" 같은 입력은 분류기가 자유대화로 재라우팅**해 명시 선택을 무력화했다. (`routing.yaml`의 `free_chat_streaming` 룰이 ADR-013 §2가 명시한 `explicit_user_request: streaming` 가드를 누락한 것이 직접 원인.)

### 설계 결함의 본질

- 시스템에 **자동 분류기**(`classify_query`→`model_router`)와 **수동 모드 토글**이 동시에 존재하여 충돌했다. 둘 중 누가 우선인지 정의되지 않았다.
- "모드"를 **입력 시점에 사용자가 결정**하는 모델은 부자연스럽다. 자연스러운 어시스턴트는 한 대화 안에서 문서 기반 답과 일반 대화를 자유롭게 오가며, 그 판단은 **시스템 책임**이다.
- 근거 유무에 대한 판단과 책임을 사용자에게 떠넘기면 안 된다. 시스템이 근거를 찾고, 있으면 제시하고, 없이 답할 때는 그 사실을 **UI로 명확히 구분**해 책임을 진다.

### 가정

- 검색은 모든 질의에 대해 시도해도 비용이 감당 가능하다 (collection-per-tenant 단일 검색 1회).
- "안녕"류 잡담은 검색 결과가 비거나 약해 자연히 ungrounded 경로로 떨어진다 — 별도 입력 분류 게이트 불요.
- 생성 모델은 근거 없는 일반 대화 답변을 낼 수 있다 (base SLM 능력).

가정이 깨지면(예: 검색 비용 폭증) 재검토 — query 신호로 검색 skip을 결정하는 별도 최적화 ADR.

---

## Decision

### 1. 사용자 모드 토글 폐기 — 단일 대화 입구

- 프론트 `ModeSelector` 제거. 사용자는 모드를 고르지 않는다.
- 사용자 채팅 입구는 단일 sync `/chat` (ADR-017 §3.1)로 통일. **모드는 사용자 입력이 아니라 검색 결과(근거 유무)의 산물**이다.
- 본 시스템은 사용자가 선택하는 대화 모드를 지원하지 않는다.

### 2. 항상 검색 — `query_type`은 RAG를 끄지 않는다

- 모든 질의는 `retrieve_context`를 거친다. `classify_query`는 `support_type`/`complexity` 힌트(검증 강도·judge 필요 여부)만 제공하고, **`use_rag`를 false로 만들지 않는다.**
- `routing.yaml`의 `free_chat_streaming` 룰과 `ui_mode_streaming_required` redirect는 제거한다. `query_type=free_chat`은 더 이상 RAG를 끄거나 streaming으로 보내지 않는다 (검색이 비면 §3의 ungrounded 경로가 자연 처리).

### 3. Gate 1 = 거부가 아니라 **분기점**

ADR-010 §4 Gate 1(retrieval 품질 검사)의 **결과 처리만** 바꾼다. 검사 로직(`min_top1_rerank`, `min_strong_chunks` 등)은 그대로다.

| Gate 1 | 경로 | 동작 |
|---|---|---|
| **pass** (근거 강함) | **grounded** | 기존 structured 생성 + verify_tier1/2 + judge_inference + 4-type citation (ADR-010 전체) |
| **fail** (근거 약/없음: `empty_retrieval`·`gate1_failed`) | **ungrounded** | 일반 지식 대화형 생성. citation 없음. verify/judge skip. `grounding="ungrounded"` 표시 |

- ungrounded 경로는 **citable context를 주입하지 않는다** — 근거 없는 답에 마커가 새지 않도록. (약한 chunk를 비인용 background로 넣는 것은 tenant config 옵션, 기본 off.)
- "근거 없음"은 **§4의 UI 배지로 명시**한다. 답변 본문에 caveat 문구를 강제 삽입하지 않는다 — 인사말 등에서 어색하고, 구분은 UI 책임(시스템이 책임지고 표시)이기 때문이다. 생성 프롬프트에는 도메인 사실(수치·조항·절차)을 단정하지 말라는 지침을 둔다.
- **진짜 fallback(거부)은 별도 사유로만 유지**: `input_pii_blocked`(ADR-020), `low_generation_quality`(생성 chain 전체 실패, ADR-013 §7). 이 둘은 ungrounded 대화 답변이 아니라 명시적 거부/안내다.

### 4. `grounding`은 1급 응답 필드 — 시스템이 책임지고 UI로 구분

- `RAGState.grounding: "grounded" | "ungrounded"` 추가. `_build_chat_response` metadata와 `chat_logs.routing_decision`에 적재.
- UI는 둘을 **명확히 구분 표시**한다:
  - `grounded` → "문서 근거" 배지 + 4-type citation 마커 (기존).
  - `ungrounded` → "일반 대화 · 등록 문서 근거 없음" 배지. 마커 없음, citation 패널 없음.
- 근거 유무를 사용자가 *추측*하게 두지 않는다. 화면이 명시한다.

### 5. 원칙 5·8 재해석 — 라벨링된 ungrounded 대화는 허용

CLAUDE.md 원칙 5·8은 "근거 없는 추측 금지"였다. 본 ADR은 이를 다음으로 정밀화한다:

- **금지되는 것**: 근거 없는 답을 *근거 있는 것처럼*(또는 출처를 숨기고 조용히) 내보내는 것. fabricated citation은 여전히 절대 금지(금지 3·4 유지).
- **허용되는 것**: 검색 결과 근거가 없을 때, **`grounding="ungrounded"`로 UI에 명시 구분**되고 caveat가 붙은 대화형 답변. 시스템이 "근거 없음"을 책임지고 드러내므로 사용자를 오도하지 않는다.

이는 trust posture의 의도된 변경이며, **UI 명시 구분이 안전장치**다.

### 6. 두 경로 공통 처리

- PII Layer 1(입력)·Layer 4(출력 마스킹)는 grounded·ungrounded **두 경로 모두** 적용 (ADR-020).
- 두 경로 모두 `save_chat_log`로 끝난다. ungrounded도 `chat_logs`에 저장(answer + grounding + 빈 citations). `ui_mode`는 더 이상 사용자 선택이 아니라 `grounding`을 반영("chat_structured" 유지하되 grounding으로 의미 구분).

### 7. Transport — 단일 sync `/chat`

- grounded 답변은 생성 후 verify가 필수이므로 본질적으로 sync다. 대화 입구를 sync `/chat` 하나로 통일한다.
- `/chat/stream`(SSE)과 streaming 서비스는 **사용자 대화 모드로는 폐기**한다. 토큰 progressive rendering이 필요하면, 그것은 사용자 모드가 아니라 grounded/ungrounded 양 경로에 공통 적용되는 **내부 렌더링 최적화**로 별도 ADR에서 다룬다. 본 시스템은 사용자가 선택하는 streaming 모드를 지원하지 않는다.

### 8. LangGraph 흐름 (변경분)

```text
... → retrieve_context → gate_1
  ├─ gate1_passed  → generate_answer(structured, contexts) → parse → verify_tier1
  │                  → detect_conflict → verify_tier2 → judge_inference
  │                  → detect_unsupported → assemble_response  [grounding=grounded]
  └─ gate1_failed  → generate_ungrounded(conversational, no citable context)
                     [grounding=ungrounded, citations=[]]
  (두 경로 합류) → mask_response_pii → compute_confidence → gate_2 → save_chat_log → END

  input_pii_blocked → fallback(거부) → save_chat_log
  low_generation_quality(생성 chain 전체 실패) → fallback(거부) → save_chat_log
```

- `ui_mode_router`(streaming redirect 분기)는 제거. `gate_1_router`가 `generate_answer`(grounded) / `generate_ungrounded`(ungrounded)로 분기.

---

## Consequences

### 긍정적 영향
- 사용자는 모드를 고르지 않는다 — 자연스럽게 대화하고, 시스템이 문서 검색 여부를 판단한다.
- "문서검색 선택 + 안녕 → redirect 안내문" 모순이 구조적으로 사라진다.
- 근거 유무가 시스템 책임 + UI 명시로 드러나, 사용자가 추측할 필요가 없다.
- 자동분류 vs 수동토글 충돌이 제거되어 라우팅이 단순·결정론적이 된다.

### 부정적 영향 / 부채
- trust posture 변경: Gate 1 fail이 "거부"에서 "근거 없는 대화 답변"으로 바뀐다. 검색이 놓친 도메인 질문이 일반 지식으로 답해질 수 있다 — UI 명시 구분이 유일한 안전장치이므로 배지/caveat가 절대 누락되면 안 된다.
- `/chat/stream`·streaming 서비스가 사용자 모드로는 사문화 — 코드 제거 또는 내부용 격하 필요.
- `grounding` 필드가 응답·로그·프론트 타입에 추가되어 동기화 대상이 늘어난다.

### 후속 작업
- `routing.yaml` free_chat_streaming 룰 제거, `query_classifier.yaml` greeting 룰은 유지(힌트용)하되 RAG 비활성 효과 제거.
- `nodes.py`: `model_router_node` redirect 제거, `generate_ungrounded_node` 추가, `gate_1_router` 분기 확장, `grounding` 세팅.
- `rag_graph.py`: `ui_mode_router` 제거, ungrounded 노드 결선.
- `rag_service._build_chat_response`: `grounding` 반영, `ui_mode_streaming_required` fallback 블록 제거.
- 프론트: `ModeSelector` 제거, `chat/page.tsx` 단일 `/chat`, `AnswerCard` grounded/ungrounded 배지, `lib/types.ts` `grounding`.
- 테스트: model_router(redirect 제거), 그래프 ungrounded 분기, 응답 빌더 grounding, 프론트 typecheck.
- CLAUDE.md ADR Index에 ADR-023 추가, ADR-013·016 Status 보완 표기.

---

## Alternatives Considered

### 1. 명시 모드 토글 유지 + 분류기보다 우선
- **장점**: 코드 변경 최소.
- **기각**: 모드를 사용자가 고르게 하는 것 자체가 부자연스럽고, 판단 책임을 사용자에게 넘긴다. 사용자 요구의 핵심("시스템이 판단")에 반함.

### 2. 입력 분류로 free_chat vs document_qa를 사전 게이트
- **장점**: 잡담에 검색 skip → 약간의 비용 절감.
- **기각**: 분류 오판 시(잡담처럼 보이는 도메인 질문) 검색을 건너뛰어 근거를 놓친다. "항상 검색 후 근거 유무로 판단"이 더 견고하고, 모순의 근원(사전 분류 게이트)을 제거한다.

### 3. Gate 1 fail 시 기존처럼 "확인 불가" 거부 유지
- **장점**: 원칙 5의 보수적 trust posture 유지.
- **기각**: 사용자 요구("문서가 없으면 일반 대화로 답")에 반함. 잡담조차 거부되어 부자연스럽다. UI 명시 구분으로 안전성을 확보하므로 허용으로 전환.

### 4. ungrounded에 약한 chunk를 citable context로 주입
- **장점**: 근거에 가까운 답 가능.
- **기각**: 검증 안 된 chunk를 인용하면 fabricated/약한 citation 위험(금지 3·4). ungrounded는 citation 없음으로 명확히 구분하는 것이 안전.

---

## Related
- [ADR-010: Citation Trust v2](./010-citation-trust-v2.md) — Gate 1 검사 로직 유지, fail 처리만 분기로 refine
- [ADR-013: Model Routing & SLM/LLM](./013-model-routing-and-slm-llm-strategy.md) — §6 모드 선택·§2 free_chat_streaming 룰 supersede
- [ADR-016: UI Architecture](./016-ui-architecture.md) — §6 ModeSelector supersede
- [ADR-017: API Specification](./017-api-specification.md) — §3.1 `/chat` 단일 입구, 응답 `grounding` 필드 추가
- [ADR-020: PII & Audit](./020-pii-and-audit-integration.md) — PII Layer 1·4 두 경로 공통 적용

---

**작성자**: AI Assistant + Project Owner
**최종 승인**: 2026-05-29
**변경 이력**: 초안 작성, 즉시 Accepted
