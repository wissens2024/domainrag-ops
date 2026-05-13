# ADR-002: Protocol/Adapter 패턴

## Status
**Accepted** (2026-05-08)

---

## Context

### 문제: 강한 결합도

RAG 시스템에서 사용되는 여러 컴포넌트들:
- LLM (vLLM, OpenAI, Claude API, 로컬 모델 등)
- Embedding (BGE, E5, OpenAI, 커스텀 등)
- Vector Store (Qdrant, Milvus, Weaviate 등)
- Reranker (BGE Reranker, 커스텀 등)
- Document Parser (PDF, DOCX, HWP, 이미지 등)

만약 코드에 직접 import한다면:

```python
# ❌ 나쁜 방식: 강한 결합
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer
from openai import OpenAI

def chat(question):
    # 1. Embedding 생성
    embedder = SentenceTransformer("bge-m3")
    query_vector = embedder.encode(question)
    
    # 2. 검색
    qdrant = QdrantClient("http://qdrant:6333")
    results = qdrant.search(...)
    
    # 3. LLM 호출
    client = OpenAI(api_key="sk-...")
    response = client.chat.completions.create(...)
```

**문제**:
- 구현체 변경 시 코드 수정 필요
- 테스트할 때 실제 서비스에 의존
- 폐쇄망 vs 클라우드 환경 분리 어려움
- 배포 환경마다 코드 변경 필요

### SOLID 원칙 위반

- 단일 책임 원칙 (SRP) 위반: 구현체 선택과 비즈니스 로직 혼재
- 개방/폐쇄 원칙 (OCP) 위반: 새 구현체 추가 시 기존 코드 수정
- 의존성 역전 원칙 (DIP) 위반: 구체적 구현에 의존

---

## Decision

### Protocol 인터페이스 정의

Python의 `Protocol` (또는 `ABC`)을 사용해 인터페이스 정의:

```python
from typing import Protocol, List

# 인터페이스 정의
class LLMClient(Protocol):
    """LLM 호출을 위한 인터페이스"""
    
    def generate(self, prompt: str, max_tokens: int = 2048) -> str:
        """프롬프트를 입력받아 텍스트를 생성한다."""
        ...

class Embedder(Protocol):
    """텍스트를 벡터로 변환하는 인터페이스"""
    
    def embed(self, text: str) -> List[float]:
        """단일 텍스트를 임베딩한다."""
        ...
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """배치 임베딩"""
        ...

class VectorStore(Protocol):
    """벡터 데이터베이스 인터페이스"""
    
    def search(self, query_vector: List[float], top_k: int) -> List[Dict]:
        """벡터 검색"""
        ...
    
    def add(self, vectors: List[List[float]], metadata: List[Dict]):
        """벡터 저장"""
        ...

# ... 기타 인터페이스들
```

### 구현체 (Adapter)

각 인터페이스에 대해 구현체를 분리:

```python
# VLLMClient - 로컬 vLLM 서빙
class VLLMClient:
    def __init__(self, base_url: str, model_name: str):
        self.client = OpenAI(api_key="not-used", base_url=base_url)
        self.model_name = model_name
    
    def generate(self, prompt: str, max_tokens: int = 2048) -> str:
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens
        )
        return response.choices[0].message.content

# QdrantVectorStore - Qdrant 구현
class QdrantVectorStore:
    def __init__(self, url: str):
        self.client = QdrantClient(url=url)
    
    def search(self, query_vector, top_k):
        results = self.client.search(...)
        return results
    
    def add(self, vectors, metadata):
        self.client.upsert(...)

# SentenceTransformersEmbedder - SBERT 기반
class SentenceTransformersEmbedder:
    def __init__(self, model_name: str):
        self.model = SentenceTransformer(model_name)
    
    def embed(self, text: str):
        return self.model.encode(text).tolist()
    
    def embed_batch(self, texts):
        return self.model.encode(texts).tolist()
```

### 사용 (Dependency Injection)

```python
# ✅ 좋은 방식: Protocol 기반

def create_rag_service(
    llm: LLMClient,
    embedder: Embedder,
    vector_store: VectorStore,
    reranker: Reranker
) -> RAGService:
    """구현체는 외부에서 주입"""
    return RAGService(llm, embedder, vector_store, reranker)

# 폐쇄망 환경 (로컬 모델)
rag_service = create_rag_service(
    llm=VLLMClient("http://vllm:8000/v1", "qwen3-14b"),
    embedder=SentenceTransformersEmbedder("bge-m3"),
    vector_store=QdrantVectorStore("http://qdrant:6333"),
    reranker=CrossEncoderReranker("bge-reranker-v2-m3")
)

# 테스트 환경 (Mock 객체)
rag_service = create_rag_service(
    llm=MockLLM(),
    embedder=MockEmbedder(),
    vector_store=MockVectorStore(),
    reranker=MockReranker()
)
```

### Factory 패턴 (선택사항)

```python
class LLMFactory:
    @staticmethod
    def create(config: LLMConfig) -> LLMClient:
        if config.provider == "vllm":
            return VLLMClient(config.endpoint, config.model)
        elif config.provider == "openai":
            return OpenAIClient(config.api_key)
        else:
            raise ValueError(f"Unknown provider: {config.provider}")

# 사용
config = load_config("configs/models.yaml")
llm = LLMFactory.create(config.llm)
```

---

## Consequences

### ✅ 긍정적 영향

1. **느슨한 결합**: 구현체를 자유롭게 교체 가능
2. **테스트 용이**: Mock 객체로 쉽게 테스트
3. **환경별 설정**: 프로덕션/개발/테스트 환경 분리
4. **확장성**: 새로운 구현체 추가가 기존 코드에 영향 없음
5. **버전 관리**: 모델/라이브러리 업그레이드 쉬움

### 예시: 모델 업그레이드

```python
# 이전 (강한 결합)
# 1. 코드 수정: BEG-M3 → BGE-M4
# 2. 모든 테스트 다시 실행
# 3. 빌드/배포

# 현재 (Protocol 기반)
# 1. 설정 파일만 수정
configs/models.yaml:
  embedding:
    model_name: "bge-m4"  # bge-m3 → bge-m4
# 2. 재배포 (코드 변경 없음)
```

### ⚠️ 주의사항

1. **초기 복잡성**: Protocol 정의에 시간 소요
   - 해결: 초기 투자로 장기 수익
2. **Protocol 명확성**: 인터페이스 설계 중요
   - 해결: 명확한 타입힌트와 Docstring 작성

---

## Alternatives Considered

### 1. Hardcoded 구현체
```python
# ❌ 검토하지 않음
embedder = SentenceTransformer("bge-m3")
```
**문제**: 모델 변경 시 코드 수정, 테스트 불가

### 2. 추상 기본 클래스 (ABC)
```python
# ~ 검토함 (Protocol과 유사하지만 덜 선호)
from abc import ABC, abstractmethod

class LLMClient(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> str:
        pass
```
**차이점**: 
- Protocol: Structural subtyping (덕 타이핑)
- ABC: Nominal subtyping (명시적 상속 필요)
- **선택**: Protocol (더 Python다움)

### 3. ✅ 현재 선택: Protocol + Dependency Injection
- 가독성 높음
- Structural typing 활용
- 런타임 오버헤드 없음

---

## 구현 가이드

### 1단계: Protocol 정의

```python
# rag_core/interfaces/embedder.py
from typing import Protocol, List

class Embedder(Protocol):
    """임베딩 인터페이스"""
    
    def embed(self, text: str) -> List[float]:
        """단일 텍스트를 임베딩한다."""
        ...
```

### 2단계: 구현체 작성

```python
# rag_core/implementations/embedders/sentence_transformers_embedder.py
from sentence_transformers import SentenceTransformer
from rag_core.interfaces import Embedder

class SentenceTransformersEmbedder:
    def __init__(self, model_name: str):
        self.model = SentenceTransformer(model_name)
    
    def embed(self, text: str) -> List[float]:
        return self.model.encode(text).tolist()
```

### 3단계: Dependency Injection

```python
# app/services/rag_service.py
from rag_core.interfaces import Embedder, LLMClient, VectorStore
from rag_core.implementations import (
    SentenceTransformersEmbedder,
    VLLMClient,
    QdrantVectorStore
)

def create_rag_service(
    llm: LLMClient,
    embedder: Embedder,
    vector_store: VectorStore
):
    return RAGService(llm, embedder, vector_store)

# 초기화
llm = VLLMClient(...)
embedder = SentenceTransformersEmbedder("bge-m3")
vector_store = QdrantVectorStore(...)

rag_service = create_rag_service(llm, embedder, vector_store)
```

### 4단계: 테스트

```python
# tests/test_rag_service.py
from rag_core.implementations import MockLLM, MockEmbedder, MockVectorStore

def test_rag_service():
    rag_service = create_rag_service(
        llm=MockLLM(),
        embedder=MockEmbedder(),
        vector_store=MockVectorStore()
    )
    # ... 테스트
```

---

## Related

- [ADR-001: Citation 메타데이터 설계](./001-citation-metadata-design.md) - 메타데이터 전달
- [ADR-003: LangGraph 오케스트레이션](./003-langraph-orchestration.md) - 워크플로우 설계
- [packages/rag_core/README.md](../../packages/rag_core/README.md) - 구현 문서

---

**작성자**: AI Assistant  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성
