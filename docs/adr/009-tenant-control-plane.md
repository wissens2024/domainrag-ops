# ADR-009: Tenant Control Plane (Config·Policy·Prompt·Model·Eval)

## Status
**Accepted** (2026-05-08)

> ADR-008(Multi-Tenant Architecture)이 격리 *경계*를 정했고, 본 ADR은 그 경계 안에 들어가는 **정책·프롬프트·모델 매핑·평가셋의 schema와 운영 모델**을 정의한다.

---

## Context

### 배경 (사실)

- ADR-008 §5는 신규 tenant 등록 시 `configs/tenants/<tenant_id>/` 디렉터리 시드를 약속했지만 **각 YAML의 schema·계층·기본값·진화 정책이 비어 있음**.
- 비전 §4 "Tenant Control Plane"은 Tenant Registry, Config, Policy, Prompt, Model Mapping, Vector Collection, Evaluation Dataset 7가지를 명시.
- 비전 §3 표가 보여주듯 보안/법무/시험문제/설비 도메인은 **입력 구조부터 다름** — 정책이 동질적 default로 처리 불가.
- 운영자가 평가 결과로 **임계치·가중치·프롬프트를 자주 튜닝**할 가능성. 코드 재배포 의존은 운영성 저하.
- 일부 정책(input_schema, model 매핑)은 ML/플랫폼 팀 통제. 일부(prompt body, citation 임계치)는 운영자 일상 편집.

### 가정

- pydantic 기반 schema 검증 사용 가능 (이미 SPEC §13 Prompt 5에서 채택)
- PostgreSQL `LISTEN/NOTIFY` 활용 가능
- Admin UI(SPEC §5.4~5.8)에 신규 메뉴(Tenant Management, Citation Inspector, Model Console, Evaluation Console) 추가 여력
- Git 기반 변경 이력이 platform-level 정책에 충분

가정 중 하나라도 깨지면 재검토 — 특히 admin UI 개발 여력이 부족하면 D1을 filesystem-only로 회귀 검토.

---

## Decision

### 1. 저장 위치 — Hybrid (Filesystem defaults + DB overrides)

| 어디에 | 무엇을 |
|---|---|
| `configs/platform/*.yaml` (git) | 모든 tenant 공유 기본값 — prompt skeleton, 기본 retrieval/citation 임계치, 기본 model spec |
| `configs/tenants/<tenant_id>/*.yaml` (git) | tenant 고유 *구조적* 정책 — input_schema, model 매핑, eval dataset 경로 |
| `tenant_config_overrides` 테이블 (DB) | tenant 고유 *가변* 값 — 임계치/가중치/prompt 본문 (운영 중 자주 튜닝되는 값) |

런타임 합성:
```
TenantConfig = merge(platform_defaults, tenant_static_files, db_overrides)
```

#### Merge 규칙 (CLAUDE.md Y3)

`TenantConfigService` 는 다음 의미로 합성한다. 운영 진실 소스: `backend/app/core/tenant_config_service.py`.

| Type | Rule |
|---|---|
| `dict` | **deep merge** — override의 key만 차례로 base 위에 덮어쓴다. 양쪽 모두에 같은 key가 있고 그 값이 dict면 재귀적으로 deep merge. |
| `list` | **override가 base를 통째로 교체**. 부분 추가는 지원하지 않는다 (의미 모호성 회피). |
| 기타 primitive (`str`/`int`/`float`/`bool`) | override 값으로 교체. |
| `None` (`null` in YAML) | **explicit** — key 존재하나 값이 None인 것과 key 부재는 다르다. `None` override는 base 값을 명시적으로 None으로 만들 의도. |

`db_overrides`는 `(category, key)` 형식의 평면 row이고, key에 dot notation(`verification.tier2.thresholds.strong`)을 사용해 deep path를 표현한다. service가 path를 dict 구조로 풀어 deep merge에 합류.

### 2. 디렉터리 구조

```
configs/
├── platform/
│   ├── prompts/
│   │   ├── chat_answer.yaml         # ADR-005 hybrid structured 기본
│   │   ├── query_classify.yaml
│   │   └── answer_schema.json
│   ├── retrieval.yaml               # ADR-006 기본값 (top_k 30+30→50→10→5, DBSF α=β=0.5)
│   ├── citation.yaml                # ADR-005 기본 임계치
│   └── model.yaml                   # 기본 LLM endpoint (shared_llm)
└── tenants/
    └── <tenant_id>/
        ├── input_schema.yaml        # 본 ADR-009 신규 — 입력 폼·메타데이터 정의 (ADR-015에서 확장)
        ├── model.yaml               # tenant_slm endpoint, fallback chain
        ├── retrieval.yaml           # platform retrieval 일부 override
        └── prompts/
            ├── chat_answer.yaml     # platform 변형 또는 자체
            └── ...
```

### 3. PostgreSQL 스키마 — tenant_config_overrides + audit

```sql
-- 가변 값 저장 (tenant 운영 중 자주 변경)
CREATE TABLE tenant_config_overrides (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    category VARCHAR(50) NOT NULL,        -- 'citation' | 'retrieval' | 'prompt' | ...
    key TEXT NOT NULL,                     -- 'verification.tier2.thresholds.strong'
    value JSONB NOT NULL,                  -- 0.78
    schema_version VARCHAR(20),
    updated_by VARCHAR(255),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (tenant_id, category, key)
);
ALTER TABLE tenant_config_overrides ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_config_overrides
  USING (tenant_id = current_setting('app.current_tenant')::text);

-- 변경 이력 (시계열)
CREATE TABLE tenant_config_change_logs (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    category VARCHAR(50) NOT NULL,
    key TEXT NOT NULL,
    old_value JSONB,
    new_value JSONB,
    changed_by VARCHAR(255),
    changed_at TIMESTAMP DEFAULT NOW(),
    reason TEXT
);
```

Filesystem 변경은 git history로 자연 추적 — 별도 audit table 불필요.

### 4. 정책 카테고리별 pydantic schema

각 카테고리에 schema 클래스(파일은 platform 또는 tenant 디렉터리에 있고, 합성 후 검증):

```python
class TenantInputSchema(BaseModel):     # ADR-015에서 확장
    schema_version: str = "v1"
    input_types: list[InputTypeDef]
    required_metadata: list[str]
    optional_metadata: list[str]

class TenantPromptConfig(BaseModel):
    name: str
    version: str
    template: str                        # system + user 합쳐도 됨
    response_schema_path: Optional[str]
    ab_slot: Literal["control", "treatment_a", "treatment_b"] = "control"

class TenantModelMapping(BaseModel):    # 라우팅은 ADR-013
    tenant_slm: ModelEndpoint
    shared_llm: ModelEndpoint
    embedding: EmbeddingSpec
    reranker: RerankerSpec
    fallback_chain: list[str]            # ["tenant_slm", "shared_llm"]

class TenantRetrievalPolicy(BaseModel):  # ADR-006/011 정합
    top_k: TopKConfig
    fusion: FusionConfig
    query_rewriting: QueryRewriteConfig

class TenantCitationPolicy(BaseModel):   # ADR-005/010 정합
    verification: VerificationConfig
    gates: GatesConfig
    confidence_weights: ConfidenceWeights
    fallback: FallbackConfig

class TenantAnswerPolicy(BaseModel):
    response_format: str                 # "structured" | "freeform"
    streaming: bool = False              # ADR-013 §6 ui_mode 분기 — false=chat_structured 기본, true=chat_streaming
    caveat_policy: str
    domain_modules: list[str]            # ["assessment"] (ADR-014)

class TenantEvaluationConfig(BaseModel):
    qa_dataset_path: str
    promotion_gate: PromotionGate
```

검증 시점:
- Tenant 등록 시 — schema 검증 통과해야 collection 생성 단계 진행
- Config reload 시 — 합성 후 재검증
- DB override 저장 시 — JSONB 저장 전 schema 검증

#### Schema 진화 절차 (ADR-020 amendment, C6 해결)

Config schema가 변하면 (예: citation.yaml v1 → v2에서 `tier3.unsupported_action` enum에 `regenerate_then_annotate` 추가, 또는 `tier2.thresholds`가 평면 → 중첩 구조로 변경) 기존 DB override JSONB가 새 pydantic schema와 불일치할 수 있다. 다음 절차 의무:

```text
1) 각 카테고리 pydantic schema에 schema_version: int 필드 의무 (시작값 1)
2) tenant_config_overrides 테이블에 schema_version_at_write INT 컬럼 추가
   - DB write 시 현재 코드의 schema_version 값을 함께 기록
3) 코드 schema_version 증가 시 다음 중 하나 의무:
   (a) backward-compatible 변경 (필드 추가, default 보강)
       → migration 불필요, 기존 row 그대로 동작
   (b) breaking 변경 (필드 제거·타입 변경·enum 축소)
       → packages/rag_core/configs/migrations/<category>_vN_to_vN+1.py 작성
       → 일괄 변환 (Alembic data migration 또는 평일 작업)
       → 변환 후 schema_version_at_write 갱신
4) 로딩 시 schema_version_at_write 검증 — 코드 version과 차이 시 두 경로:
   - migration 함수 등록되어 있으면 in-flight 변환 후 사용
   - 등록 안 된 차이면 startup 차단 + 운영자 alert
5) yaml 시드 파일은 항상 최신 schema_version 매칭 (배포 전 검증)
```

이 절차는 ADR-015의 input_schema 진화 정책과 정합 (forward-compatible 우선, breaking은 명시 migration).

### 5. 로드/캐시/재로드

- 런타임: `TenantConfigService` 클래스가 tenant당 `TenantConfig` 객체 캐시 (LRU + TTL 60초)
- DB 변경 즉시 반영: PostgreSQL `LISTEN tenant_config_changed` → service가 cache invalidate
- Filesystem 변경: dev 모드에서만 watcher; prod는 재배포 시 reload
- 수동 invalidate: `POST /api/admin/{tenant_id}/configs/reload` (admin RBAC)
- `TenantConfig` 객체는 source 추적 보유: `config.citation.thresholds.strong → from: db_override` (디버깅 시 "이 값 어디서 왔나?" 즉답)

> **[ADR-021 §2](./021-operational-bootstrap.md) 정합**: LISTEN/NOTIFY 채널 이름·payload 스키마·재연결 backoff·multi-instance broadcast 동작은 ADR-021 §2가 단일 진실 소스다. 본 절은 정책 결정(LISTEN/NOTIFY 채택)만 정의하고 운영 결선은 ADR-021을 참조한다. 신규 prompt PATCH 영구화·lifespan startup hook의 preload도 동일하게 ADR-021 §1 책임.

### 6. Per-tenant Prompt 관리

- 파일: `configs/tenants/<tenant_id>/prompts/<task>.yaml` (chat_answer, query_classify 등)
- Prompt body 본문은 DB override 가능 (운영자가 admin UI로 편집)
- 식별자 형식: `<tenant_id>:<task>:<version>:<ab_slot>`
- chat_logs에 prompt_version으로 식별자 저장 → 어떤 prompt가 답변했는지 추적

### 7. Per-tenant Evaluation Dataset

```
data/eval/
├── platform/
│   └── smoke.jsonl                    # 모든 tenant 공통 smoke test
└── tenants/
    └── <tenant_id>/
        ├── qa.jsonl                   # tenant 도메인 QA 셋
        ├── citation_gold.jsonl        # 기대 chunk_id 라벨
        └── promotion_gate.yaml
```

`promotion_gate.yaml` 예시:
```yaml
metrics:
  retrieval_recall_at_5: { min: 0.85 }
  citation_accuracy: { min: 0.80 }
  unsupported_ratio: { max: 0.20 }
  fallback_rate: { max: 0.15 }
auto_promote: false   # 운영자 명시 승인 필요
```

평가 러너(SPEC §13 Prompt 18)는 tenant별 실행 + gate 통과 여부 출력. ADR-013(model routing)이 새 모델 promotion 시 본 gate 사용.

### 8. 변경 거버넌스

- DB override 변경: `tenant_config_change_logs`에 자동 기록 (old/new/user/timestamp/reason)
- Filesystem 변경: git history (commit 메시지가 audit)
- Admin RBAC:
  - `admin` (tenant scope): 자기 tenant DB override 편집 가능
  - `platform_admin`: platform/* filesystem + 모든 tenant override 편집 가능
- 일부 high-impact key(예: `model.tenant_slm.endpoint`)는 platform_admin 전용으로 제한 (config schema에 `restricted_to: platform_admin` 메타)

### 9. Admin Console 영향 (SPEC 갱신 예고)

ADR-009 도입으로 SPEC §5 admin console에 신규 메뉴:
- **Tenant Management** — 테넌트 CRUD (등록 자동화 트리거)
- **Tenant Config Editor** — 카테고리별 override 편집 + diff preview
- **Prompt Studio** — prompt 본문 편집 + ab slot 비교
- **Model & Routing Console** — model 매핑 + ADR-013 라우팅 규칙 관리
- **Evaluation Console** — promotion_gate 실행 + 결과 비교

각 메뉴는 ADR-013/014/015 결정 후 개별 사양 확정.

---

## Consequences

### 긍정적 영향

- 정적 schema는 git 보호, 가변 hyperparameter는 admin UI 즉시 편집 — 운영 자유도 ↑
- 정책 변경 이력 자동 추적 (DB audit + git)
- platform defaults → tenant overrides 계층으로 신규 tenant 온보딩 비용 ↓
- 정책 충돌은 schema 검증으로 등록 단계에서 차단

### 부정적 영향 / 부채

- Hybrid는 두 시스템 정합성 운영 부담 (filesystem과 DB가 일관되도록)
- Admin UI 개발 범위 큼 (SPEC §5 신규 메뉴 4개)
- Source 추적 디버깅 비용 (어디서 온 값인지 추적 코드 필요)
- Schema 진화 시 migration 함수 작성 부담 — DB row와 filesystem yaml 양쪽 마이그레이션

### 후속 작업

- `tenant_config_overrides` + `tenant_config_change_logs` 테이블 마이그레이션 (RLS 포함)
- pydantic schema 모듈 (`packages/rag_core/configs/`)
- `TenantConfigService` 구현 (캐시 + LISTEN/NOTIFY)
- platform/* 기본값 시드 (현재 ADR-005/006 결정 반영)
- SPEC §5 admin console 메뉴 4개 사양 — ADR-013/014/015 결정 후 통합 갱신
- ADR-015에서 input_schema 본격 정의

---

## Alternatives Considered

### 1. Filesystem 단독 (모든 config가 git)
- **장점**: 변경 이력 git history로 자연, schema 검증 빌드 시점에 강제
- **기각 사유**: 운영자가 평가 후 임계치 튜닝하려면 git 접근·재배포 필수 → 일상 운영성 저하. 폐쇄망 운영팀은 보통 git 환경과 분리.

### 2. PostgreSQL 단독 (모든 config가 DB)
- **장점**: admin UI 통한 단일 진입점, 즉시 편집·즉시 반영
- **기각 사유**: schema 진화·마이그레이션 부담 ↑, structural decisions(input_schema)도 DB row가 되어 git review 못 받음. ML 팀이 model.yaml를 조심히 관리할 길 없음.

### 3. Filesystem 우선 + admin UI 지연 도입
- **장점**: 초기 단순
- **기각 사유**: 비전이 admin UI(Citation Inspector, Model Console, Evaluation Console)를 day 1 산출물로 명시. 본 시스템은 완제품 단일 설계라 UI를 별도 단계로 미루지 않는다.

### 4. JSON 또는 TOML 사용
- **장점**: 파서 다양성
- **기각 sayp**: YAML이 Python 생태계 표준이고 SPEC §13 Prompt 5가 이미 YAML 채택. 일관성 ↑.

### 5. 정책 상속 없이 tenant 자족적 config
- **장점**: 디버깅 단순, source 추적 불필요
- **기각 사유**: tenant 추가마다 중복 복제 → drift 누적, platform 정책 변경이 tenant에 자동 반영 안 됨. 현실적이지 않음.

---

## Related

- [ADR-002: Protocol/Adapter 패턴](./002-protocol-adapter-pattern.md) — TenantConfigService도 Protocol 패턴
- [ADR-005: Citation 신뢰 모델](./005-citation-trust-and-fallback.md) — citation 임계치는 본 ADR 카테고리 중 하나
- [ADR-006: Hybrid Retrieval](./006-hybrid-retrieval.md) — retrieval policy 카테고리 정합
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — 본 ADR이 그 경계 안 채움
- ADR-013 (예정): Model Routing — model 매핑 카테고리 schema 본격 정의
- ADR-015 (예정): Tenant Input Schema — input_schema 카테고리 본격 정의
- [ADR-021: Operational Bootstrap](./021-operational-bootstrap.md) — 본 ADR §5 LISTEN/NOTIFY 결선·preload·재연결 운영 책임
- SPEC.md §5 관리자 콘솔 / §13 Prompt 5 — 폐기 (본 ADR + [ADR-016](./016-ui-architecture.md)로 흡수)

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted
