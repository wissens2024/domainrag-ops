# ADR-017: API Specification — Routes·Schemas·Auth·Errors

## Status
**Accepted** (2026-05-08)

> 본 ADR은 SPEC.md §8의 모든 API 명세를 흡수하고 ADR-008~015가 추가한 신규 endpoint(approval/lifecycle, LoRA upload, configs reload, assessment, input schemas)를 통합한다. SPEC.md 폐기 후 API 단일 진실 소스.

---

## Context

### 배경 (사실)

- SPEC.md §8이 chat·conversation·feedback·document upload·document list·indexing·dashboard 7개 endpoint sketch.
- ADR-008~015 진행으로 신규 endpoint 다수: tenant management, hard delete, approval patch, LoRA upload, configs reload, assessment 4종, input schemas.
- ADR-008이 URL path mirror 강제 (`/api/{tenant_id}/...`).
- ADR-013이 chat을 sync(`/chat`)와 streaming(`/chat/stream`) 두 endpoint로 분기.
- ADR-014가 assessment endpoint 분리.
- SSO/JWT 인증 (사양 추후 결정), AuthAdapter Protocol stub.
- 본 프로젝트 정책: 완제품 단일 설계.

### 가정

- FastAPI 0.110+ 사용 (OpenAPI 3.1 자동 생성)
- pydantic v2 (request/response 검증)
- HTTP/JSON 표준, file upload는 multipart/form-data
- SSE for streaming (WebSocket이 아닌 단방향 SSE — 단순)
- JWT Bearer token 인증 (`Authorization: Bearer ...`)
- 모든 endpoint에 `X-Request-Id` 자동 부여 (관측성)

가정 깨지면 재검토.

---

## Decision

### 1. URL 컨벤션

- `/api/{tenant_id}/...` — tenant scope endpoint
- `/api/platform/admin/...` — platform_admin 전용
- `/api/auth/...` — 인증 관련 (tenant scope 외)
- `/api/health` — 헬스체크 (인증 불필요)

JWT claim의 `tenant_id`와 URL `{tenant_id}` mismatch → 403.

### 2. 인증 (AuthAdapter Protocol stub)

```http
Authorization: Bearer <jwt-token>
```

- AuthAdapter가 JWT 검증 → UserContext 반환 (ADR-008 §7)
- UserContext: `user_id`, `tenant_id`, `groups`, `clearance`, `roles`, `raw_claims`
- 모든 endpoint는 AuthAdapter를 FastAPI Dependency로 주입

#### Token 갱신
```http
POST /api/auth/refresh
Body: { "refresh_token": "..." }
Response: { "access_token": "...", "expires_in": 900 }
```

#### Logout
```http
POST /api/auth/logout
Response: 204 No Content
```

(SSO 사양 확정 시 별도 ADR로 endpoint 구체화.)

### 3. Chat API (ADR-005/010/013)

#### 3.1 Sync (chat_structured) — POST /api/{tenant_id}/chat

Request:
```json
{
  "conversation_id": "uuid|null",
  "user_id": "user-001",
  "question": "내부망 반출 절차를 알려줘",
  "ui_mode_request": "structured|streaming|null"
}
```

Response (success):
```json
{
  "status": "success",
  "conversation_id": "uuid",
  "message_id": "uuid",
  "answer": "내부망 반출은 신청서 작성... [1] 승인 전 반출은... [2] 긴급 절차는 명시되지... ⚠",
  "answer_segments": [...],
  "citations": [
    {
      "citation_id": "cite-001",
      "marker": "[1]",
      "tenant_id": "security",
      "support_type": "direct",
      "support_level": "strong",
      "verified": true,
      "claim_text": "내부망 반출은 신청서 작성, 부서장 승인...",
      "doc_id": "DOC-2026-0001",
      "chunk_id": "DOC-2026-0001:v1:p1:chunk:0012",
      "title": "보안 운영 매뉴얼.pdf",
      "page_number": 12,
      "section_title": "반출 신청 및 승인 절차",
      "excerpt": "내부망 자료 반출은...",
      "score": 0.82,
      "rerank_score": 0.91
    },
    { "citation_id": "...", "support_type": "synthesis", "citations": [1,2,3], ... },
    { "citation_id": "...", "support_type": "inference", "inference_chain": "...", ... },
    { "citation_id": "...", "support_type": "conflict", "conflict_groups": [[1],[2]], ... }
  ],
  "metadata": {
    "ui_mode": "chat_structured",
    "llm_model": "qwen3-7b-security-ft",
    "lora_adapter": "security-policy-v1",
    "embedding_model": "bge-m3",
    "reranker_model": "bge-reranker-v2-m3",
    "prompt_version": "security:chat_answer:v1:control",
    "latency_ms": 3120,
    "confidence": 0.82,
    "verifier": {
      "tier1_markers_removed": 0,
      "tier2_avg_similarity": 0.81,
      "tier3_unsupported_segments": 1,
      "claim_extraction_mode": "structured",
      "inference_judge_results": []
    },
    "routing_decision": { "matched_rule": "synthesis_answer", "fallback_chain_used": false },
    "classifier_decision": { "tier1_matched": "synthesis_pattern", "query_type": "document_qa", "complexity": "medium" }
  }
}
```

Response (fallback):
```json
{
  "status": "fallback",
  "conversation_id": "uuid",
  "message_id": "uuid",
  "answer": "죄송합니다. 이 주제에 대해 문서 기준으로 확인할 수 없습니다.",
  "fallback": {
    "reason": "low_retrieval | low_generation_quality | inference_judge_rejected | conflict_unverifiable | disallowed_type | empty_tenant | tenant_migrating | retrieval_unavailable",
    "near_misses": [...],
    "suggested_actions": [...],
    "retry_after_seconds": 60
  },
  "citations": [],
  "metadata": { ... }
}
```

#### 3.2 Streaming (chat_streaming) — POST /api/{tenant_id}/chat/stream (SSE)

Request: 동일.

Response: SSE 스트림
```
event: token
data: {"text": "내부망"}

event: token
data: {"text": " 반출은"}

...

event: complete
data: {"message_id": "uuid", "metadata": {...}}
```

`citations` 비활성. structured/streaming 모드 결정은 라우팅 룰 + 사용자 명시 요청.

### 4. Conversation API

```http
GET    /api/{tenant_id}/conversations               # 사용자 본인 대화 목록
GET    /api/{tenant_id}/conversations/{id}          # 상세 (메시지 포함)
DELETE /api/{tenant_id}/conversations/{id}          # 삭제
PATCH  /api/{tenant_id}/conversations/{id}          # 제목 수정
```

### 5. Feedback API

```http
POST /api/{tenant_id}/feedback
Body: { "message_id": "uuid", "feedback": "good|bad", "comment": "..." }
Response: 204
```

### 6. Document API (Tenant Admin)

#### 6.1 Upload — POST /api/{tenant_id}/admin/documents/upload

multipart form:
- `file`: binary
- `input_type`: string (ADR-015)
- `metadata`: JSON string (input_type schema에 맞는 필드들)

Response:
```json
{
  "doc_id": "DOC-2026-0001",
  "job_id": "IDX-2026-0001",
  "status": "pending",
  "input_schema_version": "v1"
}
```

업로드 후 `metadata`는 input_type schema로 검증 (ADR-015 §8). 검증 실패 시 422 Unprocessable Entity + 필드별 오류.

#### 6.2 List — GET /api/{tenant_id}/admin/documents

Query: `keyword, input_type, department, security_level, status, approval_status, page, page_size, sort_by, sort_order`

Response:
```json
{
  "items": [{ doc 메타 ... }],
  "total": 128,
  "page": 1,
  "page_size": 20
}
```

#### 6.3 Detail — GET /api/{tenant_id}/admin/documents/{doc_id}

문서 메타 + chunks 목록 + indexing 이력 + 사용 이력 (chat_logs에서 인용된 횟수).

#### 6.4 Update Metadata — PATCH /api/{tenant_id}/admin/documents/{doc_id}

input_type schema 기반 부분 갱신. `payload-only` 갱신 (ADR-007/012) — chunks 재생성 안 함.

#### 6.5 Approval — PATCH /api/{tenant_id}/admin/documents/{doc_id}/approval (ADR-007/012)

```json
{ "approval_status": "approved" }
```

content 불변, payload 자동 동기화 (chunks UPDATE + Qdrant set_payload).

#### 6.6 Re-index — POST /api/{tenant_id}/admin/documents/{doc_id}/reindex (ADR-012)

```json
{ "mode": "parser_only|chunk_re_split|embedding_only|full" }
```

Response: `{ "job_id": "..." }`

#### 6.7 Hard Delete — DELETE /api/{tenant_id}/admin/documents/{doc_id}/hard (ADR-007/012)

```json
{
  "reason": "compliance request",
  "chat_logs_action": "keep_excerpts|mask_excerpts|delete_logs"
}
```

Response: `{ "removed_chunks": 47, "affected_chat_logs": 23, "chat_logs_action_applied": "keep_excerpts" }`

### 7. Indexing Jobs API

```http
GET  /api/{tenant_id}/admin/indexing/jobs              # 목록 (status, date 필터)
GET  /api/{tenant_id}/admin/indexing/jobs/{job_id}     # 상세 (failed_chunks JSONB 포함)
POST /api/{tenant_id}/admin/indexing/jobs/{job_id}/retry
```

### 8. Chat Logs API (관리자 조회)

```http
GET  /api/{tenant_id}/admin/logs/chat                  # 필터·페이징
GET  /api/{tenant_id}/admin/logs/chat/{request_id}     # 상세 (모든 메타데이터)
```

상세 응답: chat_logs 테이블의 모든 컬럼 (citations, retrieved_chunks, verifier_metrics, routing_decision, model_failure_chain, classifier_decision, inference_judge_results, conflict_groups, fallback_reason, confidence, ui_mode, latency, prompt_version, ...).

### 9. Citation Inspector API (ADR-010)

```http
GET  /api/{tenant_id}/admin/citation-inspector/distribution
       # citation_type 분포 차트 데이터
       # Query: from_date, to_date, group_by (day|hour)
GET  /api/{tenant_id}/admin/citation-inspector/segments/{message_id}
       # 특정 답변의 segment-level 분석 (claim ↔ chunk 매핑)
POST /api/{tenant_id}/admin/citation-inspector/reverify
       # 운영자가 Tier 2 재검증 트리거 (예: 임계치 변경 후)
       Body: { "from_date": "...", "to_date": "..." }
```

### 10. Admin Dashboard API

```http
GET /api/{tenant_id}/admin/dashboard
```

Response:
```json
{
  "total_documents": 128,
  "total_chunks": 18420,
  "uploaded_today": 5,
  "indexing_completed_today": 4,
  "indexing_failed_today": 1,
  "questions_today": 213,
  "avg_latency_ms": 2840,
  "answers_without_citation": 3,
  "negative_feedback_rate": 0.08,
  "citation_type_distribution": {"direct": 78, "synthesis": 15, "inference": 5, "conflict": 2},
  "fallback_distribution": {"low_retrieval": 12, "low_generation_quality": 3, "inference_judge_rejected": 1},
  "routing_distribution": {"tenant_slm_lora": 65, "tenant_slm_no_lora": 20, "shared_llm": 15}
}
```

### 11. Tenant Configs API (ADR-009)

```http
GET   /api/{tenant_id}/admin/configs                    # 전체 효과적 config (defaults + overrides 합성)
GET   /api/{tenant_id}/admin/configs/{category}         # 카테고리별 (citation, retrieval, model, routing, lifecycle)
PATCH /api/{tenant_id}/admin/configs/{category}         # DB override 갱신
       Body: { "key": "verification.tier2.thresholds.strong", "value": 0.78, "reason": "..." }
POST  /api/{tenant_id}/admin/configs/reload             # 캐시 invalidate (수동)
GET   /api/{tenant_id}/admin/configs/{category}/history # tenant_config_change_logs
```

### 12. Prompt Studio API (ADR-009)

```http
GET   /api/{tenant_id}/admin/prompts                    # 목록 (task별)
GET   /api/{tenant_id}/admin/prompts/{task}             # 특정 task의 prompts (versions, ab_slots)
PATCH /api/{tenant_id}/admin/prompts/{task}/{version}/{ab_slot}
       Body: { "template": "...", "system": "..." }
POST  /api/{tenant_id}/admin/prompts/{task}/preview
       Body: { "template": "...", "sample_question": "..." }
       Response: { "rendered_prompt": "...", "sample_answer": "..." }
```

### 13. Routing Rules API (ADR-013)

```http
GET   /api/{tenant_id}/admin/routing                    # 현재 routing.yaml 내용
PUT   /api/{tenant_id}/admin/routing                    # 전체 yaml 교체 (lint·schema 검증 포함)
POST  /api/{tenant_id}/admin/routing/dryrun             # 시뮬레이션
       Body: { "sample_query": "...", "classifier_decision": {...} }
       Response: { "matched_rule": "...", "route": {...} }
```

### 14. LoRA Registry API (ADR-013)

```http
GET   /api/{tenant_id}/admin/lora                       # adapter 목록
POST  /api/{tenant_id}/admin/lora/upload                # multipart adapter weights upload
       Form: weights, metadata (JSON: tenant_id, version, base_model, training_meta)
       Response: { "adapter_id": "...", "status": "registered" }
POST  /api/{tenant_id}/admin/lora/{adapter_id}/activate # 활성화 (vLLM 등록 + tenant model.yaml 갱신)
POST  /api/{tenant_id}/admin/lora/{adapter_id}/retire   # 비활성화
DELETE /api/{tenant_id}/admin/lora/{adapter_id}         # 등록 해제 (active 상태면 거절)
```

### 15. Schema Editor API (ADR-015)

```http
GET   /api/{tenant_id}/input_schemas                    # 사용자도 호출 가능 (입력 폼 렌더용)
GET   /api/{tenant_id}/admin/schema                     # 관리자 — 전체 yaml + 메타
PUT   /api/{tenant_id}/admin/schema                     # 전체 yaml 교체 (backward compat 검증)
GET   /api/{tenant_id}/admin/schema/history             # tenant_input_schemas 테이블 이력
```

### 16. Evaluation API (ADR-009/013)

```http
GET   /api/{tenant_id}/admin/evaluation/datasets        # 평가셋 목록
POST  /api/{tenant_id}/admin/evaluation/run             # 평가 실행 트리거 (비동기 job)
       Body: { "config": {...}, "compare_with": "current" }
       Response: { "job_id": "..." }
GET   /api/{tenant_id}/admin/evaluation/jobs/{job_id}   # 진행 상황·결과
POST  /api/{tenant_id}/admin/evaluation/promote         # promotion_gate 통과 시 모델/prompt 승격
       Body: { "evaluation_job_id": "...", "target": "model|prompt|...", "version": "..." }
```

### 17. Assessment API (ADR-014, 모듈 활성 시)

```http
POST /api/{tenant_id}/assessment/extract
     Body: { "criteria": { "subject": "정보보안", "difficulty_distribution": {"easy": 3, "medium": 5, "hard": 2}, "exclude_recent_days": 90 } }
     Response: { "items": [...], "extracted_count": 10, "request_id": "..." }

POST /api/{tenant_id}/assessment/generate
     Body: { "criteria": { "subject": "...", "chapter": "...", "difficulty": "medium", "count": 5 }, "reference_strategy": "topk_similar" }
     Response: { "items": [...], "generated_count": 5, "validator_summary": {...}, "similarity_results": [...], "request_id": "..." }

POST /api/{tenant_id}/assessment/hybrid
     Body: { "extract": {...}, "generate": {...} }
     Response: { "items": [...], "extracted_count": 7, "generated_count": 3 }

GET  /api/{tenant_id}/admin/assessment/items            # 목록 (필터)
GET  /api/{tenant_id}/admin/assessment/items/{item_id}  # 상세
POST /api/{tenant_id}/admin/assessment/items            # 신규 등록 (input_schema 기반)
PATCH /api/{tenant_id}/admin/assessment/items/{item_id} # 수정
POST /api/{tenant_id}/admin/assessment/items/{item_id}/approve   # quality_status='approved' 전이
GET  /api/{tenant_id}/admin/assessment/review-queue     # quality_status='draft' 목록
GET  /api/{tenant_id}/admin/assessment/analytics        # used_count, distribution
```

### 18. Platform Admin API (`/api/platform/admin/*`)

```http
# Tenant Management
GET    /api/platform/admin/tenants
POST   /api/platform/admin/tenants                      # 신규 tenant 등록 (자동 collection 생성·configs 시드)
GET    /api/platform/admin/tenants/{tenant_id}
PATCH  /api/platform/admin/tenants/{tenant_id}          # status 전이 (active|suspended|archived)
DELETE /api/platform/admin/tenants/{tenant_id}/hard     # hard delete (ADR-012 cross-system 일관성)

# Cross-tenant Analytics (BYPASSRLS)
GET    /api/platform/admin/analytics/usage              # tenant별 사용량
GET    /api/platform/admin/analytics/health             # endpoint health 종합

# Platform Configs
GET    /api/platform/admin/configs/{category}           # platform/* yaml
PUT    /api/platform/admin/configs/{category}

# Endpoint Health
GET    /api/platform/admin/endpoints                    # vLLM 등 endpoint별 health
```

### 19. 공통 응답 컨벤션

#### Success
- 2xx + JSON body
- `X-Request-Id` 헤더 자동 부여

#### Validation 실패 (422)
```json
{
  "error": "validation_failed",
  "details": [
    { "field": "metadata.policy_id", "message": "필수 필드", "code": "required" }
  ]
}
```

#### Auth 실패 (401·403)
```json
{ "error": "tenant_mismatch|invalid_token|insufficient_role", "message": "..." }
```

#### Rate Limiting (429)
```json
{ "error": "rate_limited", "retry_after_seconds": 60 }
```

#### Server Error (5xx)
```json
{ "error": "internal_error|service_unavailable", "request_id": "...", "message": "운영자 문의" }
```

### 20. Pagination 컨벤션

- `page` (1-based), `page_size` (default 20, max 100)
- 응답: `{ "items": [], "total": N, "page": 1, "page_size": 20 }`

### 21. OpenAPI

FastAPI 자동 생성. `/docs` (Swagger UI), `/redoc`, `/openapi.json`. 모든 endpoint에 pydantic 모델 docstring 의무.

---

## Consequences

### 긍정적 영향

- 단일 ADR이 모든 endpoint — SPEC.md 폐기 후 API 단일 진실 소스
- ADR-008~015 신규 endpoint 모두 통합 (LoRA, Schema, Configs, Routing, Assessment, Lifecycle)
- URL path tenant mirror로 보안 가시성 ↑
- success/fallback 응답 분기로 chat 응답 계약 명확
- 4-type citation·routing·verifier 메타데이터가 chat 응답에 그대로 노출 → 운영 분석 가능

### 부정적 영향 / 부채

- Endpoint 수 60+ — 백엔드 구현 표면적 큼
- 각 endpoint의 pydantic 모델 + 검증 + 인증 미들웨어 + RLS context 주입이 일관 필요
- OpenAPI 자동 생성이 한국어 docstring 다루기 — 클라이언트 SDK 자동 생성 시 영향
- platform_admin과 tenant admin 권한 분리 거버넌스 운영 부담
- SSO 사양 미정 → 인증 미들웨어 추후 보완 필요

### 후속 작업

- FastAPI 프로젝트 구조: `app/api/{tenant_id}/...`, `app/api/platform/...`, `app/api/auth/...`
- pydantic 모델 정의 (request/response 모든 endpoint)
- 인증 미들웨어 (AuthAdapter Protocol → JWT verify → UserContext 주입 → tenant context SET)
- RLS context 주입 (PostgreSQL `SET LOCAL app.current_tenant`)
- OpenAPI 자동 생성 + 한국어 docstring
- API 테스트 자동화 (pytest + httpx)
- 에러 핸들러 통일 (FastAPI `exception_handler`)
- Rate limiting (slowapi 또는 자체)

---

## Alternatives Considered

### 1. tenant_id를 query param으로 (`/api/chat?tenant_id=...`)
- **장점**: URL 변형 적음
- **기각 사유**: ADR-008 path mirror 결정. 가시성 ↓.

### 2. GraphQL API
- **장점**: 클라이언트 query 자유도 ↑
- **기각 사유**: 학습 곡선·캐싱·폐쇄망 GraphQL 운영 부담. REST + OpenAPI가 표준 친화.

### 3. WebSocket for streaming
- **장점**: 양방향
- **기각 사유**: chat_streaming은 단방향 — SSE가 단순. WebSocket은 향후 admin 실시간 모니터링에 별도 검토.

### 4. /api/v1/... 버전 prefix
- **장점**: API versioning 명시
- **기각 사유**: 폐쇄망에서 API versioning 빈도 낮음. v1 도입 시 별도 ADR로 추가 가능.

### 5. Assessment endpoint를 chat 안에 통합 (`/chat`이 query_type으로 분기)
- **장점**: API 단순
- **기각 사유**: ADR-014가 본질적 분리 명시. assessment는 chat과 다른 응답 schema·UI.

---

## Related

- [ADR-002: Protocol/Adapter](./002-protocol-adapter-pattern.md) — AuthAdapter, ModelClient 등
- [ADR-005: Citation 신뢰 v1](./005-citation-trust-and-fallback.md) — superseded
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — URL path 정합·인증
- [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md) — Configs API
- [ADR-010: Citation Trust v2](./010-citation-trust-v2.md) — chat 응답 schema·Citation Inspector API
- [ADR-011: Hybrid Retrieval v2](./011-hybrid-retrieval-v2.md) — chat 응답의 retrieval metadata
- [ADR-012: Lifecycle v2](./012-lifecycle-v2.md) — Approval/Reindex/Hard Delete API
- [ADR-013: Model Routing](./013-model-routing-and-slm-llm-strategy.md) — chat sync/streaming, LoRA, Routing API
- [ADR-014: Assessment Workflow](./014-assessment-workflow.md) — Assessment API
- [ADR-015: Tenant Input Schema](./015-tenant-input-schema.md) — Schema Editor·Input Schema API
- [ADR-016: UI Architecture](./016-ui-architecture.md) — UI ↔ API 계약

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted (SPEC.md §8 흡수)
