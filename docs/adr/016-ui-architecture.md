# ADR-016: UI Architecture — Routes, Components, Wireframes

## Status
**Accepted** (2026-05-08)

> 본 ADR은 SPEC.md §5의 모든 화면 명세를 흡수하며, ADR-008~015가 추가한 신규 화면(Tenant Management, Schema Editor, Citation Inspector, Routing Console, Assessment Console, Evaluation Console)을 통합한다. SPEC.md 폐기 후 UI 단일 진실 소스.

---

## Context

### 배경 (사실)

- 비전 §5는 사용자 채팅 + 관리자 콘솔 8개 메뉴를 sketch.
- ADR-009~015 진행으로 신규 메뉴 5개 추가 요구 (Tenant Management, Schema Editor, Citation Inspector, Routing Console, Assessment Console).
- ADR-013이 chat_structured / chat_streaming 두 UI 모드 도입.
- ADR-010이 4-type citation 시각화 컨벤션 정의.
- ADR-014가 Assessment 별도 도메인 — 관리자 화면 분기 필요.
- ADR-015가 dynamic form (react-jsonschema-form) 채택.
- 본 프로젝트 정책: 완제품 단일 설계.

### 가정

- Next.js 14+ App Router 사용
- React 18+, TypeScript
- Tailwind CSS + shadcn/ui (또는 동급 디자인 시스템)
- react-jsonschema-form 또는 동급 (ADR-015)
- Monaco editor (Schema YAML editor용)
- SSE 클라이언트 (chat_streaming용)
- 한국어 단일 언어. 다국어(i18n)는 본 시스템에서 지원하지 않는다(필요 시 새 ADR 작성).

가정 깨지면 재검토 — 특히 디자인 시스템 변경 시 컴포넌트 사양 영향.

---

## Decision

### 1. URL 구조 — Tenant Path Mirror (ADR-008 정합)

```
/                                # 랜딩 페이지 (테넌트 선택 또는 SSO 리다이렉트)
/{tenant_id}/chat                # 사용자 채팅 (도메인 모듈에 따라)
/{tenant_id}/admin               # 관리자 콘솔 진입점
/{tenant_id}/admin/dashboard
/{tenant_id}/admin/documents
/{tenant_id}/admin/documents/upload
/{tenant_id}/admin/documents/{doc_id}
/{tenant_id}/admin/indexing
/{tenant_id}/admin/logs/chat
/{tenant_id}/admin/logs/chat/{request_id}
/{tenant_id}/admin/citation-inspector
/{tenant_id}/admin/configs                # 카테고리별 편집
/{tenant_id}/admin/prompts                # Prompt Studio
/{tenant_id}/admin/routing                # Routing Rules Editor
/{tenant_id}/admin/lora                   # LoRA Adapter Registry
/{tenant_id}/admin/schema                 # Tenant Input Schema Editor
/{tenant_id}/admin/evaluation             # Evaluation Console
/{tenant_id}/admin/assessment             # Assessment Console (assessment 모듈 활성 시)
/{tenant_id}/admin/assessment/items
/{tenant_id}/admin/assessment/generate
/{tenant_id}/admin/assessment/review-queue
/platform/admin                            # platform_admin 전용
/platform/admin/tenants                    # Tenant Management
/platform/admin/endpoints                  # Endpoint Health Dashboard
/platform/admin/analytics/usage            # Cross-tenant 사용량
/platform/admin/health                     # Process 운영 metrics (ledger/chat_log 실패 등, ADR-021 §6)
/platform/admin/configs                    # Platform-level configs

# 인증 불필요 (ADR-021 §6)
/api/health/live                           # k8s liveness probe — 항상 200
/api/health/ready                          # k8s readiness probe — DB+migration 검사
/api/health                                # legacy 단축형 (ADR-017 §1)
```

JWT claim의 `tenant_id`와 URL path의 `{tenant_id}`가 mismatch면 403 리다이렉트. `/platform/admin/*`은 `platform_admin` role만. `/api/health/*`는 인증 면제 (k8s 인프라용).

### 2. 사용자 채팅 화면 (`/{tenant_id}/chat`)

#### Layout

```
┌─────────────────────────────────────────────────────────────┐
│ DomainRAG Ops · {tenant_display_name}      [모드▾] [사용자] │
├──────────────┬─────────────────────────────┬────────────────┤
│ 대화 목록     │ 채팅 영역                   │ Citation Panel │
│              │                              │                │
│ + 새 대화     │ 사용자: 내부망 반출 절차?    │ [1] direct     │
│              │                              │ 보안매뉴얼.pdf │
│ 최근 대화     │ AI:                          │ p.12           │
│ - 보안 절차   │ 내부망 반출은... [1]        │ §반출절차      │
│ - 계정 정책   │ 승인 전 반출은... [2]        │ ───────        │
│              │ 긴급 절차는 명시되지 ⚠      │ support: strong│
│              │                              │ s = 0.82       │
│              │ [좋아요] [별로] [복사]       │ rerank = 0.91  │
│              │                              │                │
│              │ ┌────────────────────────┐  │ 원문 발췌:      │
│              │ │ 질문 입력...      [▶]  │  │ "내부망 자료..."│
│              │ └────────────────────────┘  │                │
│              │                              │ [원문 열기]    │
│              │ 모드: ◯ structured ◯ streaming                │
└──────────────┴─────────────────────────────┴────────────────┘
```

#### 컴포넌트

- `ConversationList.tsx` — 좌측 대화 목록
- `ChatArea.tsx` — 중앙 채팅 영역
- `MessageInput.tsx` — 하단 입력창
- `AnswerCard.tsx` — AI 답변 카드 (4-type 마커 렌더링)
- `CitationPanel.tsx` — 우측 citation 상세
- `ModeSelector.tsx` — chat_structured / chat_streaming 토글
- `ConflictBox.tsx` — citation_type=conflict일 때 별도 박스
- `InferenceJudgePopover.tsx` — citation_type=inference 호버 시 LLM judge 결과

#### 4-type 마커 렌더링 (ADR-010 컨벤션)

| 타입 | 본문 표기 | UI 처리 |
|---|---|---|
| direct | `...절차이다. [1]` | `[1]` 클릭 → CitationPanel |
| synthesis | `...필요합니다. [종합: 1,2,3]` | 마커 그룹 클릭 → 패널에 3 citation 동시 표시 |
| inference | `...해석됩니다. [추론: 1,2,3] 🔍` | `🔍` 호버 → InferenceJudgePopover (LLM reasoning + caveat) |
| conflict | `기준이 다릅니다. [충돌: 1 vs 2]` | ConflictBox로 별도 렌더 (양측 비교) |

`support_level`:
- `strong`: 일반 표시
- `medium`: 마커 그대로 + 호버에 ⚠ tooltip ("의미 유사도 일부 약함")
- `weak`: 응답 단계에서 제거되어 미등장 (ADR-010)
- unsupported segment: 문장 끝 ⚠ 인라인

#### Streaming 모드

`chat_streaming` 선택 시 `/api/{tenant_id}/chat/stream` SSE 호출. citation 비활성. token-by-token 렌더. 빠른 자유 대화·요약.

#### Fallback 응답 렌더링

```
근거가 부족합니다.
fallback_reason: low_retrieval

근접한 후보 (참고용, 직접 인용 아님):
- 출입 통제 지침.pdf p.5 §외부인 방문 절차 (관련도 0.55)

다음 시도해 보세요:
- 더 구체적인 키워드로 다시 질문
- 보안팀에 직접 문의
```

### 3. 관리자 콘솔 — 메뉴 구성

#### Sidebar (왼쪽 nav)

```
[Tenant: security ▾]
─────────────────
대시보드
─────────────────
지식 운영
  ├─ 문서 관리
  ├─ 문서 업로드
  ├─ 인덱싱 모니터링
  └─ Schema Editor
─────────────────
Assessment (모듈 활성 시)
  ├─ Item Bank
  ├─ Generation Workbench
  └─ Quality Review Queue
─────────────────
질의 운영
  ├─ Chat Logs
  └─ Citation Inspector
─────────────────
모델·라우팅
  ├─ Routing Rules
  ├─ Prompt Studio
  └─ LoRA Registry
─────────────────
평가
  └─ Evaluation Console
─────────────────
설정
  └─ Tenant Configs
```

`tenant.domain_modules`에 따라 Assessment 섹션 표시.

`platform_admin`은 Sidebar 상단에 `[Platform ▾]` 토글로 platform 메뉴 진입.

#### RBAC 메뉴 매핑 (CLAUDE.md Y9)

frontend RBAC 미들웨어(`/{tenant_id}/admin/*` route guard)가 다음 표대로 접근을 강제한다. 백엔드는 endpoint 단위로 동일 정책을 재검증 — *두 layer 모두 의무*다.

| 메뉴 | USER | ADMIN | PLATFORM_ADMIN |
|---|:---:|:---:|:---:|
| `/{tenant_id}/chat` | ✓ | ✓ | ✓ |
| `/{tenant_id}/admin/dashboard` |  | ✓ | ✓ |
| `/{tenant_id}/admin/documents` |  | ✓ | ✓ |
| `/{tenant_id}/admin/indexing` |  | ✓ | ✓ |
| `/{tenant_id}/admin/schema` |  | ✓ | ✓ |
| `/{tenant_id}/admin/assessment/*` (모듈 활성 시) |  | ✓ | ✓ |
| `/{tenant_id}/admin/logs/chat` |  | ✓ | ✓ |
| `/{tenant_id}/admin/citation-inspector` |  | ✓ | ✓ |
| `/{tenant_id}/admin/routing` |  | ✓ | ✓ |
| `/{tenant_id}/admin/prompts` |  | ✓ | ✓ |
| `/{tenant_id}/admin/lora` |  | ✓ | ✓ |
| `/{tenant_id}/admin/evaluation` |  | ✓ | ✓ |
| `/{tenant_id}/admin/configs` |  | ✓ | ✓ (+ restricted_to keys) |
| `/platform/admin/tenants` |  |  | ✓ |
| `/platform/admin/endpoints` |  |  | ✓ |
| `/platform/admin/analytics/*` |  |  | ✓ |
| `/platform/admin/configs/*` |  |  | ✓ |

#### Lifecycle 비활성 표기 (CLAUDE.md Y1)

`documents.approval_status='archived'` (ADR-012)와 `assessment_items.quality_status='retired'` (ADR-014)는 의미가 같다 — "더 이상 검색·출제 대상이 아니지만 row는 보존된 비활성 상태." UI는 두 상태 모두 동일 칩(badge)으로 "**비활성**" 표기 + 회색 톤. 내부 status 문자열은 보존(API·로그)하지만 사용자 노출은 통일한다. 코드에서 둘을 같이 다룰 때는 `is_inactive(row)` 헬퍼 사용.

#### 3.1 대시보드 (`/admin/dashboard`)

KPI 카드 + 차트:

```
┌─ KPI ─────────────────────────────────────────────────────┐
│ 총 문서 128 │ 총 chunk 18,420 │ 오늘 업로드 5 │ 인덱싱 실패 1│
│ 오늘 질문 213 │ 평균 응답 2.84s │ Citation 없음 3 │ 부정 8% │
└────────────────────────────────────────────────────────────┘

┌─ Citation Type 분포 (오늘) ─┐  ┌─ Fallback Reason 분포 ────┐
│ direct      ████████ 78%   │  │ low_retrieval     ▓▓▓ 12% │
│ synthesis   ███ 15%        │  │ low_gen_quality   ▓ 3%    │
│ inference   ▓ 5%           │  │ inference_reject  ▓ 1%    │
│ conflict    ▓ 2%           │  │ ...                       │
└────────────────────────────┘  └────────────────────────────┘

┌─ Routing Decision 분포 ────┐  ┌─ 모델 사용량 ──────────────┐
│ tenant_slm + lora   65%    │  │ tenant_slm: 850 calls     │
│ tenant_slm no lora  20%    │  │ shared_llm:  130 calls    │
│ shared_llm          15%    │  │ inference_judge: 45 calls │
└────────────────────────────┘  └────────────────────────────┘
```

#### 3.2 문서 관리 (`/admin/documents`)

- 검색·필터·페이지네이션
- 컬럼: 문서명·input_type·부서·보안등급·버전·chunk수·인덱싱상태·마지막색인일·상태(approved/draft/archived)·액션
- 행 클릭 → 상세
- 액션: 재색인(4-mode 선택), 비활성화(soft delete), 하드 삭제(platform_admin)

상세 페이지 (`/admin/documents/{doc_id}`):
- metadata 보기/편집 (input_type schema 기반 동적 폼)
- chunk 목록 (페이지·섹션·내용 미리보기)
- 인덱싱 이력
- 사용 이력 (이 문서에서 인용된 chat_logs)

#### 3.3 문서 업로드 (`/admin/documents/upload`)

- input_type 선택 dropdown (tenant 활성 input_types)
- 선택 후 동적 폼 (react-jsonschema-form, ADR-015)
- 파일 첨부 (multipart)
- 업로드 → indexing_jobs 추적 패널 자동 표시 (단계별 progress)

#### 3.4 인덱싱 모니터링 (`/admin/indexing`)

- 활성 + 완료 + 실패 jobs 탭
- 컬럼: job_id·문서명·input_type·status·progress·step·시작·종료·오류
- failed_chunks JSONB 상세 (어느 chunk가 왜 실패)
- 재시도 버튼 (chunk 단위 또는 전체)

#### 3.5 Chat Logs (`/admin/logs/chat`)

- 필터: 사용자·날짜·status(success/fallback)·citation_type·confidence 범위
- 컬럼: 시간·사용자·질문·답변요약·citation수·confidence·ui_mode·routing matched_rule·feedback
- 행 클릭 → 상세

상세 (`/admin/logs/chat/{request_id}`):
- 원본 질문 + rewritten_query (있으면)
- 최종 answer (4-type 마커 그대로)
- retrieved_chunks (dense_score, sparse_score, fused_score, rerank_score)
- citations (claim_text, support_level, verifier 결과)
- routing_decision JSON
- model_failure_chain (실패 이력)
- inference_judge_results (inference type 있을 때)
- conflict_groups (conflict type 있을 때)
- prompt_version 식별자
- 모든 latency 단계별

#### 3.6 Citation Inspector (`/admin/citation-inspector`) — ADR-010 신설

- 필터: 날짜·type·support_level·tenant
- 메인 차트: 4-type 분포 트렌드
- segment-by-segment 분석 — claim ↔ chunk 의미 정합 시각화
- inference 답변의 LLM judge result 검토
- conflict 답변의 양측 비교 뷰
- 운영자가 "재검증" 트리거 가능 (Tier 2 재계산)

#### 3.7 Routing Rules Editor (`/admin/routing`) — ADR-013 신설

- yaml editor (Monaco)
- "Dry run" 패널 — sample query 입력 → 룰 평가 결과 즉시 시뮬레이션
- 변경 저장 시 schema 검증 + LISTEN/NOTIFY broadcast

#### 3.8 Prompt Studio (`/admin/prompts`) — ADR-009/010 신설

- task별 prompt 목록 (chat_answer, query_classify, inference_judge, ...)
- 좌측 prompt body 편집 (Monaco), 우측 sample query로 응답 미리보기
- ab_slot (control/treatment) 비교 뷰
- 버전 history

#### 3.9 LoRA Registry (`/admin/lora`) — ADR-013 신설

- adapter 목록 (active/registered/retired 탭)
- upload 폼 (multipart weights + manifest)
- 활성화/rollback 액션
- training_metadata 확인

#### 3.10 Schema Editor (`/admin/schema`) — ADR-015 신설

- input_types 목록 (active/deprecated/archived)
- 좌측 YAML editor (Monaco), 우측 dynamic form 미리보기 (react-jsonschema-form)
- "Form Builder" 탭 — GUI로 필드 추가/제거 → YAML 자동 생성
- backward compat 검증 결과 inline
- deprecation 일정 표시

#### 3.11 Evaluation Console (`/admin/evaluation`) — ADR-009/013 신설

- promotion_gate.yaml 편집
- 평가 실행 트리거 (현재 모델·prompt 조합 vs proposed)
- 결과 비교 (recall, citation_accuracy, unsupported_ratio, fallback_rate)
- promote/reject 버튼

#### 3.12 Tenant Configs (`/admin/configs`) — ADR-009 신설

- 카테고리 탭 (citation, retrieval, model, routing, lifecycle)
- DB override 편집 (filesystem defaults는 읽기 전용 표시)
- diff preview ("이 값은 platform default에서 X였음, 지금은 Y로 override")
- 변경 이력 (tenant_config_change_logs)

#### 3.13 Assessment Console (`/admin/assessment`) — ADR-014 신설 (모듈 활성 시)

- **Item Bank** (`/items`): subject·chapter·difficulty 필터, used_count 정렬, item 상세
- **Item Editor**: input_schema 기반 동적 폼 (assessment_item type)
- **Generation Workbench** (`/generate`): criteria 입력 → similarity check 결과 → generation 실행 → quality validator 결과 → 승인/폐기
- **Quality Review Queue** (`/review-queue`): quality_status='draft' items 검토 + 일괄 승인/폐기
- **Usage Analytics**: subject/chapter/difficulty별 used_count 분포

### 4. Platform Admin 전용 (`/platform/admin/*`)

- **Tenant Management**: tenant CRUD, status 전이, 등록 자동화 트리거, hard delete
- **Endpoint Health Dashboard**: vLLM별 health·latency·동시성·실패율 (Grafana 임베드 또는 자체 차트)
- **Platform Configs**: platform/* yaml 편집 (모든 tenant 영향, 배포 단계로 적용)
- **Cross-tenant Analytics**: BYPASSRLS로 통계 (검색 결과 노출 아님)

### 4.5 로그인 착지 + 로그아웃 (ADR-022 보강, 2026-05-28)

- **역할-aware 착지**: 로그인 후 목적지는 역할로 결정한다(단일 진실 소스 `postLoginDestination`). `platform_admin → /platform/admin/tenants`, `admin·auditor → /{domain}/admin/dashboard`, `user → /{domain}/chat`. 콜백·`/console`이 모두 이 함수로 수렴 — 관리자를 채팅으로 강제 경유시키지 않는다(이전엔 콜백이 무조건 `/{domain}/chat`로 보냄). 채팅은 관리 화면의 명시 링크("관리자 콘솔 →"/도메인 칩)로 접근 유지.
- **로그아웃 = SP + IdP 둘 다 종료** (OIDC RP-Initiated Logout): SP 쿠키 삭제 + 토큰 revoke + AuthFusion `end_session_endpoint`로 이동(`id_token_hint` 필수). SP 쿠키만 지우면 IdP SSO 세션이 살아 `/console` 재진입 시 silent 재인증되어 "로그아웃이 안 되는" 것처럼 보임. `post_logout_redirect_uri`는 IdP에 사전 등록된 경우에만 첨부(`post_logout_redirect_registered`).
- **계정 전환**(switchAccount)은 `authorize?prompt=login` — 활성 SSO 세션이 있어도 재인증 강제.
- auditor는 콘솔 read-only 접근(전역). 쓰기 endpoint는 backend `require_admin`이 403으로 차단.

### 5. 응답 상태별 UX

| 상태 | UX |
|---|---|
| Loading | spinner + "답변 생성 중..." (chat_structured 1-3초) 또는 streaming 텍스트 흐름 |
| Success | AnswerCard + citations |
| Fallback | 회색 박스 + 안내문 + near_misses + suggested_actions |
| Error (네트워크 등) | 토스트 + 재시도 버튼 |
| 403 (tenant mismatch) | 풀 화면 에러 + 로그아웃 옵션 |
| 503 (Qdrant unavailable) | 토스트 + 재시도 + retry_after 카운트다운 |

### 6. 모드 전환 (ADR-013)

ChatArea의 ModeSelector:
- 사용자가 명시 선택 가능 (`chat_streaming` / `chat_structured`)
- 라우팅 룰의 ui_mode가 자동 매핑하지만 사용자 override 가능
- structured 선택 시 citation 패널 활성, streaming 선택 시 비활성 + "citation 비활성" 안내

### 7. 디자인 토큰 (Tailwind 기준)

- Primary: 보안·법무·시험 도메인별 tenant_display 색상 가능 (configs.tenants.<id>.theme.primary)
- support_level 색상:
  - strong: green-600
  - medium: yellow-600 (호버 ⚠)
  - weak: 표시 안 됨
- citation_type 색상:
  - direct: blue-600
  - synthesis: purple-600
  - inference: orange-600 (🔍)
  - conflict: red-600 (양측 박스)
- fallback: gray-500 박스

### 8. 동적 폼 (react-jsonschema-form, ADR-015)

- JSON Schema → 자동 폼
- UI Schema (별도 yaml) → 표시 방식
- field types: text, textarea, date, select, multi-select, checkbox, integer, range, file upload, JSON object (nested)
- Validation 메시지 한국어 (i18n bundle)
- 필수 필드 시각 강조

### 9. 접근성 (a11y)

- 모든 인터랙티브 요소 키보드 접근
- 스크린 리더 ARIA 라벨
- 색상 대비 WCAG AA
- citation marker는 텍스트 + 시각 둘 다 (색맹 대응)

---

## Consequences

### 긍정적 영향

- 단일 ADR이 모든 화면 명세 — SPEC.md 폐기 후 UI 단일 진실 소스
- Tenant URL path mirror로 보안 가시성 ↑ (사용자가 자신이 어느 tenant에 있는지 명확)
- 4-type citation·streaming/structured 모드 등 ADR-010/013 결정이 UI 컨벤션으로 구체화
- 신규 메뉴(Citation Inspector·Schema Editor·Routing·LoRA·Evaluation·Assessment) 모두 day 1 구현 명세

### 부정적 영향 / 부채

- 메뉴 13개 + 사용자 채팅 + platform_admin 4개 = 화면 18개 — frontend 작업량 큼
- 각 화면이 ADR-009~015의 admin API에 의존 — 백엔드 우선 일정 또는 mock API
- 디자인 시스템 통일성 책임 — 기본 shadcn/ui로 시작하지만 도메인별 커스터마이징 가능
- 한국어 단일 가정 — 다국어 요구 시 i18n 별도 작업

### 후속 작업

- Frontend 디렉터리 구조: `app/[tenantId]/chat/`, `app/[tenantId]/admin/...`, `app/platform/admin/...`
- 컴포넌트 라이브러리 셋업 (shadcn/ui + Monaco + react-jsonschema-form + SSE 클라이언트)
- 디자인 토큰 시드 (Tailwind config + tenant theme override)
- 라우팅 가드 (JWT claim ↔ URL mismatch 차단)
- 화면별 mock API → 실제 API 연동 (ADR-017과 정합)

---

## Alternatives Considered

### 1. Tenant 정보를 URL이 아닌 Header만으로
- **장점**: URL 단순
- **기각 사유**: ADR-008 3중 방어가 URL mirror 요구. 사용자가 자신의 tenant 가시성 ↓.

### 2. Subdomain (`security.domainrag.local`)
- **장점**: DNS 분리 가시화
- **기각 사유**: ADR-008에서 기각된 이유 동일 — 폐쇄망 DNS 부담.

### 3. Admin과 사용자 도메인 분리 (`admin.domainrag.local`)
- **장점**: 권한 분리 명확
- **기각 사유**: SPA 라우팅으로 충분, 도메인 분리 부담 ↑.

### 4. Streaming 단일 모드 (structured 제거)
- **장점**: UX 일관
- **기각 사유**: ADR-010 citation 검증이 sync 필요. 두 모드가 trade-off 균형.

### 5. 4-type citation을 metadata로만 (시각 분리 없음)
- **장점**: 가독성 ↑
- **기각 사유**: ADR-010 원칙 8 재해석에서 명시 — 사용자가 [추론] 인지해야 caveat 효과.

---

## Related

- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — URL path 정합
- [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md) — Configs Editor·Prompt Studio·Evaluation Console
- [ADR-010: Citation Trust v2](./010-citation-trust-v2.md) — 4-type marker 렌더링·Citation Inspector
- [ADR-011: Hybrid Retrieval v2](./011-hybrid-retrieval-v2.md) — Reranker bypass UI 표현
- [ADR-012: Lifecycle v2](./012-lifecycle-v2.md) — 4-mode reindex UI
- [ADR-013: Model Routing](./013-model-routing-and-slm-llm-strategy.md) — ModeSelector·Routing Editor·LoRA Registry
- [ADR-014: Assessment Workflow](./014-assessment-workflow.md) — Assessment Console
- [ADR-015: Tenant Input Schema](./015-tenant-input-schema.md) — react-jsonschema-form·Schema Editor
- ADR-017 (예정): API Specification — UI ↔ API 계약

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted (SPEC.md §5 흡수)
