# ADR-028: 대화 기억(Conversational Memory) — ungrounded 후속 답변에 대화 이력 주입

Status: Accepted
Date: 2026-06-19
Deciders: wissens2024, Claude

## Context

ADR-023은 "항상 검색→근거 유무로 grounded/ungrounded 자동 분기"하는 단일 대화 파이프라인을
정의했으나, 각 턴을 **독립(stateless)** 으로 처리한다 — LLM에 직전 대화가 전달되지 않는다.
그 결과 후속 질문이 깨진다: 채팅 출제(ADR-027) 후 "위 문제를 자세히 설명해줘"라고 하면,
"위 문제"가 직전 출제 문항을 가리킨다는 걸 모른 채 새 질문으로 RAG 검색→문서 없음→
ungrounded 보일러플레이트("질문이 주어졌습니다 / 근거 못 찾음...")로 떨어진다. 사용자는
ChatGPT식 멀티턴(이전 답을 이어 설명·채점·재질문)을 기대한다. ADR-027로 assessment_items가
chat_logs에 보존되므로(routing_decision JSONB) 직전 문항을 대화에서 복원할 수 있게 되었다.

## Decision

ungrounded 대화 생성(`generate_conversational`)에 **최근 대화 이력 N턴을 맥락으로 주입**한다.
RAGService가 `conversation_id`로 chat_logs에서 직전 turn들(question·answer)을 읽어 RAGState에
싣고, ungrounded 생성 노드가 이를 프롬프트의 선행 대화로 전달한다. grounded(인용) 생성 경로는
구조화 출력·인용 무결성 리스크가 있어 **이번 결정 범위에서 제외**한다(후속에서 재검토).
이력 주입은 N=최근 6메시지(약 3교환)로 제한하고, ungrounded이므로 인용 책임은 없다(ADR-023 §4).

## Considered Alternatives

1. **표적형(설명/채점 의도 전용)** — "설명/풀이/채점/왜" 의도일 때만 직전 출제 문항을 불러와
   답하게. — **기각**: 의도 분류에 의존해 깨지기 쉽고, "그럼 B는?", "더 쉽게" 같은 자유 후속을
   못 받는다. 사용자가 기대하는 범용 멀티턴이 아니다.
2. **전 경로(grounded 포함) 이력 주입** — 모든 생성에 대화 이력 전달. — **기각(이번엔)**:
   grounded는 guided JSON·claim-level citation을 쓰는데 이력이 인용 마커를 오염시키거나 환각
   인용을 유발할 수 있다(절대원칙 4·5). blast radius가 커 단계적 도입이 안전하다.
3. **프론트가 이력을 요청에 동봉** — 클라이언트가 최근 메시지를 보냄. — **기각**: 신뢰 불가
   (클라이언트가 맥락을 조작 가능), chat_logs(audit truth)와 이중 소스. 서버가 RLS로 읽는 게 정합.

## Consequences

- Positive: "위 문제 설명해줘", "왜 답이 X야", "내 답 채점해줘" 등 ungrounded 후속이 자연스럽게
  동작. ADR-027 채팅 출제의 튜터링/채점 흐름이 완성된다.
- Negative: ungrounded 답변 토큰↑(이력 만큼), chat_logs 1회 추가 read(턴당). grounded 후속은
  아직 맥락 미반영(후속 ADR 필요).
- Follow-up: 이력 주입 후 환각 모니터링(ungrounded이라 인용은 없으나 사실성 caveat 유지).
  grounded 경로 이력 주입은 별도 ADR로 검토.

## Assumptions

- 후속 설명/채점 질문은 등록 문서에 근거가 없어 ungrounded로 분기된다(시험 문항은 문서가 아님).
- chat_logs.answer가 직전 문항의 전체 텍스트(질문·보기·정답·해설)를 보존한다(ADR-027 §6).
- 대화당 turn 수가 수십 이내라 최근 N턴 read가 저비용이다.
