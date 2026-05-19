# ADR-015: Tenant Input Schema — 정의·진화·동적 폼·페이로드

## Status
**Accepted** (2026-05-08)

> ADR-009가 `configs/tenants/<id>/input_schema.yaml`을 약속만 했고 schema 메커니즘이 비어있었다. ADR-014가 assessment_item type을 별도 정의해야 함을 명시했다. 본 ADR이 schema 정의 형식·진화 정책·동적 폼 생성·페이로드 처리·input_type 분류·schema editor를 단일 진실 소스로 정의한다. ADR-009가 deferred 처리했던 schema migration 항목도 본 ADR이 완전 설계로 채운다.

---

## Context

### 배경 (사실)

- 비전 §3 표가 보여주듯 보안·법무·시험·설비 도메인은 **입력 구조·메타데이터가 본질적으로 다름**.
- 비전 §5는 공통 입력 구조와 도메인별 입력 예시를 sketch로만 제공.
- ADR-014가 assessment_item을 별도 input_type으로 처리하기로 결정.
- ADR-009의 `TenantInputSchema` pydantic 모델이 placeholder로 남아있음 — 실제 schema 정의·검증·진화 메커니즘 부재.
- 본 프로젝트 정책: 완제품 단일 설계, MVP/Phase 분리 금지 (메모리 참조).
- 입력 폼은 운영자·일반 사용자가 사용 — 동적 생성 없으면 새 input_type 추가마다 코드 작업 필요.

### 가정

- JSON Schema Draft 2020-12 표준 활용 가능 (Python·JS 라이브러리 풍부)
- frontend가 `react-jsonschema-form` 또는 동급 라이브러리 사용 가능
- PostgreSQL JSONB가 도메인 특화 필드 저장·인덱싱 (GIN index)에 충분
- Qdrant payload가 임의 JSON 구조 저장 가능 (검증된 사실)
- 운영자가 YAML 편집·Form Builder GUI 둘 다 사용 가능 (둘 다 제공)

가정이 깨지면 재검토 — 특히 PostgreSQL JSONB 인덱싱이 도메인별 검색 성능에 부족하면 도메인 특화 컬럼 추가 검토.

---

## Decision

### 1. Schema 정의 형식 — JSON Schema Draft 2020-12 (YAML 표기)

source of truth는 YAML, 런타임 검증은 JSON Schema. 두 형식이 동등 — YAML이 더 읽기 쉬우므로 편집·git diff 친화.

```yaml
# configs/tenants/security/input_schema.yaml
schema_version: "2026-05-08"        # 본 input_schema 자체의 버전
tenant_id: security

input_types:
  - name: policy_document
    schema_version: "v1"             # 이 input_type의 버전
    title: 보안 정책 문서
    description: 보안 정책·매뉴얼·절차서
    
    fields:
      # 공통 필드 (모든 input_type 공통)
      $ref: "platform/common_fields.yaml#/common_document"
      
      # 도메인 특화 필드
      properties:
        policy_id:
          type: string
          title: 정책 ID
          pattern: "^SEC-POL-[0-9]+$"
        policy_type:
          type: string
          enum: [data_export, access_control, incident_response]
          title: 정책 유형
        authority_rank:
          type: integer
          minimum: 0
          maximum: 100
          title: 권위 등급
          description: 다른 정책과 충돌 시 우선순위 (높을수록 우선)
        effective_date:
          type: string
          format: date
        revision_date:
          type: string
          format: date
        supersedes:
          type: array
          items: { type: string }
          title: 대체하는 이전 정책
        related_procedures:
          type: array
          items: { type: string }
      
      required: [policy_id, policy_type, authority_rank, effective_date]
    
    parser_policy: pdf_with_section
    chunking_policy: semantic_with_authority
    
  - name: procedure
    schema_version: "v1"
    title: 절차서
    fields:
      properties:
        procedure_id:
          type: string
        related_policies:
          type: array
          items: { type: string }
      required: [procedure_id]
    parser_policy: pdf_with_section
    chunking_policy: semantic
```

#### 공통 필드 ($ref)

```yaml
# configs/platform/common_fields.yaml
common_document:
  properties:
    title:
      type: string
      title: 제목
    department:
      type: string
      title: 부서
    security_level:
      type: string
      enum: [public, internal, confidential, secret]
    acl:
      type: array
      items: { type: string }
    owner:
      type: string
    valid_from:
      type: string
      format: date
    valid_until:
      type: ["string", "null"]
      format: date
    tags:
      type: array
      items: { type: string }
    quality_status:
      type: string
      enum: [draft, reviewed, approved, archived]
  required: [title, security_level]
```

YAML이 source, 런타임에 JSON Schema로 변환 (`yaml.safe_load` + `jsonschema` 라이브러리 검증).

### 2. Schema 진화 — Forward-compatible

#### 원칙

| 변경 종류 | 허용 | 절차 |
|---|---|---|
| 필드 추가 (optional) | ✅ 자유 | YAML 수정 + git commit + LISTEN/NOTIFY 즉시 반영 |
| 필드 추가 (required) | ⚠ 조건부 | default 값 동반 또는 backfill migration 후 |
| 필드 제거 | ❌ 즉시 불가 | deprecation 6개월 → archived 단계 → 제거 |
| 필드 type 변경 | ❌ 즉시 불가 | 새 필드명으로 추가 + 이전 deprecation |
| enum value 추가 | ✅ 자유 | YAML 수정 |
| enum value 제거 | ❌ 즉시 불가 | deprecation 단계 |

#### Deprecation 단계

```yaml
# configs/tenants/security/input_schema.yaml
input_types:
  - name: policy_document
    schema_version: v2
    fields:
      properties:
        old_field:
          type: string
          deprecated: true                  # JSON Schema 표준 키워드
          deprecation_message: "v3에서 제거 예정. new_field를 사용하세요."
          deprecation_until: "2026-11-08"   # 6개월 후
        new_field:
          type: string
```

- Frontend가 `deprecated: true` 필드는 회색·경고 표시, 신규 입력은 차단 가능 (`writeOnly: false` 또는 form 비활성)
- API는 여전히 deprecated 필드 받음 (backward compat)
- `deprecation_until` 도래 시 `archived: true`로 전환 (자동 cron 또는 운영자 수동), API는 거절
- archived 후에도 기존 데이터는 보존 — 검색·표시는 가능

#### Schema Version 추적

- 모든 documents/assessment_items row에 `input_schema_version VARCHAR(20)` 컬럼 (등록 시점의 schema_version 보관)
- 검색 시 schema_version 무관하게 모든 row 조회 가능 (필드 부재는 null로 처리)
- 새 schema 도입 후 기존 row는 *그대로 보존* — 검색은 union으로 동작

#### Validation 시점

```text
1. Tenant 등록 시: input_schema.yaml 전체 JSON Schema 형식 검증
2. 새 schema 배포 시: backward compatibility 검사
   - 기존 input_type의 required 필드가 추가되었는가? → backfill 필요 또는 default 동반
   - 필드 type이 incompatible 변경? → 거절
3. Document/Item 등록 시: 해당 input_type schema로 입력 검증 (jsonschema 라이브러리)
```

### 3. Dynamic Form Generation — react-jsonschema-form

#### Frontend 흐름

```text
1. 사용자가 /admin/{tenant_id}/documents/upload 진입
2. GET /api/{tenant_id}/input_schemas → input_types 목록 + 각 schema 반환
3. 사용자가 input_type 선택 (예: policy_document)
4. RJSF가 schema → 폼 자동 렌더 (text input, date picker, select, multi-select 등)
5. 사용자 입력 → JSON Schema 클라이언트 측 검증
6. POST /api/{tenant_id}/documents/upload + multipart file + metadata JSON
7. 서버 측 재검증 → 통과 시 인덱싱 파이프라인
```

#### UI Schema (별도 파일)

JSON Schema는 *데이터 구조*, UI Schema는 *표시 방식*:

```yaml
# configs/tenants/security/ui_schema.yaml
policy_document:
  ui:order: [policy_id, policy_type, title, authority_rank, effective_date, revision_date, ...]
  policy_type:
    "ui:widget": select
  authority_rank:
    "ui:widget": range          # 슬라이더
  related_procedures:
    "ui:widget": multiselect
```

UI Schema도 YAML, frontend에 함께 전달.

#### i18n

JSON Schema의 `title`/`description`을 한국어로 작성. 다국어 필요 시 i18n bundle 별도 (`title_en`, `title_ko`...) — 본 ADR은 한국어 단일 가정.

### 4. Payload 처리 (PostgreSQL + Qdrant)

#### PostgreSQL `metadata JSONB`

documents·assessment_items 테이블의 `metadata JSONB` 컬럼이 schema-specific 필드 모두 보관:

```sql
ALTER TABLE documents ADD COLUMN metadata JSONB DEFAULT '{}';
ALTER TABLE documents ADD COLUMN input_schema_version VARCHAR(20);
ALTER TABLE documents ADD COLUMN input_type VARCHAR(64) NOT NULL;

CREATE INDEX idx_documents_metadata ON documents USING gin (metadata);
CREATE INDEX idx_documents_input_type ON documents (tenant_id, input_type);
```

GIN 인덱스로 `metadata @> '{"policy_type": "data_export"}'` 같은 JSON 검색 가능.

도메인 특화 필드 중 자주 검색되는 것은 generated column으로 인덱싱:
```sql
ALTER TABLE documents ADD COLUMN policy_type VARCHAR(50) 
  GENERATED ALWAYS AS (metadata->>'policy_type') STORED;
CREATE INDEX idx_documents_policy_type ON documents (tenant_id, policy_type);
```

#### Qdrant payload

chunk indexing 시 documents.metadata + chunk-level 메타가 합쳐져 Qdrant payload로:
```json
{
  "tenant_id": "security",
  "doc_id": "DOC-2026-0001",
  "chunk_id": "DOC-...:v1:p1:chunk:0012",
  "input_type": "policy_document",
  "input_schema_version": "v1",
  "policy_id": "SEC-POL-001",
  "policy_type": "data_export",
  "authority_rank": 90,
  "...common fields...": "..."
}
```

검색 필터에서 `must={"policy_type": "data_export", "authority_rank": {">": 50}}` 식 활용 가능 — ADR-011 retrieve_context의 필터 빌더가 schema-aware 필요.

### 5. Input Types — 다중·도메인별

`tenants` 테이블에 `default_input_types JSONB DEFAULT '[]'` 컬럼:
```yaml
# 보안 tenant
default_input_types: ["policy_document", "procedure", "incident_report"]

# 법무 tenant
default_input_types: ["contract", "case_law", "regulation"]

# 시험 tenant (ADR-014)
default_input_types: ["assessment_item"]

# 혼합 tenant (RAG + Assessment 둘 다)
default_input_types: ["policy_document", "assessment_item"]
```

각 input_type이 다른 parser·chunking·indexing 파이프라인:
```yaml
# input_schema.yaml의 각 input_type
parser_policy: pdf_with_section | docx_default | structured_item | spreadsheet
chunking_policy: semantic | semantic_with_authority | per_clause | none  # assessment는 chunking 없음
indexing_target: chunks | items                                          # ADR-014
```

`indexing_target: items`인 input_type은 ADR-014 assessment_items 테이블·`items_<tenant>` collection으로 라우팅. 그 외는 documents·`chunks_<tenant>` collection.

### 6. Schema Editor — Admin UI 두 갈래

#### (a) YAML Editor + 미리보기

- Monaco editor (VS Code 엔진)
- 좌측 YAML 편집, 우측 react-jsonschema-form 미리보기 (실시간)
- Lint·schema 검증 inline
- "변경 저장" 시 validation + git commit (filesystem) 또는 DB override

#### (b) Form Builder GUI

- "필드 추가" 버튼 → 필드명·type·title·옵션 GUI 입력
- 추가된 필드가 우측 미리보기에 즉시 반영
- 저장 시 YAML로 변환 후 commit

둘 다 day 1 제공. YAML이 source of truth, GUI는 편의 도구.

### 7. ADR-014 정합 — assessment_item input_type

```yaml
# configs/tenants/exam_bank/input_schema.yaml
input_types:
  - name: assessment_item
    schema_version: v1
    title: 시험 문제
    indexing_target: items                  # ADR-014
    fields:
      properties:
        question_id:
          type: string
        subject:
          type: string
        chapter:
          type: string
        difficulty:
          type: string
          enum: [easy, medium, hard]
        question_type:
          type: string
          enum: [multiple_choice, true_false, short_answer, essay]
        question_text:
          type: string
        choices:
          type: array
          items: { type: string }
        answer:
          type: string
        explanation:
          type: string
        learning_objective:
          type: string
        tags:
          type: array
          items: { type: string }
      required: [question_id, subject, chapter, difficulty, question_type, question_text, answer]
    parser_policy: structured_item
    chunking_policy: none
```

ADR-014 §1 assessment_items 테이블 컬럼은 schema가 정의한 필드와 1:1 대응 + ADR-014 고유 필드(used_count, generation_mode, validator_results 등). schema 변경이 ADR-014 테이블 마이그레이션 트리거.

### 8. 검증 시점 통합

```text
1. Schema 등록 (Tenant 생성 또는 input_schema 갱신):
   - YAML parse → JSON Schema 변환
   - JSON Schema 자체 검증 (meta-schema)
   - Backward compat 검사 (이전 schema와 비교)
   - 통과 시 configs commit + LISTEN/NOTIFY broadcast

2. Document/Item 등록 (사용자 업로드 또는 admin 수동):
   - input_type 선택
   - 해당 input_type schema 로드 (TenantConfigService 캐시)
   - 입력 페이로드 jsonschema validate
   - 통과 시 documents/items 테이블 INSERT + 인덱싱 trigger

3. Re-index (ADR-012 reindex 모드):
   - 기존 metadata 보존, schema_version 보존
   - 새 schema가 적용된 후의 신규 문서만 새 schema로 검증
```

### 9. tenant_input_schemas 테이블 (감사 + Optimistic Locking, Y8)

```sql
CREATE TABLE tenant_input_schemas (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    schema_version VARCHAR(20) NOT NULL,    -- 단조 증가 (v1, v2, v3, …)
    yaml_content TEXT NOT NULL,             -- 전체 YAML (원본)
    json_schema JSONB NOT NULL,             -- 변환된 JSON Schema
    status VARCHAR(20) DEFAULT 'active',    -- 'active' | 'deprecated'
    is_active BOOLEAN DEFAULT true,         -- view용 boolean (status='active'와 동기)
    created_by VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    deactivated_at TIMESTAMP,
    UNIQUE (tenant_id, schema_version)
);

ALTER TABLE tenant_input_schemas ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_input_schemas
  USING (tenant_id = current_setting('app.current_tenant')::text);
```

각 schema_version의 YAML·JSON Schema 모두 보존 → audit·rollback·과거 데이터 검증에 사용.

#### Optimistic Locking 절차 (CLAUDE.md Y8)

복수 admin이 같은 tenant의 schema를 동시 편집할 때 마지막 PUT이 silently 덮어쓰지 않도록 `schema_version`을 **lock token**으로 사용한다.

1. 클라이언트(Schema Editor UI)가 `GET /admin/schema`를 호출해 현재 `active` row를 받는다. 응답 body에 `schema_version` 포함.
2. 사용자가 편집 후 `PUT /admin/schema` body `{schema_yaml, ui_schema_yaml?, base_version}`을 보낸다. `base_version`은 1번에서 받은 값이어야 한다.
3. 서버 트랜잭션:
   - `SELECT … FROM tenant_input_schemas WHERE tenant_id=… AND status='active' FOR UPDATE`로 행 잠금.
   - `current_active.schema_version`이 `base_version`과 일치하지 않으면 → `SchemaVersionConflictError` → HTTP **409** `schema_version_conflict` + body `{current_version, base_version}` 반환. 클라이언트는 GET으로 최신본을 받아 사용자에게 merge UI 노출.
   - 일치하면 `UPDATE … SET status='deprecated', deactivated_at=NOW()` 후 새 schema를 `schema_version=current_version+1`로 `INSERT … status='active'`.
   - 같은 트랜잭션이므로 `UNIQUE (tenant_id, schema_version)` 위반이 IntegrityError로 표면화될 수 있고 그때도 `SchemaVersionConflictError`로 정규화한다.
4. 트랜잭션 commit 직후 서버는 in-process `InputSchemaLoader.apply_runtime_override`를 호출해 같은 인스턴스의 다음 요청에 새 schema가 즉시 반영되게 한다. multi-instance 동기화는 [ADR-021](./021-operational-bootstrap.md) §LISTEN/NOTIFY 책임.

`schema_version`은 SemVer가 아니라 단조 증가하는 정수 string(`"v1"`, `"v2"`, …)로 운영한다 — lock token 비교가 simple equality로 충분하다.

운영 진실 소스: `backend/app/repositories/tenant_input_schema_repository.py PostgresTenantInputSchemaRepository.insert_new_active`.

---

## Consequences

### 긍정적 영향

- 도메인이 추가될 때 코드 변경 없이 input_schema.yaml + ui_schema.yaml만으로 새 input_type 도입 가능
- 비전 §3·§5의 "테넌트별 입력 구조 차별화" 본질 충실 반영
- JSON Schema 표준으로 frontend 라이브러리·검증 도구 풍부
- Forward-compatible 진화로 기존 데이터 무결성 보장 + 점진적 schema 발전 가능
- ADR-009 deferred(schema 진화 migration)·ADR-014 assessment_item 정합 모두 본 ADR이 채움
- Schema editor 두 갈래(YAML + GUI)로 운영자·개발자 모두 친화

### 부정적 영향 / 부채

- 구현 표면적 큼 — Monaco editor, react-jsonschema-form 통합, Form Builder GUI, 검증 파이프라인, deprecation 자동화 cron
- JSONB 검색이 generated column 인덱싱 없으면 도메인 대형화 시 성능 ↓ — 자주 쓰는 필드는 generated column 추가 운영 부담
- Schema deprecation 6개월 운영 절차 — 운영자가 배포·알림·archived 전환 책임
- 도메인별 parser·chunking 정책이 schema 의존 → 정책별 코드 모듈 추가
- Schema 변경 시 영향받을 운영 영역 식별이 운영자 책임 (영향 분석 도구는 기본 미제공)

### 후속 작업

- 마이그레이션: `tenant_input_schemas` 테이블, documents/items에 `metadata JSONB`, `input_schema_version`, `input_type` 컬럼
- `configs/platform/common_fields.yaml`, `configs/platform/input_schema_templates/` 시드 (도메인별 시작 템플릿)
- jsonschema validation 모듈 + backward compat checker
- API endpoint: `GET /api/{tenant_id}/input_schemas`, `PATCH /api/{tenant_id}/input_schemas/{input_type}`
- LangGraph 노드: `validate_input` (등록 직전), `route_indexing` (input_type → chunks vs items)
- Admin console: Schema Editor (YAML editor + Form Builder + 미리보기)
- Deprecation 자동화 cron (deprecation_until 도래 시 archived 전환)

---

## Alternatives Considered

### 1. JSON Schema 대신 Pydantic 모델
- **장점**: Python 타입 안전, 코드 generation
- **기각 사유**: 런타임 변경 어려움 (코드 재배포). 운영자가 schema 변경할 수 없음. JSON Schema가 동적 운영에 본질적으로 우월.

### 2. 자체 DSL (YAML 자유 형식)
- **장점**: 도메인 친화 어휘
- **기각 사유**: 표준 도구(검증·동적 폼·문서화) 못 씀. JSON Schema 표준의 생태계 가치 큼.

### 3. Forward-compatible 대신 Strict migration (모든 변경에 backfill)
- **장점**: 데이터 일관성
- **기각 사유**: 운영 비용 ↑, 대형 tenant에서 backfill 시간 부담. forward-compat가 안전성·운영 성균형.

### 4. Schema editor를 YAML 편집기만 (Form Builder 없음)
- **장점**: 구현 단순
- **기각 사유**: 운영자(비개발자)가 YAML 편집 진입장벽 ↑. Form Builder가 운영성 ↑.

### 5. Schema editor를 Form Builder만 (YAML 직접 편집 차단)
- **장점**: 가드레일 강함
- **기각 사유**: 고급 사용자(JSON Schema 표준 활용)가 Form Builder의 표현 한계로 막힘. 둘 다 제공이 최선.

### 6. Schema 진화 미지원 (한 번 정한 schema 변경 불가)
- **장점**: 시스템 단순
- **기각 사유**: 도메인 진화 막음. 운영 중 새 메타데이터 필요해지면 새 input_type으로 우회해야 함 — 비효율.

### 7. 도메인 특화 컬럼 (metadata JSONB 대신)
- **장점**: 검색 성능 ↑
- **기각 사유**: 새 input_type 추가마다 컬럼 추가 → schema migration 부담. JSONB + generated column이 절충.

### 8. UI Schema와 Data Schema를 통합 (한 YAML)
- **장점**: 파일 1개
- **기각 사유**: 표준 분리 (JSON Schema vs UI Schema). UI 변경이 schema 검증과 무관해야 함.

---

## Related

- [ADR-002: Protocol/Adapter](./002-protocol-adapter-pattern.md) — Parser·Chunker가 input_type별 구현체
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — tenant scope, RLS
- [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md) — input_schema.yaml 위치, schema migration deferred 항목 본 ADR이 채움
- [ADR-011: Hybrid Retrieval v2](./011-hybrid-retrieval-v2.md) — payload 검색 필터 schema-aware
- [ADR-012: Lifecycle v2](./012-lifecycle-v2.md) — re-index 시 schema_version 보존
- [ADR-014: Assessment Workflow](./014-assessment-workflow.md) — assessment_item이 input_type
- SPEC.md §5 / §6 / §8 — 폐기 (본 ADR + [ADR-016](./016-ui-architecture.md) + [ADR-017](./017-api-specification.md)이 흡수)

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted
