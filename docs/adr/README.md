# ADR (Architecture Decision Records)

프로젝트 설계 결정을 기록하고 공유하기 위한 문서들

---

## ADR 개요

각 ADR은 다음 구조를 따릅니다:

```
# ADR-NNN: 제목

## Status
- Proposed / Accepted / Deprecated / Superseded

## Context
문제 상황과 배경

## Decision
선택한 해결책

## Consequences
결과 (긍정적, 부정적 영향)

## Alternatives Considered
고려했던 다른 방법들

## Related
관련 ADR 또는 문서
```

---

## 현재 ADR 목록

### [ADR-001: Citation 메타데이터 설계](./001-citation-metadata-design.md)
**Status**: Accepted  
**주제**: Chunk 단위 메타데이터로 설명 가능한 AI 구현

### [ADR-002: Protocol/Adapter 패턴](./002-protocol-adapter-pattern.md)
**Status**: Accepted  
**주제**: 모든 RAG 컴포넌트를 교체 가능하게 설계

### [ADR-003: LangGraph 오케스트레이션](./003-langraph-orchestration.md)
**Status**: Accepted  
**주제**: RAG 워크플로우 관리 방식

### [ADR-004: 보안 & ACL 모델](./004-security-acl-model.md)
**Status**: Accepted  
**주제**: 폐쇄망 환경에서의 권한 관리

### [ADR-005: Citation 신뢰 모델 및 Fallback 정량 기준](./005-citation-trust-and-fallback.md)
**Status**: Superseded by ADR-010  
**주제**: (historical) 3-tier verifier, hybrid claim 추출, 2-게이트 fallback, confidence 산출. direct citation 한정

### [ADR-006: Hybrid Retrieval](./006-hybrid-retrieval.md)
**Status**: Superseded by ADR-011  
**주제**: (historical) bge-m3 dense+sparse 통합, Qdrant named vectors, DBSF fusion, top_k 단계. 단일 tenant 가정

### [ADR-007: Document/Chunk Lifecycle](./007-document-chunk-lifecycle.md)
**Status**: Superseded by ADR-012  
**주제**: (historical) chunk_id 재현성, 재업로드 idempotent, 버전 전이, 부분 실패, soft/hard delete, approval 자동화, 모델 교체 alias swap. 단일 tenant 가정

### [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md)
**Status**: Accepted  
**주제**: tenant_id 1차 분기, collection-per-tenant, PostgreSQL RLS, JWT claim+URL path 3중 방어, tenants 테이블, tenant_resolver 노드, AuthAdapter Protocol stub. ADR-006 부분 supersede

### [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md)
**Status**: Accepted  
**주제**: Hybrid 저장(filesystem defaults + DB overrides), platform→tenant 계층, pydantic schema per category, LISTEN/NOTIFY 캐시 invalidate, prompt 식별자, evaluation promotion_gate, admin RBAC

### [ADR-010: Citation Trust v2 (Multi-type)](./010-citation-trust-v2.md)
**Status**: Accepted  
**주제**: 4-type citation(direct/synthesis/inference/conflict), 원칙 8 재해석, per-type verifier, LLM-as-judge for inference, conflict 휴리스틱+LLM, marker syntax 한국어 라벨, per-tenant allowed_citation_types. **Supersedes ADR-005**

### [ADR-011: Hybrid Retrieval v2 (Multi-tenant)](./011-hybrid-retrieval-v2.md)
**Status**: Accepted  
**주제**: collection-per-tenant + bge-m3 dense+sparse, DBSF/RRF/weighted_sum 선택, reranker shared default + per-tenant override + bypass 옵션, query rewriting(HyDE/llm_expand) 완전 구현, 임베딩 이종화 운영, 가용성 실패 핸들링. **Supersedes ADR-006**

### [ADR-012: Lifecycle v2 (Multi-tenant + Complete)](./012-lifecycle-v2.md)
**Status**: Accepted  
**주제**: tenant lifecycle 4-status, chunks archival 90일, old collection 30일 hold, Alembic 기반 schema 진화, hard delete cross-system 일관성(dead-letter), backup/restore per tenant, 4가지 reindex 모드, doc-level approval 한정. **Supersedes ADR-007**

### [ADR-013: Model Routing & SLM/LLM Strategy](./013-model-routing-and-slm-llm-strategy.md)
**Status**: Accepted  
**주제**: Rule-based YAML router, 2-tier query classifier, multi-tenant single vLLM + LoRA per-request, LoRA registry+학습/배포 워크플로우, chat_structured/chat_streaming 두 모드, fallback chain, Model & Routing 콘솔. SLM router·budget tracking 미지원

### [ADR-014: Assessment Workflow](./014-assessment-workflow.md)
**Status**: Accepted  
**주제**: 별도 `assessment_items` 테이블 + `items_<tenant>` collection, 3 모드(extract/generate/hybrid), bge-m3 similarity check 0.85, 4 LLM validator(answer/explanation/choices/difficulty), item_id citation, tenant.domain_modules 다중 활성, Assessment Console 메뉴

### [ADR-015: Tenant Input Schema](./015-tenant-input-schema.md)
**Status**: Accepted  
**주제**: JSON Schema(YAML 표기), forward-compatible 진화 + deprecation 6개월, react-jsonschema-form 동적 폼, metadata JSONB + GIN/generated column, input_types 다중·도메인별, YAML editor + Form Builder GUI 둘 다, ADR-009 schema migration deferred 항목 완성

### [ADR-016: UI Architecture](./016-ui-architecture.md)
**Status**: Accepted  
**주제**: URL path tenant mirror, 사용자 채팅 + admin 13메뉴 + platform_admin 4메뉴, 4-type citation 마커 렌더링, ConflictBox·InferenceJudgePopover, ModeSelector(structured/streaming), shadcn/ui + Monaco + react-jsonschema-form. SPEC.md §5 흡수

### [ADR-017: API Specification](./017-api-specification.md)
**Status**: Accepted  
**주제**: `/api/{tenant_id}/...` path mirror, JWT Bearer + AuthAdapter, success/fallback 분기 chat 응답, 60+ endpoint(chat sync/SSE, conversation, feedback, document·indexing·dashboard·configs·prompts·routing·lora·schema·evaluation·assessment·platform admin), OpenAPI 자동 생성. SPEC.md §8 흡수

### [ADR-018: SSO Integration with AuthFusion](./018-sso-integration-authfusion.md)
**Status**: Accepted  
**주제**: AuthFusion OIDC Provider 연동 (Authorization Code + PKCE, JWKS RS256), **OAuth2 client ≡ tenant 매핑** (TokenClaims 수정 0), AuthFusionAdapter 구체 구현, `user_tenant_membership` 테이블로 clearance/department 보강, multi-tenant user 전환은 OIDC re-login, service account (client_credentials), Mock 모드. ADR-008 §7 stub 완성

### [ADR-019: Infrastructure Sharing & Resource Allocation](./019-infrastructure-sharing.md)
**Status**: Accepted  
**주제**: PostgreSQL 115번 인스턴스 공유 + 별도 `domainrag` database, RLS 성능 가이드라인(C3 흡수), 임베딩 마이그레이션 명시 워크플로우(C4 흡수), GPU 매핑(174:GPU0=Ollama공유 / GPU1·2=Shared LLM / GPU3=Embedding+Reranker, 115:GPU0·1=Tenant SLM), KeyHub 활용(LoRA·secrets), Ollama 공유 정책

### [ADR-020: PII Detection & Audit Integration](./020-pii-and-audit-integration.md)
**Status**: Accepted  
**주제**: 4-layer PII 처리(input·storage·indexing·response), WiSentinel `dlp-core` 룰 포팅(직접 호출 X), AuthFusion Ledger 보안 이벤트 publish, chat_logs PII 마스킹 정책, 컴플라이언스 모드(standard·gdpr_strict·hipaa_strict), right-to-erasure. M1 PII 공백 해결

---

## 진행 중인 ADR

현재 검토 중인 설계 결정들:

- (없음)

---

## 새로운 ADR 제안

새로운 설계 결정이 필요하면:

1. 파일명 형식: `NNN-kebab-case-title.md`
2. 다음 번호 확인 후 생성
3. 구조 템플릿 사용
4. 팀 리뷰 요청

---

**관련 문서**: [ARCHITECTURE.md](../ARCHITECTURE.md)
