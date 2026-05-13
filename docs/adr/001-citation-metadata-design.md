# ADR-001: Citation 메타데이터 설계

## Status
**Accepted** (2026-05-08)

> **2026-05-08 보완 노트 (ADR-008)**: 본 ADR이 정의한 Chunk·Citation 모델은 단일 테넌트 가정. ADR-008(Multi-Tenant Architecture)에 따라 두 모델 모두에 `tenant_id: str` 필드가 1차 분기로 추가됨. chunk_id 형식은 ADR-007이 정의하며, 검색 시 collection은 `chunks_<tenant_id>`로 분리(ADR-006 부분 supersede). 필드 산출 규칙은 ADR-005가 책임.

---

## Context

### 문제
RAG 챗봇이 답변할 때, 사용자는 그 답변이 어디서 나왔는지 알아야 합니다.

- "근거: 보안 운영 매뉴얼.pdf" ← 너무 일반적
- "근거: p.12" ← 정보 부족
- "근거: 보안 운영 매뉴얼.pdf, p.12, 섹션 '반출 절차'" ← 필요한 수준

### 설명 가능한 AI의 요구사항

각 답변에 대해 추적 가능해야 함:
- ✅ 어떤 문서?
- ✅ 몇 페이지?
- ✅ 어떤 섹션?
- ✅ 어떤 chunk?
- ✅ 원문은?
- ✅ 문서 버전?
- ✅ 보안등급?
- ✅ 임베딩 모델 버전?

### 기존 방식의 문제점

```python
# ❌ 나쁜 예
citations = [
    {"doc_id": "DOC-001", "title": "안내.pdf"}
]

# 문제:
# - page_number 없음
# - section_title 없음
# - 어떤 chunk인지 모름
# - 임베딩 모델이 뭐였는지 모름
```

---

## Decision

### Chunk Metadata 설계

모든 chunk에 다음 메타데이터를 포함:

```python
@dataclass
class Chunk:
    # 식별자
    chunk_id: str                    # DOC-2026-0001:v1:chunk:0012
    doc_id: str
    doc_version: str
    chunk_index: int
    
    # 컨텐츠
    title: str                       # 원본 문서 제목
    content: str                     # 실제 텍스트
    
    # 위치 정보 (Citation에 필수)
    page_number: Optional[int]       # 12
    section_title: Optional[str]     # "반출 신청 및 승인 절차"
    heading_path: List[str]          # ["보안 정책", "자료 반출", "절차"]
    
    # 문자 위치
    start_char: int                  # 1820
    end_char: int                    # 2470
    token_count: int                 # 384
    
    # 조직/분류
    department: str                  # "security"
    doc_type: str                    # "manual"
    
    # 보안
    security_level: str              # "internal", "confidential"
    acl: List[str]                   # ["group:security"]
    
    # 버전 추적
    embedding_model: str             # "bge-m3"
    embedding_version: str           # "2026-05"
    parser_version: str              # "parser-v1"
    chunk_strategy: str              # "semantic-v1"
    
    # 검색 결과 (런타임)
    score: Optional[float]           # Qdrant 유사도
    rerank_score: Optional[float]    # Reranker 점수
```

### Citation 데이터 모델

```python
@dataclass
class Citation:
    # 식별자
    citation_id: str                 # cite-001
    marker: str                      # "[1]"
    
    # 근거 chunk 정보
    chunk_id: str
    doc_id: str
    
    # 사용자에게 표시할 정보
    title: str                       # 문서명
    page_number: Optional[int]       # 페이지
    section_title: Optional[str]     # 섹션
    excerpt: str                     # 원문 발췌
    
    # 신뢰도
    score: float                     # 검색 유사도 (0-1)
    rerank_score: float              # Rerank 점수 (0-1)
    support_level: str               # "weak" / "medium" / "strong"
    verified: bool                   # Citation 검증 완료?
```

### 프론트엔드 표시

```
사용자 답변: "내부망 반출은 신청서 작성, 부서장 승인, 보안부서 검토 후 가능하다고 명시되어 있습니다. [1]"

근거 문서:
[1] 보안 운영 매뉴얼.pdf 
    페이지: 12
    섹션: 반출 신청 및 승인 절차
    
    원문 발췌:
    "내부망 자료 반출은 신청서 작성, 부서장 승인, 
     보안부서 검토 후 가능하다. ..."
     
    [유사도: 82%] [재정렬 점수: 91%]
```

---

## Consequences

### ✅ 긍정적 영향

1. **설명 가능성**: 모든 답변에서 정확한 근거 추적 가능
2. **감사(Audit)**: 어떤 모델/버전으로 답변했는지 기록
3. **품질 개선**: 점수 기반으로 저품질 답변 식별
4. **유지보수**: 버전 업그레이드 시 영향 범위 명확
5. **사용자 신뢰**: 근거를 직접 확인 가능

### ⚠️ 부정적 영향

1. **저장 공간 증가**: chunk 메타데이터 크기 증가
   - 해결: Qdrant는 효율적으로 처리, 문제 없음
2. **인덱싱 복잡성**: 더 많은 메타데이터 추출 필요
   - 해결: Parser/Chunker에서 자동 생성

### 트레이드오프

| 항목 | 선택 | 이유 |
|-----|-----|-----|
| 메타데이터 크기 | 큼 | 추적 가능성이 비용보다 중요 |
| 메타데이터 구조 | 계층적 | 유연성과 확장성 필요 |
| Citation 검증 | 필수 | 거짓 citation 방지 필수 |

---

## Alternatives Considered

### 1. Citation을 문서명만 저장
```python
# ❌ 검토하지 않음
citations = [{"title": "보안 운영 매뉴얼.pdf"}]
```
**문제**: page_number, section이 없어서 사용자가 찾기 어려움

### 2. Citation을 검색 시점에 생성
```python
# ❌ 검토하지 않음
def generate_citations_at_query_time(answer, chunks):
    # 런타임에 citation 생성
    ...
```
**문제**: 저장된 로그에서 재현 불가 (버전 변경 시)

### 3. 모든 chunk 메타데이터를 저장하지 말고 필요한 것만
```python
# ❌ 검토하지 않음
chunk = {
    "id": "chunk-123",
    "content": "...",
    "page": 12
}
```
**문제**: 나중에 추가 정보가 필요하면 재인덱싱 필요

### 4. ✅ 현재 선택: 풍부한 메타데이터 포함 (모든 필요한 정보 저장)
- 초기 비용: 높음
- 유지보수 비용: 낮음
- 확장성: 높음

---

## Related

- [ADR-002: Protocol/Adapter 패턴](./002-protocol-adapter-pattern.md) - 메타데이터 생성 방식
- [ADR-003: LangGraph 오케스트레이션](./003-langraph-orchestration.md) - Citation 검증 로직
- [ARCHITECTURE.md](../ARCHITECTURE.md#metadata-design) - 메타데이터 전체 설계
- [IMPLEMENTATION_SPEC.md](../../IMPLEMENTATION_SPEC.md#6-메타데이터-설계) - 메타데이터 명세

---

## 구현 체크리스트

- [ ] Chunk 모델 정의 (rag_core/models/chunk.py)
- [ ] Citation 모델 정의 (rag_core/models/citation.py)
- [ ] Parser들이 page_number, section_title 추출
- [ ] Chunker가 heading_path 생성
- [ ] Metadata 생성 유틸리티 (metadata_builder.py)
- [ ] Citation 생성 로직 (citation_service.py)
- [ ] Citation 검증 로직 (verify_citations node)
- [ ] 프론트엔드 Citation Panel 구현
- [ ] 로그에 모든 메타데이터 저장

---

**작성자**: AI Assistant  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성
