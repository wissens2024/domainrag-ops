# ADR-027: 대화형 근거기반 출제·채점 (Conversational Grounded Assessment)

## Status
**Accepted** (2026-06-19)

> ADR-014/025는 assessment 출제·검수를 **콘솔(admin) 폼·큐 워크플로우**로 정의했고, ADR-023은 사용자 채팅을 **RAG 질의응답 단일 파이프라인**(항상 검색 → 근거 유무 자동 분기)으로 정의했다. 그 결과 사용자 채팅에서 "과목별 2문제 출제해줘"가 RAG 문서 검색(0건) → Gate 1 fail → **ungrounded 잡담**으로 새버린다. 본 ADR이 사용자 채팅에서 **자연어로 출제·채점**하는 경로를 단일 진실 소스로 정의한다 — 문제은행을 근거(grounding)로 새 문항을 생성하고, 검증을 통과한 문항만 노출하며, 대화형으로 채점한다. ADR-014/025/023/013을 연결·확장한다(supersede 아님).

---

## Context

### 사실 (현 상태)
- 사용자 채팅(`/{domain}/chat`, ADR-023)은 `classify_query → model_router → retrieve_context → Gate 1 → generate(grounded/ungrounded)` 파이프라인이다. exam-engineer 도메인은 **RAG 문서·chunk가 0건**이고 지식은 전부 `assessment_items`(별도 저장소, ADR-014/금지#7)에 있다. 실측(2026-06-19): "정보처리기사 과목별로 2문제씩 출제해줘" → `일반 대화` 배지 + "2문제씩 출제는 일반적입니다…" 일반론. 출제가 일어나지 않았다.
- **분류·라우팅 뼈대는 이미 존재하나 실행부가 없다(죽은 분기).** `configs/platform/query_classifier.yaml`에 `query_type: assessment_generation`(패턴 `출제|문제 만들|문제 생성`), `routing.yaml`에 `assessment_generation` 룰(`require_similarity_check: true`)이 정의돼 있으나, `require_similarity_check` 신호를 소비하는 노드가 없어 그대로 `generate_answer_node`(RAG 문서 검색)로 흘러 ungrounded로 떨어진다.
- 콘솔에는 근거기반 생성 엔진 `AssessmentGenerateService.generate()`가 가동 중이다 — approved 문항을 `items_<domain>`(Qdrant)에서 검색해 few-shot reference로 주입하고, similarity/dedup, `AssessmentValidator`(answer/explanation/choices/difficulty 4종, Y2 aggregate), retry, `temperature=0.4`로 새 문항을 만든다. **단, 생성물을 `repo.upsert(source="generated")`로 DB에 draft 저장한다.**

### 사용자 의도 (사실)
- 문제은행 색인의 목적은 **동일 기출 재사용이 아니라**, LLM이 "이 수준·스타일·범위로 **새 문제를 생성**"하게 하는 **레퍼런스(grounding)** 다.
- 채팅은 ChatGPT처럼 **1문제·N문제·과목별·모의고사·채점을 자연어로 유연하게** 처리해야 한다(고정 흐름 강제 금지).
- **할루시네이션 최소화가 1순위 요구.**

### 제약
- ADR-023은 "입력 사전 분류로 RAG를 끄는 게이트"를 의도적으로 기각(오분류 시 근거 누락). 따라서 출제 의도 분기는 *검색을 끄는* 게이트가 아니라 *다른 근거원(문제은행)으로 라우팅하는 액션 분기*여야 하며, 확신이 낮으면 RAG 경로로 폴백해야 한다.
- 코드 하드코딩 금지(원칙 8) — 임계치·temperature·기본 개수는 configs.
- 멀티테넌트 1차 분기(원칙 1·2), 외부 API 금지(원칙 12) 준수.

---

## Decision

### 1. 출제·채점은 "액션 의도"로 분기한다 (질의응답 아님)
- 채팅 그래프에 **출제/풀이요청/채점 의도** 판별을 둔다. 이미 존재하는 `query_classifier`의 `assessment_generation` 분류를 실행에 연결하고, 신규 **`assessment_node`**(LangGraph)를 추가해 의도가 출제면 이 노드로, 아니면 기존 ADR-023 RAG 경로로 보낸다.
- **확신이 낮으면 RAG 경로로 폴백한다** — ADR-023의 "사전 분류로 근거를 놓치지 말라"는 철학을 보존한다. 출제 분기는 검색을 끄지 않고 *근거원을 문제은행으로 바꾼다*.

### 2. 출제 = 문제은행 근거기반 generate (동일 기출 서빙 아님)
- 기존 `AssessmentGenerateService`를 재사용한다 — approved 문항을 `items_<domain>`에서 검색해 few-shot 근거로 깔고 **새 문항을 생성**한다. **색인된 원본을 그대로 출제하지 않는다.**

### 3. 파라미터는 자연어에서 유연 추출
- 개수·과목·난이도·모의고사 여부를 메시지에서 파싱한다(미지정 시 configs 기본값). 단일 고정 흐름(한문제씩 vs 일괄)을 강제하지 않는다 — 1문제, N문제, 과목별, 모의고사 모두 같은 경로에서 처리한다. 과목 미지정·다과목 요청은 도메인의 과목 분포를 활용해 분배한다.

### 4. 할루시네이션 최소화 게이트 (필수)
- **(a) 근거 강제**: 참조 문항이 없으면 출제하지 않고 "해당 과목 승인 문항이 없어 출제할 수 없다"고 안내한다(자유 창작 금지).
- **(b) 검증 게이트**: `AssessmentValidator` 통과 문항만 사용자에게 노출하고, 실패분은 폐기·재생성한다.
- **(c) 결정성**: 낮은 temperature(configs) + 원문 언어 고정(ADR-026 드리프트 가드 재사용).

### 5. 채점(검수)은 대화형이되 정답 고정
- 사용자가 답하면 **그 세션에서 생성된 문항의 정답·해설에만 근거**해 채점·피드백한다. 채점 중 새 정답·새 사실을 지어내지 않는다. (콘솔의 "검수"=운영자 품질 QA와는 다른, 응시자 채점이다.)

### 6. 채팅 생성 문항은 ephemeral — DB 문제은행에 저장하지 않는다
- 채팅 출제 문항은 **대화 세션(채점용) + `chat_logs`(audit, 절대원칙 9·10)에만** 남기고, `assessment_items` DB에 upsert하지 않는다. ChatGPT식 일회성이며, 미검수 생성분이 문제은행·Review Queue를 오염시키지 않는다(Y2 정합).
- 이를 위해 `AssessmentGenerateService.generate()`에 **`persist=False` 경로**를 추가한다(콘솔은 `persist=True` 유지). 좋은 문항의 영속화는 콘솔 워크플로우의 명시 저장으로만 한다.

### 7. `grounding`·로깅 확장
- `RAGState.grounding`에 **`assessment`** 값을 추가한다(기존 `grounded`/`ungrounded`와 구분). 응답 metadata·`chat_logs.routing_decision`에 적재한다.
- 채팅 응답은 출제/채점을 일반 답변과 구분되는 **문항 카드**로 표시하고, `chat_logs`에 종류(`assessment_generate`/`assessment_grade`)를 기록한다. `classifier_decision`/`routing_decision`은 기존 save_chat_log 경로가 이미 저장한다.

---

## Considered Alternatives

1. **동일 기출 그대로 서빙(extract 노출)** — 색인 문항을 채팅에서 그대로 출제. **기각**: 사용자가 명시적으로 "동일문제 재사용 아님"을 요구. 정답까지 담긴 원본 노출은 유출·재사용성 문제.
2. **근거 없는 자유 LLM 생성** — 문제은행 무시, LLM 창작. **기각**: 수준 미달·할루시네이션. 1순위 요구(할루 최소) 정면 위반.
3. **별도 "퀴즈 모드" UI 토글** — 채팅에 모드 스위치. **기각**: ADR-023이 모드 토글을 폐기. ChatGPT처럼 단일 대화에서 자연어 분기가 사용자 요구.
4. **출제도 ADR-023처럼 의도 판별 없이 항상 검색→근거유무로만 분기** — **기각**: 출제는 Q&A가 아니라 *생성 액션*이라 grounding 분기로 포착 안 됨("출제해줘"는 검색 결과가 있어도 답이 아니라 문항 생성이 필요). 의도 판별이 불가피하며, 오분류는 RAG 폴백으로 완화.
5. **채팅 생성 문항을 draft로 DB 저장** — 엔진 현행대로. **기각**: 잡담성 출제가 대량 draft로 Review Queue 오염(Y2 정신 위반), 콘솔 검수 워크플로우와 충돌. 사용자가 ephemeral 선택.
6. **출제 전용 별도 엔드포인트(`/assessment/chat`)** — 채팅과 분리. **기각**: 사용자는 하나의 대화에서 출제·채점·일반질문을 오가길 원함. 단일 `/chat` 입구(ADR-023 §7)에 의도 분기로 흡수하는 것이 자연스럽다.

---

## Consequences

- **Positive**: 사용자가 채팅에서 자연어로 출제·채점을 받는다(ChatGPT식, 1/N/모의고사/채점 유연). 콘솔 generate 엔진 재사용으로 신규 생성 로직 0. 근거 + validator로 할루 최소화. 콘솔(폼·큐 QA)과 채팅(대화형 학습)이 역할 분리되어 충돌 없음. 이미 깔린 분류/라우팅 신호선을 실행에 연결.
- **Negative / 부채**: 의도 판별 오분류 위험(일반 질문을 출제로 오인) — 폴백으로 완화하나 모니터링 필요. 채팅 출제는 ephemeral이라 통계·재사용엔 안 잡힘(의도된 trade-off). 채점 정확도가 validator·정답 품질에 종속. generate에 `persist` 분기가 추가되어 경로가 둘로 늘어남.
- **Follow-up**:
  - `AssessmentGenerateService.generate(persist=False)` 경로 추가(테스트 포함).
  - LangGraph: `assessment_node` 추가, `assessment_intent` 분기 라우터, RAG 경로 폴백. `require_similarity_check` 신호 소비.
  - 자연어 파라미터 추출(개수·과목·난이도·모의고사) — classify/경량 추출.
  - `grounding="assessment"` 응답·로그·프론트 타입 동기화. 채점 follow-up 처리(세션 문항 참조).
  - 프론트: 채팅 문항 카드 + 채점 UX.
  - configs: 기본 개수·temperature·과목 분배 정책(`assessment.yaml`).
  - ADR-017에 채팅 assessment 응답 페이로드 추가. CLAUDE.md ADR Index 등재.
  - 모니터링: 의도 오분류율, validator 폐기율, 출제 불가(근거 없음) 비율.

---

## Assumptions

- approved 문항이 과목별로 충분해 generate가 근거를 확보한다(현재 exam-engineer approved 4,331). 비면 §4(a) 출제 거부·안내가 정상 동작.
- shared LLM(Qwen2.5-7B-AWQ)이 근거 few-shot + 낮은 temp에서 검증 통과 문항을 생성한다(콘솔에서 가동 중). 미달 시 validator가 거른다.
- 의도 판별은 경량(규칙 + 필요시 소형 LLM 분류)으로 충분하며, 애매분은 RAG 폴백으로 안전하다.
- 채점은 세션 내 생성 문항의 정답·해설을 신뢰원으로 삼는다(생성 시 validator를 통과한 정답).

가정이 깨지면(예: 생성 품질이 validator를 못 넘김, 의도 오분류 과다) 재검토 — 출제 특화 LoRA·reference 강화·의도 분류 고도화 별도 ADR.

---

## Related
- [ADR-014: Assessment Workflow](./014-assessment-workflow.md) — generate/validator 엔진 재사용·확장(채팅 entry point 추가)
- [ADR-025: Multimodal Ingestion & Figure-Reuse](./025-assessment-multimodal-ingestion.md) — figure-reuse 출제도 동일 채팅 경로에 편입 가능
- [ADR-023: Unified Conversational Pipeline](./023-unified-conversational-pipeline.md) — 의도 분기는 *액션 라우팅*으로 ADR-023의 retrieval-skip 금지와 양립, `grounding`에 `assessment` 추가
- [ADR-013: Model Routing & SLM/LLM](./013-model-routing-and-slm-llm-strategy.md) — `assessment_generation` 라우팅 룰(기존) 활용, temperature 라우팅
- [ADR-026: Assessment LLM Extraction](./026-assessment-llm-extraction.md) — 언어 드리프트 가드 재사용

---

**작성자**: AI Assistant + Project Owner
**최종 승인**: 2026-06-19
**변경 이력**: 초안 작성, 즉시 Accepted
