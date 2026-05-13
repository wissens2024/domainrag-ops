# Frontend

DomainRAG Ops 사용자 인터페이스 및 관리자 콘솔 (Next.js + TypeScript)

## 개요

Next.js 기반 풀스택 프론트엔드로 다음을 포함합니다:

- **사용자 채팅 화면** (`/chat`) - 도메인 질문, 답변, 근거 문서 citation 표시
- **관리자 콘솔** (`/admin/*`) - 문서 관리, 인덱싱 모니터링, 로그 조회
- **실시간 상태 업데이트** - 인덱싱 진행률, 질문 로그
- **반응형 디자인** - 모바일 친화적 UI

---

## 프로젝트 구조

```
frontend/
├── README.md                      # 이 파일
├── package.json                   # 의존성
├── next.config.js                 # Next.js 설정
├── tsconfig.json                  # TypeScript 설정
├── tailwind.config.js             # Tailwind CSS 설정
├── postcss.config.js              # PostCSS 설정
│
├── app/                           # Next.js App Router
│   ├── layout.tsx                 # 루트 레이아웃
│   ├── page.tsx                   # 홈 페이지 (리다이렉트)
│   ├── chat/
│   │   ├── layout.tsx             # 채팅 레이아웃
│   │   ├── page.tsx               # 채팅 메인 페이지
│   │   └── [id]/
│   │       └── page.tsx           # 특정 대화 페이지
│   │
│   └── admin/
│       ├── layout.tsx             # 관리자 레이아웃
│       ├── page.tsx               # 어드민 홈 (리다이렉트)
│       ├── dashboard/
│       │   └── page.tsx           # 대시보드
│       ├── documents/
│       │   ├── page.tsx           # 문서 목록
│       │   ├── upload/
│       │   │   └── page.tsx       # 문서 업로드
│       │   └── [id]/
│       │       └── page.tsx       # 문서 상세
│       ├── indexing/
│       │   └── page.tsx           # 인덱싱 모니터링
│       └── logs/
│           ├── page.tsx           # 로그 메인
│           └── chat/
│               ├── page.tsx       # 질문 로그
│               └── [id]/
│                   └── page.tsx   # 로그 상세
│
├── components/
│   ├── chat/
│   │   ├── ChatWindow.tsx         # 채팅 메인 컴포넌트
│   │   ├── AnswerCard.tsx         # AI 답변 카드
│   │   ├── CitationPanel.tsx      # 근거 문서 패널
│   │   ├── ChatInput.tsx          # 질문 입력창
│   │   ├── ConversationList.tsx   # 대화 목록 사이드바
│   │   ├── MessageBubble.tsx      # 메시지 버블
│   │   └── LoadingSpinner.tsx     # 로딩 스피너
│   │
│   ├── admin/
│   │   ├── DashboardCards.tsx     # 대시보드 카드
│   │   ├── DocumentTable.tsx      # 문서 테이블
│   │   ├── DocumentUploadForm.tsx # 문서 업로드 폼
│   │   ├── IndexingJobsTable.tsx  # 인덱싱 job 테이블
│   │   ├── ChatLogsTable.tsx      # 질문 로그 테이블
│   │   ├── LogDetailModal.tsx     # 로그 상세 모달
│   │   └── ProgressBar.tsx        # 진행률 바
│   │
│   └── common/
│       ├── Header.tsx             # 상단 헤더
│       ├── Sidebar.tsx            # 좌측 사이드바
│       ├── Button.tsx             # 재사용 가능한 버튼
│       ├── Input.tsx              # 재사용 가능한 입력
│       ├── Modal.tsx              # 모달 다이얼로그
│       ├── Alert.tsx              # 알림 메시지
│       ├── Badge.tsx              # 상태 뱃지
│       └── Pagination.tsx         # 페이지네이션
│
├── lib/
│   ├── api.ts                     # API 클라이언트
│   ├── types.ts                   # TypeScript 타입 정의
│   ├── hooks/
│   │   ├── useChat.ts             # 채팅 로직 hook
│   │   ├── useAdmin.ts            # 관리자 로직 hook
│   │   ├── useFetch.ts            # 데이터 페칭 hook
│   │   └── useLocalStorage.ts     # 로컬 스토리지 hook
│   └── utils/
│       ├── formatters.ts          # 포맷팅 유틸
│       ├── validators.ts          # 검증 유틸
│       └── constants.ts           # 상수
│
├── public/
│   ├── favicon.ico
│   ├── logo.svg
│   └── placeholder.png
│
├── styles/
│   ├── globals.css                # 전역 스타일
│   └── variables.css              # CSS 변수
│
└── __tests__/
    ├── components/
    └── lib/
```

---

## 주요 페이지 & 기능

### 사용자 채팅 화면 (`/chat`)

**기능:**
- 새 대화 시작 버튼
- 이전 대화 목록 (좌측 사이드바)
- 중앙 채팅 영역
  - 사용자 메시지 표시
  - AI 답변 카드 (citation marker [1], [2] 포함)
  - 로딩 상태
  - 오류 메시지
- 하단 질문 입력창
- 우측 Citation Panel
  - 클릭한 citation의 상세 정보
  - 원문 발췌
  - 페이지 번호, 섹션 제목
  - 원문 보기 링크

**API 연동:**
- `POST /api/chat` - 질문 전송 & 답변 수신
- `GET /api/conversations` - 대화 목록
- `POST /api/feedback` - 피드백 제출

---

### 관리자 대시보드 (`/admin/dashboard`)

**표시 항목:**
- 총 문서 수
- 총 chunk 수
- 오늘 업로드 문서 수
- 인덱싱 성공/실패 수
- 오늘 질문 수
- 평균 응답 시간
- citation 없는 답변 수
- 사용자 피드백 부정 비율

**API 연동:**
- `GET /api/admin/dashboard` - 대시보드 통계

---

### 문서 관리 (`/admin/documents`)

**기능:**
- 문서 목록 테이블
  - 검색, 필터링 (부서, 보안등급, 상태)
  - 정렬 (문서명, 업로드일, 상태)
  - 페이지네이션
- 문서 상세 정보
- 메타데이터 수정
- 재색인 버튼
- 비활성화 버튼

**API 연동:**
- `GET /api/admin/documents` - 문서 목록
- `PUT /api/admin/documents/{id}` - 메타데이터 수정
- `POST /api/admin/documents/{id}/reindex` - 재색인

---

### 문서 업로드 (`/admin/documents/upload`)

**입력 필드:**
- 파일 선택
- 문서명
- 부서
- 문서 유형
- 보안 등급
- 버전
- 태그
- 유효 기간

**업로드 결과:**
- 업로드 진행률
- 파싱 상태
- Chunk 생성 수
- Embedding 상태
- 최종 결과 (성공/실패)

**API 연동:**
- `POST /api/admin/documents/upload` - 파일 업로드

---

### 인덱싱 모니터링 (`/admin/indexing`)

**표시:**
- 인덱싱 job 테이블
- Job ID, 문서명, 상태
- 진행률 바
- 시작/종료 시간
- 오류 메시지
- 재시도 버튼

**상태:**
- `pending` - 대기 중
- `parsing` - 파싱 중
- `chunking` - 청킹 중
- `embedding` - 임베딩 중
- `indexing` - 인덱싱 중
- `completed` - 완료
- `failed` - 실패

**API 연동:**
- `GET /api/admin/indexing/jobs` - Job 목록
- `POST /api/admin/indexing/jobs/{id}/retry` - 재시도

---

### 질문 로그 (`/admin/logs/chat`)

**표시:**
- 질문 시간, 사용자, 질문 내용
- 답변 요약
- 검색된 chunk 수, citation 수
- 사용 모델
- 응답 시간
- 피드백
- 상세 보기 링크

**상세 정보:**
- 원본 질문
- 재작성된 질문
- 최종 답변
- Retrieved chunks 목록 (score, rerank_score)
- Citations 매핑
- 사용 모델 (LLM, Embedding, Reranker)
- Prompt version

**API 연동:**
- `GET /api/admin/logs/chat` - 로그 목록
- `GET /api/admin/logs/chat/{id}` - 상세 정보

---

## TypeScript 타입 정의

`lib/types.ts`에 정의된 주요 타입:

```typescript
// Chat 관련
interface ChatRequest {
  conversation_id?: string
  user_id: string
  question: string
}

interface Citation {
  citation_id: string
  marker: string
  doc_id: string
  chunk_id: string
  title: string
  page_number?: number
  section_title?: string
  excerpt: string
  score: number
  rerank_score: number
  verified: boolean
}

interface ChatResponse {
  conversation_id: string
  message_id: string
  answer: string
  citations: Citation[]
  metadata: {
    llm_model: string
    embedding_model: string
    reranker_model: string
    prompt_version: string
    latency_ms: number
  }
}

// Document 관련
interface Document {
  doc_id: string
  title: string
  department: string
  doc_type: string
  security_level: string
  version: string
  chunk_count: number
  status: "draft" | "approved" | "archived"
  created_at: string
  updated_at: string
}

// Indexing 관련
interface IndexingJob {
  job_id: string
  doc_id: string
  filename: string
  status: "pending" | "parsing" | "chunking" | "embedding" | "indexing" | "completed" | "failed"
  progress: number
  step: string
  error_message?: string
  total_chunks: number
  indexed_chunks: number
  started_at?: string
  finished_at?: string
}

// Dashboard 통계
interface DashboardStats {
  total_documents: number
  total_chunks: number
  uploaded_today: number
  indexing_completed_today: number
  indexing_failed_today: number
  questions_today: number
  average_latency_ms: number
  answers_without_citation: number
  negative_feedback_rate: number
}
```

---

## 스타일링

### Tailwind CSS + CSS Variables

글로벌 색상 팔레트:

```css
/* CSS Variables */
--color-primary: #3b82f6
--color-secondary: #10b981
--color-danger: #ef4444
--color-warning: #f59e0b
--color-gray-50: #f9fafb
--color-gray-900: #111827

--shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05)
--shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1)
```

### 컴포넌트 스타일링

- **Button**: 기본, 다양한 크기 (sm, md, lg), variant (primary, secondary, danger)
- **Input**: 텍스트, 숫자, 파일 업로드
- **Card**: 정보 표시 컨테이너
- **Badge**: 상태 뱃지 (성공, 경고, 실패)
- **Alert**: 알림 메시지 (성공, 에러, 정보)

---

## API 클라이언트

`lib/api.ts`에서 API 호출 처리:

```typescript
export const apiClient = {
  // Chat API
  chat: (request: ChatRequest) => POST<ChatResponse>('/api/chat', request),
  
  // Admin API
  admin: {
    dashboard: () => GET<DashboardStats>('/api/admin/dashboard'),
    documents: {
      list: (params: DocumentQueryParams) => GET<DocumentListResponse>('/api/admin/documents', params),
      upload: (formData: FormData) => POST('/api/admin/documents/upload', formData),
    },
    indexing: {
      jobs: () => GET<IndexingJobsResponse>('/api/admin/indexing/jobs'),
      retry: (jobId: string) => POST(`/api/admin/indexing/jobs/${jobId}/retry`),
    },
    logs: {
      chat: (params: LogQueryParams) => GET<ChatLogsResponse>('/api/admin/logs/chat', params),
      detail: (logId: string) => GET<ChatLogDetail>(`/api/admin/logs/chat/${logId}`),
    },
  },
  
  // Feedback API
  feedback: (request: FeedbackRequest) => POST('/api/feedback', request),
}
```

---

## 커스텀 Hooks

### useChat

채팅 로직 관리:

```typescript
const {
  messages,           // 메시지 목록
  conversations,      // 대화 목록
  loading,           // 로딩 상태
  error,             // 에러
  currentConversation, // 현재 대화
  
  sendMessage,       // 메시지 전송
  startNewChat,      // 새 대화 시작
  deleteConversation, // 대화 삭제
  submitFeedback,    // 피드백 제출
} = useChat()
```

### useAdmin

관리자 로직 관리:

```typescript
const {
  stats,             // 대시보드 통계
  documents,         // 문서 목록
  indexingJobs,      // 인덱싱 job
  chatLogs,          // 질문 로그
  
  loadStats,         // 통계 로드
  loadDocuments,     // 문서 목록 로드
  uploadDocument,    // 문서 업로드
  loadIndexingJobs,  // Job 목록 로드
  loadChatLogs,      // 로그 로드
} = useAdmin()
```

---

## 개발 시작

### 사전 요구사항

- Node.js 18+
- npm 또는 yarn

### 설치 및 실행

```bash
# 의존성 설치
npm install

# 개발 서버 실행
npm run dev

# 빌드
npm run build

# 프로덕션 실행
npm start

# 타입 체크
npm run type-check

# 린트
npm run lint

# 테스트
npm run test
```

서버: http://localhost:3000

---

## 구현 진행 상황

- [ ] 기본 레이아웃 (Header, Sidebar, Main)
- [ ] 채팅 화면 (`/chat`)
  - [ ] 채팅 메시지 표시
  - [ ] 질문 입력 및 전송
  - [ ] Citation marker 표시
  - [ ] Citation panel
- [ ] 관리자 콘솔 (`/admin/dashboard`)
  - [ ] 대시보드 카드
  - [ ] 문서 관리 페이지
  - [ ] 문서 업로드 폼
  - [ ] 인덱싱 모니터링
  - [ ] 로그 조회
- [ ] TypeScript 타입 정의
- [ ] API 클라이언트 + Hooks
- [ ] 스타일링 (Tailwind)
- [ ] Unit 테스트
- [ ] E2E 테스트

---

## 팁 & 체크리스트

### ✅ 구현할 때 확인

- [ ] TypeScript 타입 정의 포함
- [ ] 모든 페이지/컴포넌트에 JSDoc 주석
- [ ] 반응형 디자인 (모바일 친화)
- [ ] 에러 핸들링 (API 실패, 네트워크 오류)
- [ ] 로딩 상태 표시
- [ ] 접근성 (alt text, ARIA labels)
- [ ] 성능 최적화 (lazy loading, 메모이제이션)

### ❌ 피할 것

- HTML 직접 조작 (useRef 과용)
- 과도한 prop drilling (context 사용)
- 하드코딩된 문자열 (상수 파일 사용)
- API 호출 반복 (캐싱/최적화)

---

## 참고 문서

- [IMPLEMENTATION_SPEC.md - Prompt 2](../IMPLEMENTATION_SPEC.md#prompt-2-프론트엔드-채팅-ui)
- [IMPLEMENTATION_SPEC.md - Prompt 3](../IMPLEMENTATION_SPEC.md#prompt-3-관리자-콘솔-ui)
- [lib/types.ts](./lib/types.ts) - TypeScript 타입
- [API 명세](../docs/API.md)
