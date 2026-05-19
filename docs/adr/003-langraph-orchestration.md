# ADR-003: LangGraph 오케스트레이션

## Status
**Accepted** (2026-05-08)

> **2026-05-08 보완 노트 (ADR-005에 의해 강화)**:
> 본 ADR §Decision의 `should_generate` 조건 (`len(reranked_chunks) > 0`)은 단일 약한 게이트였으며, **ADR-005 Gate 1 정량 조건**으로 대체된다 — `rerank_score > 0.5인 chunk 수 < 2 OR top1 rerank_score < 0.6 → fallback (reason="low_retrieval")`. 또한 verify_citations 노드는 ADR-005에 의해 `parse_response → verify_tier1 → verify_tier2 → detect_unsupported → assemble_response → compute_confidence`로 분해되며, generate 후 Gate 2가 추가된다 (`verified=0 OR unsupported_ratio>0.5 OR confidence<0.5`). 본 ADR은 LangGraph 채택 결정 자체로서는 유효하므로 Status는 Accepted 유지. 노드/엣지 사양의 단일 진실 소스는 [ADR-013 §9 LangGraph 흐름](./013-model-routing-and-slm-llm-strategy.md)이다 (SPEC.md §10.2는 폐기 후 ADR-013으로 흡수).

> **2026-05-08 보완 노트 (ADR-008)**: 본 ADR의 `RAGState`에 `tenant_id: str` 필수 필드가 추가되며, LangGraph 진입점 직후에 신규 노드 **`tenant_resolver`**가 실행된다. 이 노드는 JWT claim에서 tenant_id 추출, URL path와 일치 검증(mismatch 시 403), PostgreSQL `SET LOCAL app.current_tenant`, Qdrant collection 이름(`chunks_<tenant_id>`) 결정을 책임진다. 모든 후속 노드는 `state.tenant_id`로 격리된 리소스에 접근. 그래프 구조 자체는 본 ADR 그대로.

---

## Context

### 문제: RAG 워크플로우 관리

RAG 시스템의 처리 순서:

```
1. 질문 받기
   ↓
2. 사용자 권한 확인
   ↓
3. 질문 분류
   ↓
4. 문서 검색 (ACL 필터)
   ↓
5. Reranking
   ↓
6. LLM 답변 생성
   ↓
7. Citation 검증
   ↓
8. 로그 저장
   ↓
9. 답변 반환
```

### 기존 방식의 문제

#### ❌ 방식 1: 순차적 함수 호출

```python
def chat(question, user_id):
    # 모든 로직이 한 함수에...
    user = load_user(user_id)
    query_type = classify(question)
    chunks = retrieve(question, user.acl)
    reranked = rerank(question, chunks)
    answer = generate(question, reranked)
    citations = verify_citations(answer, reranked)
    save_log(question, answer, citations)
    return answer
```

**문제**:
- 함수가 길어서 이해하기 어려움
- 각 단계의 상태 추적 어려움
- 에러 처리 복잡
- 각 단계를 독립적으로 테스트 불가
- 조건부 분기 (if/else) 많음

#### ❌ 방식 2: Celery 기반 비동기 태스크

```python
@task
def process_question(question, user_id):
    try:
        user = load_user.delay(user_id).get()
        chunks = retrieve.delay(question).get()
        # ...
    except Exception as e:
        log_error(e)
```

**문제**:
- 메시지 큐 복잡성
- 트랜잭션 관리 어려움
- 실패 복구 복잡
- 로컬 테스트 어려움

---

## Decision

### LangGraph 사용

LangGraph는 LangChain에서 제공하는 상태 머신 기반 워크플로우 프레임워크:

```python
from langgraph.graph import StateGraph
from typing import TypedDict, List

# 1. 상태 정의
class RAGState(TypedDict):
    request_id: str
    user_id: str
    question: str
    
    user_context: UserContext
    retrieved_chunks: List[Chunk]
    reranked_chunks: List[Chunk]
    
    answer: str
    citations: List[Citation]
    
    error: Optional[str]

# 2. Node 정의 (각 단계)
def load_user_node(state: RAGState) -> RAGState:
    """사용자 컨텍스트 로드"""
    user_context = load_user_context(state["user_id"])
    state["user_context"] = user_context
    return state

def retrieve_node(state: RAGState) -> RAGState:
    """문서 검색"""
    chunks = retrieve_with_acl(
        state["question"],
        state["user_context"]
    )
    state["retrieved_chunks"] = chunks
    return state

def rerank_node(state: RAGState) -> RAGState:
    """Reranking"""
    reranked = rerank(
        state["question"],
        state["retrieved_chunks"]
    )
    state["reranked_chunks"] = reranked
    return state

# ... 기타 nodes

# 3. 조건부 라우팅 (선택)
def should_generate(state: RAGState) -> str:
    """근거가 충분한지 확인"""
    if len(state["reranked_chunks"]) > 0:
        return "generate_answer"
    else:
        return "fallback"

# 4. 그래프 구성
graph = StateGraph(RAGState)
graph.add_node("load_user", load_user_node)
graph.add_node("retrieve", retrieve_node)
graph.add_node("rerank", rerank_node)
graph.add_node("generate", generate_node)
graph.add_node("verify", verify_citations_node)
graph.add_node("save_log", save_log_node)
graph.add_node("fallback", fallback_node)

# 엣지 (순서)
graph.add_edge("load_user", "retrieve")
graph.add_edge("retrieve", "rerank")
graph.add_edge("rerank", "generate")
graph.add_conditional_edges("rerank", should_generate)
graph.add_edge("generate", "verify")
graph.add_edge("verify", "save_log")

# 시작/종료
graph.set_entry_point("load_user")
graph.set_finish_point("save_log")

# 5. 실행
runnable = graph.compile()
result = runnable.invoke({
    "request_id": "REQ-001",
    "user_id": "user-001",
    "question": "내부망 반출 절차?"
})
```

### Node 설계 원칙

각 Node는 얇게 유지:

```python
# ❌ Node에 모든 로직
def retrieve_node(state):
    chunks = retrieve(state["question"])
    reranked = rerank(state["question"], chunks)  # X
    filtered = filter_by_acl(reranked, state["user"])  # X
    return state

# ✅ Node는 얇게, Service가 수행
class RetrievalService:
    def retrieve_and_rerank(self, query, user_context):
        chunks = self.retrieve(query, user_context)
        reranked = self.reranker.rerank(query, chunks)
        return reranked

def retrieve_node(state):
    service = RetrievalService(...)
    state["reranked_chunks"] = service.retrieve_and_rerank(
        state["question"],
        state["user_context"]
    )
    return state
```

### 에러 처리

```python
def handle_error(state: RAGState, error: Exception) -> RAGState:
    """에러 처리"""
    logger.error(f"RAG error: {error}")
    state["error"] = str(error)
    
    # 부분 결과 반환
    if state.get("reranked_chunks"):
        return generate_fallback_answer(state)
    else:
        return {"answer": "죄송합니다. 답변을 생성할 수 없습니다."}
```

---

## Consequences

### ✅ 긍정적 영향

1. **명확성**: 각 단계가 명확하게 분리
2. **유지보수성**: 단계별 수정이 용이
3. **테스트 용이성**: 각 node를 독립적으로 테스트
4. **상태 추적**: 모든 상태가 명확
5. **조건부 분기**: 깔끔한 라우팅
6. **시각화**: 워크플로우 그래프 생성 가능
7. **에러 추적**: 어느 단계에서 실패했는지 명확

### ⚠️ 주의사항

1. **학습 곡선**: LangGraph 문법 학습 필요
   - 해결: 문서 참고, 예제로 시작
2. **성능 오버헤드**: 상태 관리 비용
   - 해결: 미미함 (ms 단위), 무시 가능

---

## Alternatives Considered

### 1. Airflow
```
❌ 너무 무거움 (데이터 파이프라인용)
- 스케일링 과복잡
- 폐쇄망 환경에서 설정 복잡
```

### 2. Temporal / Durable Functions
```
❌ 장기 실행 워크플로우용
- RAG는 단기 (수초)이므로 오버킬
```

### 3. Pydantic Workflow (커스텀)
```
~ 가능하지만 재발명
- LangGraph가 더 나음
```

### 4. ✅ 현재 선택: LangGraph
- 간단하고 직관적
- 상태 관리 자동화
- LangChain 생태계 통합

---

## 구현 로드맵

### Phase 1: 기본 구조
- [ ] RAGState 정의
- [ ] 각 Node 구현
- [ ] 그래프 구성
- [ ] 기본 테스트

### Phase 2: 에러 처리
- [ ] 예외 처리 추가
- [ ] 폴백 로직
- [ ] 재시도 로직

### Phase 3: 모니터링
- [ ] 로깅
- [ ] 메트릭 수집
- [ ] 워크플로우 시각화

### Phase 4: 최적화
- [ ] 병렬화 (retrieve + rerank 동시 처리)
- [ ] 캐싱
- [ ] 성능 프로파일링

---

## Related

- [ADR-002: Protocol/Adapter 패턴](./002-protocol-adapter-pattern.md) - Node가 Protocol 기반 컴포넌트 사용
- [ADR-001: Citation 메타데이터](./001-citation-metadata-design.md) - 상태에 포함되는 데이터
- IMPLEMENTATION_SPEC.md §10 LangGraph workflow — 폐기 후 [ADR-013 §9](./013-model-routing-and-slm-llm-strategy.md)로 흡수

---

**작성자**: AI Assistant  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성
