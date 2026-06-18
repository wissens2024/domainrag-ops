# Backend

DomainRAG Ops FastAPI 백엔드 - RAG 챗봇 API 및 관리 기능

## 개요

FastAPI 기반 Python 백엔드로 다음을 포함합니다:

- **Chat API** - 사용자 질문 처리, citation 포함 답변 생성
- **Admin API** - 문서 업로드, 메타데이터 관리, 로그 조회
- **Document Ingestion** - 파일 파싱, chunking, embedding, indexing
- **RAG Orchestration** - LangGraph 기반 워크플로우
- **Security & ACL** - 사용자 권한 기반 접근 제어

---

## 프로젝트 구조

```
backend/
├── README.md                      # 이 파일
├── pyproject.toml                 # Poetry 설정
├── poetry.lock                    # 의존성 락 파일
├── requirements.txt               # pip 요구사항
├── main.py                        # 프로젝트 루트 진입점
│
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI 애플리케이션
│   │
│   ├── api/                       # API 라우트
│   │   ├── __init__.py
│   │   ├── chat.py                # POST /api/chat
│   │   ├── documents.py           # 문서 관련 API
│   │   ├── admin.py               # 관리자 API
│   │   └── feedback.py            # 피드백 API
│   │
│   ├── services/                  # 비즈니스 로직
│   │   ├── __init__.py
│   │   ├── rag_service.py         # RAG 오케스트레이션
│   │   ├── document_service.py    # 문서 관리
│   │   ├── indexing_service.py    # 인덱싱 작업
│   │   ├── retrieval_service.py   # 검색 & ACL
│   │   ├── citation_service.py    # Citation 생성/검증
│   │   └── admin_service.py       # 관리자 기능
│   │
│   ├── models/                    # Pydantic 모델
│   │   ├── __init__.py
│   │   ├── chat.py                # Chat 요청/응답
│   │   ├── document.py            # Document 모델
│   │   ├── indexing.py            # Indexing 모델
│   │   ├── citation.py            # Citation 모델
│   │   └── admin.py               # Admin 모델
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py              # 설정 로더
│   │   ├── security.py            # 보안 (ACL, 권한)
│   │   ├── logging.py             # 로깅 설정
│   │   └── exceptions.py          # 커스텀 예외
│   │
│   └── db/
│       ├── __init__.py
│       ├── postgres.py            # PostgreSQL 연결
│       ├── schemas.py             # SQLAlchemy 스키마
│       └── queries.py             # 자주 사용하는 쿼리
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                # pytest 설정
│   ├── test_chat.py               # Chat API 테스트
│   ├── test_document.py           # Document API 테스트
│   ├── test_indexing.py           # Indexing 테스트
│   ├── test_services/
│   │   ├── test_rag_service.py
│   │   ├── test_retrieval_service.py
│   │   └── test_citation_service.py
│   └── fixtures/
│       ├── sample_documents/      # 테스트 문서
│       └── mock_data.py           # Mock 데이터
│
├── migrations/                    # Alembic DB 마이그레이션 (선택)
│   └── versions/
│
└── logs/
    └── app.log
```

---

## 주요 API 엔드포인트

### Chat API

#### POST /api/chat

사용자 질문에 대해 근거 문서 기반 답변을 생성합니다.

**요청:**
```json
{
  "conversation_id": "optional-uuid",
  "user_id": "user-001",
  "question": "내부망 반출 절차를 알려줘"
}
```

**응답:**
```json
{
  "conversation_id": "uuid",
  "message_id": "uuid",
  "answer": "내부망 반출은 신청서 작성, 부서장 승인, 보안부서 검토 후 가능하다고 명시되어 있습니다. [1]",
  "citations": [
    {
      "citation_id": "cite-001",
      "marker": "[1]",
      "doc_id": "DOC-2026-0001",
      "chunk_id": "DOC-2026-0001:v1:chunk:0012",
      "title": "보안 운영 매뉴얼.pdf",
      "page_number": 12,
      "section_title": "반출 신청 및 승인 절차",
      "excerpt": "내부망 자료 반출은 신청서 작성...",
      "score": 0.82,
      "rerank_score": 0.91,
      "verified": true
    }
  ],
  "metadata": {
    "llm_model": "qwen2.5-7b-instruct-awq",
    "embedding_model": "bge-m3",
    "reranker_model": "bge-reranker-v2-m3",
    "prompt_version": "rag_answer_v1",
    "latency_ms": 3120
  }
}
```

---

### Document API

#### POST /api/admin/documents/upload

문서를 업로드하고 인덱싱 작업을 시작합니다.

**요청:** multipart/form-data
```
file: <binary>
title: "보안 운영 매뉴얼.pdf"
department: "security"
doc_type: "manual"
security_level: "internal"
version: "v1"
tags: ["보안", "내부망"]
valid_from: "2026-05-01"
valid_until: null
owner: "security-team"
```

**응답:**
```json
{
  "doc_id": "DOC-2026-0001",
  "job_id": "IDX-2026-0001",
  "status": "pending",
  "message": "문서 업로드가 완료되었고 인덱싱 작업이 시작되었습니다."
}
```

#### GET /api/admin/documents

문서 목록을 조회합니다.

**쿼리 매개변수:**
- `keyword: str` - 검색어
- `department: str` - 부서 필터
- `doc_type: str` - 문서 유형 필터
- `security_level: str` - 보안 등급 필터
- `status: str` - 상태 필터
- `page: int` - 페이지 (기본값: 1)
- `page_size: int` - 페이지 크기 (기본값: 20)

**응답:**
```json
{
  "total": 128,
  "page": 1,
  "page_size": 20,
  "documents": [
    {
      "doc_id": "DOC-2026-0001",
      "title": "보안 운영 매뉴얼.pdf",
      "department": "security",
      "doc_type": "manual",
      "security_level": "internal",
      "version": "v1",
      "chunk_count": 128,
      "status": "approved",
      "created_at": "2026-05-08T10:00:00Z",
      "updated_at": "2026-05-08T10:00:00Z"
    }
  ]
}
```

---

### Admin API

#### GET /api/admin/dashboard

대시보드 통계를 반환합니다.

**응답:**
```json
{
  "total_documents": 128,
  "total_chunks": 18420,
  "uploaded_today": 5,
  "indexing_completed_today": 4,
  "indexing_failed_today": 1,
  "questions_today": 213,
  "average_latency_ms": 2840,
  "answers_without_citation": 3,
  "negative_feedback_rate": 0.08
}
```

#### GET /api/admin/indexing/jobs

인덱싱 작업 목록을 조회합니다.

**응답:**
```json
{
  "jobs": [
    {
      "job_id": "IDX-2026-0001",
      "doc_id": "DOC-2026-0001",
      "filename": "보안 운영 매뉴얼.pdf",
      "status": "completed",
      "progress": 100,
      "step": "indexing",
      "total_chunks": 128,
      "indexed_chunks": 128,
      "started_at": "2026-05-08T10:00:00Z",
      "finished_at": "2026-05-08T10:03:00Z"
    }
  ]
}
```

#### GET /api/admin/logs/chat

질문 로그를 조회합니다.

**응답:**
```json
{
  "total": 213,
  "page": 1,
  "logs": [
    {
      "request_id": "REQ-001",
      "user_id": "user-001",
      "question": "내부망 반출 절차는?",
      "answer": "내부망 반출은...",
      "retrieved_chunks_count": 5,
      "citations_count": 2,
      "llm_model": "qwen2.5-7b-instruct-awq",
      "latency_ms": 3120,
      "feedback": "good",
      "created_at": "2026-05-08T10:00:00Z"
    }
  ]
}
```

#### GET /api/admin/logs/chat/{log_id}

특정 질문 로그의 상세 정보를 조회합니다.

**응답:**
```json
{
  "request_id": "REQ-001",
  "user_id": "user-001",
  "question": "내부망 반출 절차는?",
  "rewritten_query": "내부망 자료 반출 절차",
  "answer": "내부망 반출은...",
  "retrieved_chunks": [
    {
      "chunk_id": "DOC-2026-0001:v1:chunk:0012",
      "doc_id": "DOC-2026-0001",
      "title": "보안 운영 매뉴얼.pdf",
      "page_number": 12,
      "section_title": "반출 신청 및 승인 절차",
      "content": "...",
      "score": 0.82,
      "rerank_score": 0.91
    }
  ],
  "citations": [...],
  "llm_model": "qwen2.5-7b-instruct-awq",
  "embedding_model": "bge-m3",
  "reranker_model": "bge-reranker-v2-m3",
  "prompt_version": "rag_answer_v1",
  "latency_ms": 3120,
  "feedback": "good",
  "created_at": "2026-05-08T10:00:00Z"
}
```

---

## 서비스 레이어

### RAGService

전체 RAG 오케스트레이션을 담당합니다.

```python
class RAGService:
    async def process_question(
        self,
        question: str,
        user_id: str,
        conversation_id: Optional[str] = None
    ) -> ChatResponse:
        """
        사용자 질문을 처리하여 답변과 citation을 생성합니다.
        
        단계:
        1. 사용자 컨텍스트 로드 (그룹, 권한)
        2. 쿼리 분류
        3. 문서 검색 (ACL 필터 적용)
        4. Reranking
        5. LLM 답변 생성
        6. Citation 검증
        7. 로그 저장
        """
```

### DocumentService

문서 업로드 및 메타데이터 관리를 담당합니다.

```python
class DocumentService:
    async def upload_document(
        self,
        file: UploadFile,
        metadata: DocumentMetadata
    ) -> DocumentUploadResponse:
        """
        1. MinIO에 원본 파일 저장
        2. Database에 문서 메타데이터 저장
        3. Indexing job 생성
        4. 백그라운드 처리 시작
        """
```

### IndexingService

문서 파싱, chunking, embedding, indexing을 담당합니다.

```python
class IndexingService:
    async def process_document(self, job_id: str):
        """
        1. 원본 파일 파싱 (PDF/DOCX/TXT)
        2. Chunking (semantic, 최대 토큰 제한)
        3. Metadata 생성
        4. Embedding 생성
        5. Qdrant에 저장
        6. Job 상태 업데이트
        """
```

### RetrievalService

벡터 검색 및 ACL 필터링을 담당합니다.

```python
class RetrievalService:
    def retrieve_with_acl(
        self,
        query: str,
        user_context: UserContext,
        top_k: int = 10
    ) -> List[Chunk]:
        """
        1. ACL 필터 빌드 (사용자 그룹, 보안 등급)
        2. Qdrant 검색
        3. 메타데이터 필터 적용 (유효 기간 등)
        4. 상위 k개 반환
        """
```

### CitationService

Citation 생성 및 검증을 담당합니다.

```python
class CitationService:
    def generate_citations(
        self,
        answer: str,
        retrieved_chunks: List[Chunk],
        llm_response: str
    ) -> List[Citation]:
        """
        1. LLM 답변에서 citation marker [1], [2] 추출
        2. Marker가 실제 chunk에 존재하는지 검증
        3. 존재하지 않는 marker 제거
        4. Citation 객체 생성
        """
```

---

## 설정 관리

### config.py

Pydantic BaseSettings 기반 설정 로더:

```python
class ModelConfig(BaseSettings):
    llm_endpoint: str = "http://vllm:8000/v1"
    llm_model: str = "qwen2.5-7b-instruct-awq"
    embedding_model: str = "bge-m3"
    reranker_model: str = "bge-reranker-v2-m3"
    
class RetrievalConfig(BaseSettings):
    vector_db_url: str = "http://qdrant:6333"
    top_k: int = 10
    rerank_top_k: int = 5
    
class AppConfig(BaseSettings):
    models: ModelConfig
    retrieval: RetrievalConfig
    database_url: str
    minio_endpoint: str
```

**환경 변수:**
```bash
MODELS__LLM_ENDPOINT=http://vllm:8000/v1
MODELS__LLM_MODEL=qwen2.5-7b-instruct-awq
RETRIEVAL__TOP_K=10
DATABASE_URL=postgresql://user:pass@postgres:5432/domainrag
```

---

## 데이터베이스

### PostgreSQL 스키마

**documents 테이블:**
```python
class Document(Base):
    __tablename__ = "documents"
    
    id = Column(UUID, primary_key=True)
    doc_id = Column(String, unique=True)
    title = Column(String)
    source_type = Column(String)  # pdf, docx, txt
    department = Column(String)
    doc_type = Column(String)
    security_level = Column(String)  # public, internal, confidential
    version = Column(String)
    owner = Column(String)
    tags = Column(JSON)
    approval_status = Column(String)  # draft, approved, archived
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
```

**chunks 테이블:**
```python
class Chunk(Base):
    __tablename__ = "chunks"
    
    id = Column(UUID, primary_key=True)
    chunk_id = Column(String, unique=True)
    doc_id = Column(String, ForeignKey("documents.doc_id"))
    doc_version = Column(String)
    chunk_index = Column(Integer)
    title = Column(String)
    page_number = Column(Integer)
    section_title = Column(String)
    heading_path = Column(JSON)  # ["보안 정책", "자료 반출", "..."]
    content = Column(Text)
    content_hash = Column(String)
    start_char = Column(Integer)
    end_char = Column(Integer)
    token_count = Column(Integer)
    security_level = Column(String)
    acl = Column(JSON)  # ["group:security", "user:admin"]
    tags = Column(JSON)
    embedding_model = Column(String)
    embedding_version = Column(String)
    vector_id = Column(String)  # Qdrant point ID
    created_at = Column(DateTime, default=datetime.utcnow)
```

---

## 보안 & ACL

### UserContext

사용자 권한 정보:

```python
class UserContext:
    user_id: str
    groups: List[str]  # ["security", "it-admin"]
    clearance: str     # "public", "internal", "confidential"
```

### ACL Filter

Qdrant payload filter 생성:

```python
def build_acl_filter(user_context: UserContext) -> Filter:
    """
    조건:
    1. chunk.acl에 user_id 또는 group이 포함
    2. chunk.security_level이 user.clearance 이하
    3. chunk.valid_from <= today
    4. chunk.valid_until is null 또는 >= today
    5. chunk.approval_status = approved
    """
```

---

## 테스트

### Unit 테스트

```bash
# 모든 테스트 실행
pytest

# 특정 테스트 파일
pytest tests/test_chat.py

# 커버리지 리포트
pytest --cov=app tests/
```

**테스트 예시:**

```python
@pytest.mark.asyncio
async def test_chat_returns_answer_with_citations(mock_qdrant, mock_llm):
    """Chat API가 citation을 포함한 답변을 반환하는지 확인"""
    service = RAGService(mock_qdrant, mock_llm)
    response = await service.process_question("반출 절차", "user-001")
    
    assert response.answer
    assert len(response.citations) > 0
    assert all(c.page_number for c in response.citations)

@pytest.mark.asyncio
async def test_retrieval_applies_acl_filter(mock_qdrant):
    """Retrieval이 ACL 필터를 적용하는지 확인"""
    service = RetrievalService(mock_qdrant)
    user_context = UserContext(
        user_id="user-001",
        groups=["security"],
        clearance="internal"
    )
    
    chunks = service.retrieve_with_acl("반출", user_context)
    
    # 보안 등급이 internal 이상인 chunk는 없어야 함
    assert all(c.security_level <= "internal" for c in chunks)
```

---

## 개발 시작

### 사전 요구사항

- Python 3.10+
- PostgreSQL 14+
- Qdrant
- vLLM (또는 Mock)

### 설치 및 실행

```bash
# 의존성 설치 (Poetry)
poetry install

# 또는 pip
pip install -r requirements.txt

# 환경 설정
cp .env.example .env
# .env 파일 수정

# 데이터베이스 마이그레이션
alembic upgrade head

# 개발 서버 실행
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

서버: http://localhost:8000

API 문서: http://localhost:8000/docs

---

## 구현 진행 상황

- [ ] FastAPI 기본 설정
- [ ] PostgreSQL 연결
- [ ] Chat API (`POST /api/chat`)
  - [ ] 기본 동작
  - [ ] Citation 생성
  - [ ] Citation 검증
- [ ] Document Upload API (`POST /api/admin/documents/upload`)
  - [ ] 파일 업로드
  - [ ] MinIO 저장
  - [ ] 메타데이터 DB 저장
- [ ] Parser & Chunker
  - [ ] PDF 파싱
  - [ ] DOCX 파싱
  - [ ] TXT 파싱
  - [ ] Semantic chunking
- [ ] Embedding & Indexing
  - [ ] 로컬 embedder
  - [ ] Qdrant 연결
  - [ ] Vector 저장
- [ ] Retrieval & ACL
  - [ ] Qdrant 검색
  - [ ] ACL 필터
  - [ ] Metadata 필터
- [ ] Reranker
  - [ ] Cross-encoder 모델
  - [ ] Reranking 로직
- [ ] LangGraph Orchestration
  - [ ] Workflow 정의
  - [ ] Node 구현
- [ ] Admin APIs
  - [ ] Dashboard
  - [ ] Document 관리
  - [ ] Indexing 모니터링
  - [ ] Log 조회
- [ ] Unit 테스트
- [ ] 통합 테스트

---

## 팁 & 체크리스트

### ✅ 구현할 때 확인

- [ ] 모든 함수에 타입힌트
- [ ] 모든 함수/클래스에 Docstring
- [ ] 에러 처리 (try-except, logging)
- [ ] 설정값 하드코딩 안 함 (YAML/환경변수)
- [ ] ACL 필터 항상 적용
- [ ] Citation 검증 로직 포함
- [ ] Unit 테스트 작성
- [ ] 비동기 처리 (`async/await`)

### ❌ 피할 것

- 외부 API 호출 (OpenAI)
- 권한 필터 없는 검색
- 근거 없는 답변
- 하드코딩된 모델명/endpoint

---

## 참고 문서

- [IMPLEMENTATION_SPEC.md - API 명세](../IMPLEMENTATION_SPEC.md#8-api-명세)
- [docs/API.md](../docs/API.md)
- [CLAUDE.md](../CLAUDE.md)
