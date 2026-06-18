# DomainRAG Ops

폐쇄망 GPU 서버 기반 **멀티테넌트 RAG 플랫폼**

## 프로젝트 개요

**DomainRAG Ops**는 테넌트별 입력 스키마로 도메인 지식을 구조화하고, 멀티 신호 RAG 검색과 SLM/LLM 라우팅으로 답변하며, **4-type claim-level citation**으로 근거를 설명하는 멀티테넌트 RAG 플랫폼입니다.

### 핵심 특징

- **멀티테넌트 first-class** — 보안·법무·시험·설비 등 도메인 별 입력 스키마와 정책
- **격리 3중 방어** — collection-per-tenant + PostgreSQL Row-Level Security + JWT/URL path mirror
- **4-type Citation** — direct / synthesis / inference / conflict (LLM-as-judge로 inference 검증)
- **SLM/LLM 라우팅** — Tenant SLM + LoRA per-request, Shared LLM fallback
- **Assessment 워크플로우** — 시험 문제 출제·생성·유사도·품질 검증 (RAG와 별도)
- **Configs 분리** — `configs/platform/*` + `configs/tenants/<id>/*` + DB overrides
- **Protocol/Adapter 패턴** — LLM·Embedder·Retriever·Reranker·VectorStore·Parser·AuthAdapter

### 설계 원칙

```
1. 모든 데이터·정책·검색에 tenant_id 1차 분기
2. 모든 답변에는 검증된 citation을 첨부
3. citation은 chunk/page/section 또는 item 단위
4. chunk metadata는 설명 가능성의 핵심
5. 모델·임베딩·검색기·reranker는 configs로 교체
6. 외부 API 의존 최소화 (폐쇄망 vLLM 기본)
7. MVP/Phase 분리 없음 — 완제품 단일 설계
```

자세한 원칙·금지사항·작업 체크리스트는 [CLAUDE.md](CLAUDE.md) 참조.

---

## 단일 진실 소스 — ADR

모든 설계 결정은 **ADR(Architecture Decision Record)**가 단일 진실 소스입니다.

| ADR | 주제 |
|---|---|
| [ADR-001](docs/adr/001-citation-metadata-design.md) | Citation 메타데이터 스키마 |
| [ADR-002](docs/adr/002-protocol-adapter-pattern.md) | Protocol/Adapter 패턴 |
| [ADR-003](docs/adr/003-langraph-orchestration.md) | LangGraph 오케스트레이션 |
| [ADR-004](docs/adr/004-security-acl-model.md) | 보안 & ACL 모델 |
| [ADR-005](docs/adr/005-citation-trust-and-fallback.md) | Citation 신뢰 v1 (Superseded by ADR-010) |
| [ADR-006](docs/adr/006-hybrid-retrieval.md) | Hybrid Retrieval v1 (Superseded by ADR-011) |
| [ADR-007](docs/adr/007-document-chunk-lifecycle.md) | Lifecycle v1 (Superseded by ADR-012) |
| [ADR-008](docs/adr/008-multi-tenant-architecture.md) | Multi-Tenant Architecture |
| [ADR-009](docs/adr/009-tenant-control-plane.md) | Tenant Control Plane |
| [ADR-010](docs/adr/010-citation-trust-v2.md) | **Citation Trust v2** (4-type) |
| [ADR-011](docs/adr/011-hybrid-retrieval-v2.md) | **Hybrid Retrieval v2** (multi-tenant) |
| [ADR-012](docs/adr/012-lifecycle-v2.md) | **Lifecycle v2** (multi-tenant + complete) |
| [ADR-013](docs/adr/013-model-routing-and-slm-llm-strategy.md) | Model Routing & SLM/LLM |
| [ADR-014](docs/adr/014-assessment-workflow.md) | Assessment Workflow |
| [ADR-015](docs/adr/015-tenant-input-schema.md) | Tenant Input Schema |
| [ADR-016](docs/adr/016-ui-architecture.md) | UI Architecture |
| [ADR-017](docs/adr/017-api-specification.md) | API Specification |
| [ADR-018](docs/adr/018-sso-integration-authfusion.md) | SSO Integration with AuthFusion |
| [ADR-019](docs/adr/019-infrastructure-sharing.md) | Infrastructure Sharing & Resource Allocation |
| [ADR-020](docs/adr/020-pii-and-audit-integration.md) | PII Detection & Audit Integration |

ADR 인덱스: [docs/adr/README.md](docs/adr/README.md)

---

## 프로젝트 구조

```
domainrag-ops/
├── README.md                          # 본 파일
├── CLAUDE.md                          # 프로젝트 compass (절대 원칙·금지·완료 기준)
│
├── docs/
│   ├── README.md
│   └── adr/                           # 설계 결정 단일 진실 소스 (ADR-001 ~ 017)
│       ├── README.md
│       └── 0NN-*.md
│
├── frontend/                          # Next.js (App Router)
│   ├── app/
│   │   ├── [tenantId]/
│   │   │   ├── chat/
│   │   │   └── admin/
│   │   │       ├── dashboard/
│   │   │       ├── documents/
│   │   │       ├── indexing/
│   │   │       ├── logs/
│   │   │       ├── citation-inspector/
│   │   │       ├── routing/
│   │   │       ├── prompts/
│   │   │       ├── lora/
│   │   │       ├── schema/
│   │   │       ├── evaluation/
│   │   │       ├── configs/
│   │   │       └── assessment/
│   │   ├── platform/admin/
│   │   └── layout.tsx
│   ├── components/                    # AnswerCard, CitationPanel, ConflictBox, ...
│   └── lib/
│
├── backend/                           # FastAPI
│   ├── app/
│   │   ├── api/
│   │   │   ├── tenant/                # /api/{tenant_id}/*
│   │   │   ├── platform/              # /api/platform/admin/*
│   │   │   └── auth/
│   │   ├── services/
│   │   ├── models/                    # pydantic + ORM
│   │   └── core/
│   │       ├── auth_adapter.py        # AuthAdapter Protocol
│   │       ├── tenant_resolver.py
│   │       ├── tenant_config_service.py
│   │       └── rls.py                 # PostgreSQL Row-Level Security helpers
│   ├── alembic/                       # schema 진화 migration (ADR-012)
│   └── tests/
│
├── packages/rag_core/                 # RAG 핵심 라이브러리
│   ├── interfaces/                    # Protocol 정의 (ADR-002)
│   ├── implementations/
│   │   ├── embedders/
│   │   ├── retrievers/
│   │   ├── rerankers/
│   │   ├── vector_stores/
│   │   ├── parsers/
│   │   └── llm_clients/
│   ├── workflows/                     # LangGraph (ADR-003 + Gate 1/2)
│   └── citation/                      # 4-type verifier (ADR-010)
│
├── infra/
│   ├── docker-compose.yml             # PostgreSQL, Qdrant, MinIO, vLLM (tenant + shared)
│   └── scripts/
│       ├── tenant_register.sh         # 새 tenant 자동 등록 (ADR-008)
│       ├── tenant_backup.sh           # ADR-012
│       └── tenant_restore.sh
│
├── configs/
│   ├── platform/                      # 모든 tenant 기본값 (ADR-009)
│   │   ├── prompts/
│   │   ├── retrieval.yaml
│   │   ├── citation.yaml
│   │   ├── routing.yaml
│   │   ├── model.yaml
│   │   ├── lifecycle.yaml
│   │   ├── assessment.yaml
│   │   ├── query_classifier.yaml
│   │   └── common_fields.yaml
│   └── tenants/
│       └── <tenant_id>/               # tenant 고유 설정 (overrides + input_schema)
│           ├── input_schema.yaml
│           ├── ui_schema.yaml
│           ├── overrides.yaml
│           ├── model.yaml
│           └── prompts/
│
└── data/
    └── eval/
        ├── platform/                  # 모든 tenant 공통 smoke test
        └── tenants/<tenant_id>/       # tenant별 평가셋 + promotion_gate.yaml
```

---

## 빠른 시작

### 사전 요구사항

- Docker & Docker Compose
- Python 3.10+
- Node.js 18+
- PostgreSQL 14+ (또는 Docker)
- Qdrant 1.10+ (또는 Docker)
- 폐쇄망 vLLM 0.6+ 인스턴스 (multi-LoRA 지원)

### 개발 환경 설정

```bash
git clone <repository-url>
cd domainrag-ops

cp .env.example .env

# Docker Compose로 인프라 기동 (PostgreSQL, Qdrant, MinIO, vLLM)
docker-compose up -d

# Alembic 초기 schema (ADR-012)
cd backend
alembic upgrade head

# Backend
poetry install
poetry run uvicorn app.main:app --reload --port 8000

# Frontend (다른 터미널)
cd frontend
npm install
npm run dev
```

### 첫 tenant 등록

```bash
# 자동화 스크립트 (Qdrant collection 생성·configs 시드·MinIO prefix 초기화)
./infra/scripts/tenant_register.sh security \
    --display-name "보안팀" \
    --domain-type security \
    --embedding-model bge-m3 \
    --modules rag

# IdP에 tenant role 등록은 SSO 운영자 수동
```

### 접속

- 사용자 채팅: `http://localhost:3010/{tenant_id}/chat`
- Tenant Admin: `http://localhost:3010/{tenant_id}/admin/dashboard`
- Platform Admin: `http://localhost:3010/platform/admin/tenants`
- API 문서 (OpenAPI): `http://localhost:8000/docs`

---

## 주요 컴포넌트

### Frontend (Next.js · ADR-016)

- `/{tenant_id}/chat` — 사용자 채팅 (chat_structured / chat_streaming)
- `/{tenant_id}/admin/*` — Tenant Admin 13개 메뉴
- `/platform/admin/*` — platform_admin 4개 메뉴

### Backend (FastAPI · ADR-017)

- `/api/{tenant_id}/chat` — sync chat (citation 포함)
- `/api/{tenant_id}/chat/stream` — SSE streaming
- `/api/{tenant_id}/admin/*` — 60+ admin endpoints
- `/api/platform/admin/*` — platform 운영
- `/api/auth/*` — JWT 인증

### RAG Core (`packages/rag_core`)

- **Interfaces**: Protocol 정의 (ADR-002)
- **LangGraph Workflow**: tenant_resolver → classify → router → retrieve → Gate 1 → generate → verify → Gate 2 → save_log
- **4-type Citation Verifier**: Tier 1/2/3 + LLM-as-judge for inference (ADR-010)

### Infrastructure (ADR-019)

- **PostgreSQL 16** (115번 기존 인스턴스, 별도 `domainrag` database) — RLS·BYPASSRLS role 분리, chat_logs 시간 partitioning
- **Qdrant 1.10+** — collection-per-tenant (`chunks_<tenant_id>`, `items_<tenant_id>`), named vectors, DBSF fusion
- **MinIO** — prefix-per-tenant 원본 보관
- **vLLM** — 174번 통합 인스턴스: Qwen2.5-7B-Instruct-AWQ (Shared LLM·Tenant SLM 겸용 + multi-LoRA per-request). 설계상 별도 14B Shared LLM은 GPU 부족으로 운영 불가 → 7B 단일 (ADR-019)
- **Embedding/Reranker** (174번 GPU 3) — bge-m3 + bge-reranker-v2-m3
- **AuthFusion** (외부 의존) — OIDC SSO + KeyHub (LoRA·secrets) + Ledger (보안 audit)
- **Ollama** (174번 GPU 0, 공유) — Qwen Vision (OCR·일부 fallback)

---

## 절대 금지사항 (요약)

자세한 사항은 [CLAUDE.md](CLAUDE.md) 참조.

- `tenant_id` 빠뜨리지 말 것
- citation을 chunk_id/item_id 없이 만들지 말 것
- `support_level`/`verified` placeholder 하드코딩 금지
- inference citation을 LLM-as-judge 없이 노출 금지
- 모델명·임계치를 코드에 하드코딩 금지 (configs)
- ACL filter 없이 검색 금지 (RLS는 안전망)
- RAG와 Assessment를 같은 데이터 모델로 처리 금지
- "MVP"/"Phase 2"/"추후 검토" 표현 금지 — 완제품 단일 설계
- 외부 LLM API를 기본값으로 사용 금지
- 테스트 없이 핵심 모듈 구현 금지

---

## 기여 워크플로우

새 기능·결정이 필요하면:

1. 관련 [ADR](docs/adr/)을 먼저 읽는다
2. 결정 변경이면 새 ADR 작성 (`docs/adr/NNN-...`), 기존은 `Superseded by ADR-NNN`로 Status 변경
3. 코드는 ADR을 참조 (`# see ADR-010 §4`)
4. 테스트: 단위 + RLS 통합 + 4-type citation 분기

---

## 라이선스

MIT
