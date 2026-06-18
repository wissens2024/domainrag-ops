# RAG Core

DomainRAG Ops의 핵심 라이브러리 - Protocol 기반 플러그인 아키텍처

## 개요

**rag_core**는 RAG (Retrieval-Augmented Generation) 시스템의 모든 컴포넌트를 Protocol 인터페이스로 정의하고, 구체적 구현체들을 제공하는 패키지입니다.

### 핵심 원칙

```
1. Protocol/Interface 기반 설계 (구현체 교체 가능)
2. Dependency Injection으로 느슨한 결합
3. 모든 컴포넌트는 독립적으로 테스트 가능
4. LangGraph 워크플로우로 오케스트레이션
```

---

## 프로젝트 구조

```
packages/rag_core/
├── README.md                      # 이 파일
├── pyproject.toml                 # Poetry 설정
├── poetry.lock
├── requirements.txt
│
└── rag_core/
    ├── __init__.py
    │
    ├── interfaces/                # Protocol 인터페이스
    │   ├── __init__.py
    │   ├── llm_client.py          # LLM 인터페이스
    │   ├── embedder.py            # Embedding 인터페이스
    │   ├── retriever.py           # Retriever 인터페이스
    │   ├── reranker.py            # Reranker 인터페이스
    │   ├── vector_store.py        # Vector DB 인터페이스
    │   ├── document_parser.py     # Document Parser 인터페이스
    │   └── chunker.py             # Chunker 인터페이스
    │
    ├── implementations/           # 구체적 구현체
    │   ├── __init__.py
    │   │
    │   ├── embedders/
    │   │   ├── __init__.py
    │   │   ├── sentence_transformers_embedder.py  # BGE, E5 등
    │   │   └── mock_embedder.py   # 테스트용
    │   │
    │   ├── retrievers/
    │   │   ├── __init__.py
    │   │   ├── qdrant_retriever.py
    │   │   └── mock_retriever.py
    │   │
    │   ├── rerankers/
    │   │   ├── __init__.py
    │   │   ├── cross_encoder_reranker.py  # BGE Reranker
    │   │   └── mock_reranker.py
    │   │
    │   ├── vector_stores/
    │   │   ├── __init__.py
    │   │   ├── qdrant_vector_store.py
    │   │   └── mock_vector_store.py
    │   │
    │   ├── parsers/
    │   │   ├── __init__.py
    │   │   ├── pdf_parser.py
    │   │   ├── docx_parser.py
    │   │   ├── txt_parser.py
    │   │   └── mock_parser.py
    │   │
    │   ├── chunkers/
    │   │   ├── __init__.py
    │   │   ├── semantic_chunker.py
    │   │   ├── fixed_size_chunker.py
    │   │   └── mock_chunker.py
    │   │
    │   └── llm_clients/
    │       ├── __init__.py
    │       ├── vllm_client.py     # OpenAI compatible
    │       ├── openai_client.py   # 폐쇄망에서는 사용 X
    │       └── mock_llm.py
    │
    ├── workflows/                 # LangGraph 워크플로우
    │   ├── __init__.py
    │   ├── rag_graph.py           # 메인 RAG 워크플로우
    │   ├── states.py              # 상태 정의
    │   ├── nodes/
    │   │   ├── __init__.py
    │   │   ├── classify_query.py
    │   │   ├── retrieve_context.py
    │   │   ├── rerank_context.py
    │   │   ├── generate_answer.py
    │   │   ├── verify_citations.py
    │   │   └── fallback.py
    │   └── conditions/
    │       ├── __init__.py
    │       └── routing_conditions.py
    │
    ├── utils/
    │   ├── __init__.py
    │   ├── text_processors.py     # 텍스트 전처리
    │   ├── metadata_builder.py    # 메타데이터 생성
    │   ├── citation_extractor.py  # Citation 마커 추출
    │   └── acl_filter_builder.py  # ACL 필터 빌드
    │
    ├── models/
    │   ├── __init__.py
    │   ├── chunk.py               # Chunk 데이터 모델
    │   ├── citation.py            # Citation 데이터 모델
    │   ├── user_context.py        # UserContext 모델
    │   └── document.py            # Document 메타데이터
    │
    └── exceptions.py              # 커스텀 예외
```

---

## Core Interfaces

### 1. LLMClient

LLM (Language Model) 인터페이스:

```python
from typing import Protocol

class LLMClient(Protocol):
    """LLM 호출을 위한 인터페이스"""
    
    def generate(
        self,
        prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> str:
        """
        프롬프트를 입력받아 텍스트를 생성한다.
        
        Args:
            prompt: 입력 프롬프트
            max_tokens: 최대 생성 토큰 수
            temperature: 샘플링 온도 (0-1)
            
        Returns:
            생성된 텍스트
            
        Raises:
            LLMError: LLM 호출 실패
        """
        ...
```

**구현체:**
- `VLLMClient` - vLLM OpenAI-compatible endpoint
- `OpenAIClient` - OpenAI API (폐쇄망에서 사용 불가)
- `MockLLM` - 테스트용

---

### 2. Embedder

임베딩 인터페이스:

```python
class Embedder(Protocol):
    """텍스트를 벡터로 변환하는 인터페이스"""
    
    def embed(self, text: str) -> List[float]:
        """
        단일 텍스트를 임베딩한다.
        
        Returns:
            임베딩 벡터 (예: 768차원)
        """
        ...
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """배치 임베딩 (성능 최적화)"""
        ...
```

**구현체:**
- `SentenceTransformersEmbedder` - BGE-M3, E5 등
- `MockEmbedder` - 테스트용

---

### 3. VectorStore

벡터 데이터베이스 인터페이스:

```python
class VectorStore(Protocol):
    """벡터 검색을 위한 인터페이스"""
    
    def search(
        self,
        query_vector: List[float],
        top_k: int = 10,
        filter: Optional[Dict] = None,
    ) -> List[Chunk]:
        """
        유사 벡터를 검색한다.
        
        Args:
            query_vector: 쿼리 임베딩
            top_k: 반환할 상위 k개
            filter: Qdrant payload filter
            
        Returns:
            유사 Chunk 리스트
        """
        ...
    
    def add(self, chunks: List[Chunk], vectors: List[List[float]]):
        """Chunk와 벡터를 저장한다."""
        ...
```

**구현체:**
- `QdrantVectorStore` - Qdrant 벡터 DB
- `MockVectorStore` - 테스트용

---

### 4. Reranker

재정렬 인터페이스:

```python
class Reranker(Protocol):
    """후보 문서를 재정렬하는 인터페이스"""
    
    def rerank(
        self,
        query: str,
        candidates: List[Chunk],
    ) -> List[Tuple[Chunk, float]]:
        """
        쿼리와의 관련도로 후보를 재정렬한다.
        
        Returns:
            (Chunk, rerank_score) 튜플 리스트 (점수 내림차순)
        """
        ...
```

**구현체:**
- `CrossEncoderReranker` - BGE Reranker
- `MockReranker` - 테스트용

---

### 5. DocumentParser

문서 파싱 인터페이스:

```python
class DocumentParser(Protocol):
    """문서를 파싱하는 인터페이스"""
    
    def parse(self, file_path: str) -> ParsedDocument:
        """
        파일을 파싱하여 텍스트와 메타데이터를 추출한다.
        
        Returns:
            ParsedDocument {
                text: str,
                pages: List[Page],
                metadata: Dict
            }
        """
        ...
```

**구현체:**
- `PDFParser` - PDF 파일
- `DOCXParser` - Word 문서
- `TXTParser` - 텍스트 파일

---

### 6. Chunker

청킹 인터페이스:

```python
class Chunker(Protocol):
    """문서를 chunk로 분할하는 인터페이스"""
    
    def chunk(
        self,
        text: str,
        metadata: DocumentMetadata,
        parsed_pages: Optional[List[Page]] = None,
    ) -> List[Chunk]:
        """
        텍스트를 의미 있는 chunk로 분할한다.
        
        Returns:
            Chunk 리스트 (각각 page_number, section_title 포함)
        """
        ...
```

**구현체:**
- `SemanticChunker` - 의미 기반 분할
- `FixedSizeChunker` - 고정 크기 분할

---

## Core Models

### Chunk

```python
@dataclass
class Chunk:
    """개별 문서 조각"""
    chunk_id: str                    # DOC-ID:v:chunk:index
    doc_id: str
    doc_version: str
    chunk_index: int
    
    title: str                       # 원본 문서 제목
    content: str                     # 실제 텍스트
    
    page_number: Optional[int]
    section_title: Optional[str]
    heading_path: List[str]          # ["보안 정책", "자료 반출", "..."]
    
    start_char: int                  # 원문에서의 위치
    end_char: int
    token_count: int
    
    # 메타데이터
    department: str
    doc_type: str
    security_level: str              # public, internal, confidential
    acl: List[str]                   # ["group:security", "user:admin"]
    tags: List[str]
    
    # Versioning
    embedding_model: str
    embedding_version: str
    parser_version: str
    chunk_strategy: str
    
    # Vector store
    vector_id: Optional[str]         # Qdrant point ID
    vector: Optional[List[float]]    # 임베딩 벡터 (선택)
    
    # 시간
    created_at: datetime
```

### Citation

```python
@dataclass
class Citation:
    """답변의 근거 문서"""
    citation_id: str
    marker: str                      # "[1]", "[2]"
    
    chunk_id: str
    doc_id: str
    title: str
    
    page_number: Optional[int]
    section_title: Optional[str]
    
    excerpt: str                     # 원문 발췌
    
    score: float                     # 검색 유사도
    rerank_score: float              # Rerank 점수
    
    support_level: str               # weak, medium, strong
    verified: bool
```

### UserContext

```python
@dataclass
class UserContext:
    """사용자 권한 정보"""
    user_id: str
    groups: List[str]                # ["security", "it-admin"]
    clearance: str                   # public, internal, confidential
    
    def can_access(self, chunk: Chunk) -> bool:
        """Chunk 접근 권한 확인"""
        # 1. 보안등급 체크
        if chunk.security_level > self.clearance:
            return False
        # 2. ACL 체크
        if chunk.acl:
            has_access = (self.user_id in chunk.acl or
                         any(f"group:{g}" in chunk.acl for g in self.groups))
            if not has_access:
                return False
        return True
```

---

## Workflows (LangGraph)

### RAG Graph

메인 RAG 워크플로우:

```python
class RAGGraph:
    """
    사용자 질문 → 답변 + Citation 생성 워크플로우
    
    Flow:
    1. classify_query - 질문 유형 분류
    2. load_user_policy - 사용자 권한 로드
    3. build_acl_filter - ACL 필터 생성
    4. retrieve_context - 문서 검색
    5. rerank_context - 재정렬
    6. generate_answer - 답변 생성
    7. verify_citations - Citation 검증
    8. save_chat_log - 로그 저장
    9. fallback - 근거 부족 시 폴백
    """
```

**상태 (State):**

```python
class RAGState(TypedDict, total=False):
    # 입력
    request_id: str
    user_id: str
    question: str
    
    # 권한
    user_groups: List[str]
    user_clearance: str
    
    # 처리 중간 결과
    retrieved_chunks: List[Chunk]
    reranked_chunks: List[Chunk]
    final_contexts: List[Chunk]
    
    # 출력
    draft_answer: str
    final_answer: str
    citations: List[Citation]
    
    # 메타데이터
    llm_model: str
    embedding_model: str
    reranker_model: str
    latency_ms: int
```

---

## Utils

### CitationExtractor

LLM 답변에서 citation marker 추출:

```python
def extract_citations(answer: str) -> List[str]:
    """
    답변에서 [1], [2] 같은 marker를 추출한다.
    
    Returns:
        ["[1]", "[2]"]
    """
    ...
```

### ACLFilterBuilder

ACL 필터 빌드:

```python
def build_acl_filter(user_context: UserContext) -> Dict:
    """
    Qdrant payload filter 생성
    
    조건:
    1. chunk.acl에 user_id 또는 group 포함
    2. chunk.security_level <= user.clearance
    3. chunk.valid_from <= today
    4. chunk.valid_until >= today 또는 null
    """
    ...
```

### MetadataBuilder

Chunk 메타데이터 생성:

```python
def build_chunk_metadata(
    parsed_page: Page,
    document: Document,
    chunk_index: int,
    embedding_model: str,
) -> Dict:
    """
    Page 정보와 Document 메타데이터에서
    Chunk 메타데이터를 생성한다.
    """
    ...
```

---

## 사용 예시

### 예 1: 간단한 검색

```python
from rag_core.implementations import (
    SentenceTransformersEmbedder,
    QdrantVectorStore
)
from rag_core.utils import build_acl_filter

# 초기화
embedder = SentenceTransformersEmbedder("bge-m3")
vector_store = QdrantVectorStore("http://qdrant:6333")

# 검색
query = "내부망 반출 절차"
query_vector = embedder.embed(query)

user_context = UserContext(
    user_id="user-001",
    groups=["security"],
    clearance="internal"
)

acl_filter = build_acl_filter(user_context)
results = vector_store.search(query_vector, filter=acl_filter, top_k=10)

# 결과
for chunk in results:
    print(f"{chunk.title} (p.{chunk.page_number}): {chunk.content[:100]}")
```

### 예 2: 전체 RAG 파이프라인 (LangGraph)

```python
from rag_core.workflows import RAGGraph
from rag_core.implementations import (
    VLLMClient,
    SentenceTransformersEmbedder,
    QdrantVectorStore,
    CrossEncoderReranker,
)

# 컴포넌트 초기화
llm = VLLMClient("http://vllm:8000/v1", "qwen2.5-7b-awq")
embedder = SentenceTransformersEmbedder("bge-m3")
vector_store = QdrantVectorStore("http://qdrant:6333")
reranker = CrossEncoderReranker("bge-reranker-v2-m3")

# RAG Graph 생성
rag_graph = RAGGraph(
    llm=llm,
    embedder=embedder,
    vector_store=vector_store,
    reranker=reranker
)

# 실행
response = await rag_graph.process(
    question="내부망 반출 절차는?",
    user_id="user-001"
)

print(f"답변: {response.final_answer}")
for citation in response.citations:
    print(f"  [{citation.marker}] {citation.title} p.{citation.page_number}")
```

---

## 테스트

### Unit 테스트

```bash
pytest -v

# 특정 모듈만 테스트
pytest tests/test_embedders.py -v

# 커버리지
pytest --cov=rag_core tests/
```

**테스트 예시:**

```python
def test_chunk_acl_filtering():
    """Chunk ACL이 정상 작동하는지 확인"""
    chunk = Chunk(
        chunk_id="...",
        acl=["group:security"],
        security_level="internal"
    )
    
    # 권한 있는 사용자
    user = UserContext(
        user_id="user-001",
        groups=["security"],
        clearance="internal"
    )
    assert user.can_access(chunk)
    
    # 권한 없는 사용자
    user2 = UserContext(
        user_id="user-002",
        groups=["sales"],
        clearance="public"
    )
    assert not user2.can_access(chunk)
```

---

## 의존성

주요 라이브러리:

```toml
[dependencies]
# Core
pydantic = "^2.0"
langchain = "^0.1"
langgraph = "^0.0"

# Embedders & Rerankers
sentence-transformers = "^2.2"

# Vector Store
qdrant-client = "^2.3"

# Document Parsing
PyPDF2 = "^3.0"
python-docx = "^0.8"

# LLM
openai = "^1.0"

# Async
httpx = "^0.24"
```

---

## 확장 가능성

### 새로운 Embedder 추가

```python
from rag_core.interfaces import Embedder

class CustomEmbedder:
    """커스텀 임베딩 모델"""
    
    def embed(self, text: str) -> List[float]:
        # 구현
        ...
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        # 구현
        ...

# 사용
embedder = CustomEmbedder()
```

### 새로운 VectorStore 추가

```python
from rag_core.interfaces import VectorStore

class MilvusVectorStore:
    """Milvus Vector DB"""
    
    def search(self, query_vector, top_k, filter=None):
        # Milvus 검색 로직
        ...
    
    def add(self, chunks, vectors):
        # Milvus에 저장
        ...
```

---

## 구현 상태

- [ ] Interfaces 정의
  - [ ] LLMClient
  - [ ] Embedder
  - [ ] VectorStore
  - [ ] Reranker
  - [ ] DocumentParser
  - [ ] Chunker
- [ ] Implementations
  - [ ] VLLMClient
  - [ ] SentenceTransformersEmbedder
  - [ ] QdrantVectorStore
  - [ ] CrossEncoderReranker
  - [ ] PDFParser, DOCXParser, TXTParser
  - [ ] SemanticChunker
- [ ] Core Models
  - [ ] Chunk, Citation, UserContext
- [ ] Workflows
  - [ ] RAGGraph 정의
  - [ ] Node 구현
- [ ] Utils
  - [ ] CitationExtractor
  - [ ] ACLFilterBuilder
  - [ ] MetadataBuilder
- [ ] Tests
  - [ ] Unit tests
  - [ ] Integration tests

---

## 참고 문서

- [CLAUDE.md](../../CLAUDE.md)
- [IMPLEMENTATION_SPEC.md](../../IMPLEMENTATION_SPEC.md)
