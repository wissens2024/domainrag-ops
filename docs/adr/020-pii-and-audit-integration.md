# ADR-020: PII Detection & Audit Integration

## Status
**Accepted** (2026-05-08)

> 본 ADR은 이전 점검의 major 공백 **M1 (PII 처리 부재)**를 해결하고, WiSentinel과의 통합 범위를 **룰 라이브러리 재사용 + 선택적 audit subscribe**로 확정한다. WiSentinel과의 동기 호출(`inspect-content`)은 운영 결합 위험으로 채택하지 않는다.

---

## Context

### 배경 (사실)

- 이전 점검에서 PII 처리 정책이 SPEC.md 폐기 후 어디에도 흡수되지 않음을 확인 (M1 공백).
- 외부 시스템 **WiSentinel** (`C:\dev\workspace\ai-aware-sse`)이 12종 PII + 5종 Secrets + Prompt Injection 정규식 룰 보유 (`packages/dlp-core` TypeScript).
- WiSentinel use case는 *외부 AI* 서비스로의 prompt 유출 차단 (Chrome Extension·mitmproxy 중심). DomainRAG는 *내부* 폐쇄망 RAG로 외부 유출이 본질적으로 발생 안 함.
- WiSentinel의 audit:capture Redis queue는 통합 감사 분석에 활용 가능.
- AuthFusion ledger도 hash chain audit (FAU_STG.1)을 운영하지만 SSO 이벤트 한정.
- DomainRAG 자체 chat_logs(ADR-005/010/012)는 풍부한 메타데이터 보유 — 표준 audit는 자체 감당 가능.
- 사용자 결정: WiSentinel 직접 통합은 비추, 라이브러리·이벤트 통합만 권장.

### 가정

- WiSentinel `dlp-core` 룰이 한국어·영문 PII에 적정 정확도 (운영 검증 필요)
- DomainRAG 응답이 외부 시스템으로 직접 나가지 않음 (내부 폐쇄망)
- 사용자가 채팅 입력에 PII를 직접 타이핑하는 경우는 드물지만 발생 가능
- 문서 chunk content에 PII가 섞여 있을 수 있음 (보안 매뉴얼에 예시·주민번호 등 포함 가능성)
- chat_logs 보관 기간은 컴플라이언스에 따라 결정 (본 ADR은 90일 default)

가정 깨지면 재검토 — 특히 dlp-core 룰 정확도가 한국어에서 부족하면 자체 룰 보강 필요.

---

## Decision

### 1. PII Detection 모듈 — DomainRAG 자체 구축

WiSentinel과 동기 호출 의존 회피. DomainRAG `packages/rag_core/pii/` 신설:

```text
packages/rag_core/pii/
  ├── interfaces.py          # PIIDetector Protocol
  ├── rules/
  │   ├── pii_kr.yaml        # 한국 PII (주민번호·전화·계좌·사번·이메일·여권 등)
  │   ├── pii_en.yaml        # 영문 PII
  │   ├── secrets.yaml       # API key·token 등
  │   └── injection.yaml     # Prompt Injection 패턴
  ├── detector.py            # 정규식 기반 (룰 yaml 로딩)
  └── masker.py              # 매칭 결과 마스킹 (`***-****-****` 등)
```

룰 yaml은 **WiSentinel `dlp-core` 룰을 기반으로 포팅**:
- TypeScript 정규식 → Python `re` (대부분 호환)
- 룰 카테고리·심각도·기본 마스킹 전략을 그대로 차용
- 라이선스 호환 확인 후 inline 또는 별도 패키지로 import

### 2. PII 처리 시점 (다층 방어)

```text
[입력 단계 — 사용자 질문] ──── [Layer 1] PII Detection on input
                                  → 발견 시 정책별 처리
                                  → chat_logs.input_pii_found JSONB 기록

[저장 단계 — chat_logs 저장] ── [Layer 2] PII 마스킹 정책 적용
                                  → 원본 보존 vs 마스킹 보관 정책 분기

[인덱싱 단계 — 문서 chunk] ─── [Layer 3] PII Detection on indexing
                                  → 발견 시 chunks.pii_warnings JSONB 기록
                                  → 검색 결과 노출 시 marker 추가

[응답 단계 — LLM 출력] ──────── [Layer 4] PII Detection on response
                                  → 답변에 PII 새면 마스킹 후 노출
                                  → chat_logs.output_pii_masked JSONB 기록
```

각 layer는 configs로 enable/disable 가능. 운영 시작 시 모두 enable 권장.

### 3. 입력 PII 처리 정책 (Layer 1)

```yaml
# configs/platform/pii.yaml
input:
  enable: true
  on_pii_found:
    high_severity:    block   # 차단 + 사용자에게 안내 ("개인정보가 포함되어 있습니다")
    medium_severity:  warn    # 경고만, 진행 허용
    low_severity:     log     # 로그만
  severity_map:
    rrn: high                  # 주민등록번호
    credit_card: high
    api_key: high
    phone: medium
    email: low
    ip_address: low
  audit:
    log_to_chat_logs: true    # 발견 사실만, 원본 PII는 마스킹된 형태로
```

block 시 응답:
```json
{
  "status": "blocked",
  "reason": "input_pii_blocked",
  "blocked_categories": ["rrn"],
  "message": "주민등록번호로 보이는 정보가 포함되어 있습니다. 개인정보를 제거하고 다시 시도해 주세요."
}
```

### 4. chat_logs PII 마스킹 정책 (Layer 2)

`chat_logs`에 다음 컬럼 추가 (ADR-019 PostgreSQL migration):

```sql
ALTER TABLE chat_logs
  ADD COLUMN input_pii_found JSONB DEFAULT '[]',
  -- [{category, severity, position, masked_form}, ...]
  ADD COLUMN output_pii_masked JSONB DEFAULT '[]',
  ADD COLUMN pii_storage_policy VARCHAR(20) DEFAULT 'mask';
```

#### `pii_storage_policy` enum (공식 정의)

| 값 | 의미 | 전이 |
|---|---|---|
| `mask` | chat_logs.question·answer에 PII 마스킹 적용 후 보관. 원본은 PII detection 후 즉시 폐기. **기본값** | `plain` 승인 시 → `plain` (platform_admin 명시 승인, §8 ledger event) |
| `plain` | 원본 PII 그대로 보관 (보안 사고 조사 등 합법 사유). | 운영 종료 시 `mask`로 회수 가능 / right-to-erasure 시 `erased`로 전이 |
| `erased` | 사용자 right-to-erasure(§10) 후 상태. question/answer NULL + citations/retrieved_chunks 빈 배열 + input_pii_found/output_pii_masked 빈 배열 + user_id NULL. 운영 지표(confidence/latency/routing_decision 등)는 보존. | 단방향 (다시 mask/plain으로 복원 불가) |

기본 정책: `mask`. `plain`은 platform_admin 명시 승인(POST `/api/platform/admin/tenants/{tid}/pii-storage-approvals`) 시에만. `compliance_mode='gdpr_strict'`은 어떠한 승인에도 `mask` 강제(§10).

`erased`는 right-to-erasure 흐름의 종착 상태로, ADR-021 §3에 등록될 자동 보관 정책 cron(`pii.storage.pii_masked_after_days`)이 *plain → mask 자동 전환*만 책임지고 `erased` 전이는 사용자 명시 요청에만 일어난다.

### 5. 인덱싱 단계 PII Detection (Layer 3)

문서 chunk 색인 시:

```python
# packages/rag_core/indexing/pii_check.py
def check_chunk_pii(chunk: Chunk) -> list[PIIWarning]:
    detector = PIIDetector.load(configs.platform.pii)
    findings = detector.scan(chunk.content)
    return [PIIWarning(category=f.category, severity=f.severity, position=f.position)
            for f in findings if f.severity in ("medium", "high")]

# chunks 테이블에 pii_warnings JSONB 컬럼 추가
# Qdrant payload에도 pii_warnings 보관 (검색 결과 응답 시 marker 노출)
```

검색 결과의 답변 카드(ADR-016)에 PII 경고 marker:

```text
근거 [1] 보안매뉴얼.pdf p.12 ⚠ (개인정보 예시 포함)
```

운영자가 admin UI에서 PII 경고 chunk 일괄 조회·검토·재가공 가능.

### 6. 응답 PII Masking (Layer 4)

LLM 응답에 PII가 새는 경우 (예: chunk에 주민번호 예시가 있고 LLM이 그대로 인용):

```python
# LangGraph 노드 `mask_response_pii` 추가 (assemble_response 직후)
def mask_response_pii(state):
    detector = PIIDetector.load(state.tenant_config.pii)
    masked, findings = detector.mask(state.final_answer)
    state.final_answer = masked
    state.metadata.output_pii_masked = findings
    return state
```

마스킹 후에도 원래 의도가 보존되어야 함 (예: "주민번호 ***-***-***** 형식으로 작성"). 검증·테스트 셋으로 정확도 모니터링.

### 7. WiSentinel 통합 — 선택적 audit subscribe (직접 호출 X)

```yaml
# configs/platform/audit.yaml
wisentinel:
  enable: false                      # 운영 환경에 WiSentinel 있을 때만 true
  redis_url: redis://wisentinel-redis.local:6379
  publish_channel: audit:capture     # WiSentinel 큐와 호환
  publish_events:
    - chat_response                  # DomainRAG chat 응답 이벤트
    - pii_detection                  # PII 탐지 이벤트
    - fallback_triggered             # fallback 발동
  payload_format: wisentinel_v1      # WiSentinel schema 호환
```

DomainRAG 측은 **publish만** (subscribe 안 함). WiSentinel·다른 audit 시스템이 통합 분석 가능.
DomainRAG 자체 chat_logs는 그대로 PostgreSQL에 보관 (이중 저장).

WiSentinel의 `inspect-content` 동기 호출은 **사용 안 함** — 운영 결합 위험 회피.

### 8. AuthFusion Ledger 통합 — 보안 이벤트만

DomainRAG 보안 관련 이벤트(예: tenant_id mismatch 403, 인증 실패, hard delete)는 AuthFusion Ledger (port 8089)에 publish:

```yaml
audit:
  authfusion_ledger:
    enable: true
    endpoint: http://authfusion-ledger.local:8089
    api_key_keyhub_ref: keyhub://domainrag/authfusion-ledger-token
    events:
      - auth_failure
      - tenant_mismatch
      - hard_delete
      - platform_admin_action
```

AuthFusion Ledger는 hash chain 기반 무결성 보장 (FAU_STG.1) — 사고 조사·감사 시 단일 통합 ledger 활용.

### 9. PII 보관 기간 정책

```yaml
# configs/platform/data_retention.yaml
chat_logs:
  default_retention_days: 90
  per_tenant_override: true        # tenant 별 overrides.yaml에서 조정 가능
  pii_masked_after_days: 7         # 7일 후 input_pii_found 원문 PII는 마스킹된 form만 유지
documents:
  retention_policy: indefinite     # 문서 자체는 별도 정책 (ADR-012)
indexing_jobs:
  retention_days: 30
```

월별 cron으로 retention 만료 row archival/삭제. archival은 별도 cold storage(예: MinIO 별도 bucket)로 이전.

### 10. 컴플라이언스 모드

```yaml
# configs/tenants/<tenant_id>/overrides.yaml
compliance_mode: gdpr_strict       # standard | gdpr_strict | hipaa_strict (도메인별)
```

`gdpr_strict`/`hipaa_strict` 시:
- Layer 1 입력 PII high → block 강제 (override 불가)
- chat_logs.pii_storage_policy = 'mask' 강제
- 사용자 right-to-erasure API 노출 (`DELETE /api/{tenant_id}/me/chat_logs`)
- 보관 기간 단축 (예: 30일)

기본은 `standard` (위 §3·4·9 기본값).

---

## Consequences

### 긍정적 영향

- M1 PII 공백 day 1 해결
- WiSentinel과 운영 결합 0 — 가용성·latency 의존 회피
- WiSentinel `dlp-core` 룰 자산 활용으로 휠 재발명 회피
- AuthFusion Ledger 통합으로 보안 사고 조사 시 단일 audit chain
- 컴플라이언스 모드로 도메인별 규정(GDPR·HIPAA·국내 개인정보보호법) 차등 대응

### 부정적 영향 / 부채

- 4-layer PII detection으로 응답 latency 추가 (~30~80ms)
- dlp-core 룰 한국어 정확도 검증 필요 (false positive·false negative 모두 위험)
- chat_logs 컬럼 추가로 storage 약간 증가
- 응답 마스킹이 LLM 답변 의미 훼손 가능 (예: "이 부분은 주민번호 예시였습니다"가 마스킹 후 의미 불명)
- WiSentinel publish channel 사용 시 Redis 가용성 의존 (단, publish 실패해도 chat_logs는 보존되므로 critical은 아님)
- right-to-erasure 구현 시 chat_logs hard delete 절차 (ADR-012 hard delete 정책과 정합 필요)

### 후속 작업

- `packages/rag_core/pii/` 모듈 구현
- `configs/platform/pii.yaml` + `audit.yaml` + `data_retention.yaml` 신설
- chat_logs 컬럼 추가 Alembic migration
- chunks 테이블 `pii_warnings JSONB` 컬럼 추가
- LangGraph에 `mask_response_pii` 노드 추가 (assemble_response 직후, Gate 2 직전)
- 운영자 admin UI에 "PII 경고 chunk 검토" 메뉴 추가 (ADR-016 보강)
- 평가 셋: 한국어 PII detection accuracy 측정 (전용 평가 dataset 구축)
- WiSentinel publish 실험 (운영 환경 가능 시)
- AuthFusion Ledger 통합 테스트
- right-to-erasure API 명세 (ADR-017 보강)

---

## Alternatives Considered

### 1. WiSentinel `inspect-content` 동기 호출 (직접 통합)
- **장점**: PII detection 책임 위임, 룰 항상 최신
- **기각 사유**: 매 요청 동기 호출은 운영 결합·latency·가용성 의존 ↑. 사용자 결정으로 회피.

### 2. PII detection 미도입 (chat_logs raw 저장)
- **장점**: 구현 부담 0
- **기각 사유**: M1 공백 그대로, 컴플라이언스 위반.

### 3. 외부 PII detection SaaS (예: Google DLP)
- **장점**: 정확도 높음
- **기각 사유**: 폐쇄망 정책 위반.

### 4. LLM 자체에 PII 마스킹 prompt
- **장점**: 별도 모듈 0
- **기각 사유**: LLM 신뢰성 의존, 결정론 부족, prompt injection으로 우회 가능.

### 5. NLP 기반 NER (Named Entity Recognition)
- **장점**: 정규식 한계 보완 (인명·주소 등)
- **기각 사유**: 추가 모델 부담. 본 시스템은 NER을 지원하지 않는다. 정규식이 부족하다는 운영 증거가 누적되면 별도 ADR을 작성한다.

### 6. WiSentinel과 동일 audit 시스템 단일화 (DomainRAG chat_logs 폐기)
- **장점**: 통합 단일 source
- **기각 사유**: WiSentinel은 외부 AI 사용 audit 도구 — DomainRAG의 풍부한 메타데이터 schema와 부합 안 함.

---

## Related

- [ADR-001: Citation 메타데이터 설계](./001-citation-metadata-design.md) — chunk 메타데이터에 pii_warnings 추가
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — tenant 별 compliance_mode
- [ADR-010: Citation Trust v2](./010-citation-trust-v2.md) — Layer 4 마스킹 노드 추가
- [ADR-012: Lifecycle v2](./012-lifecycle-v2.md) — right-to-erasure는 hard delete와 정합
- [ADR-013: Model Routing](./013-model-routing-and-slm-llm-strategy.md) — prompt injection 방어 prompt 템플릿
- [ADR-016: UI Architecture](./016-ui-architecture.md) — PII 경고 marker·검토 메뉴
- [ADR-017: API Specification](./017-api-specification.md) — right-to-erasure API
- [ADR-018: SSO Integration](./018-sso-integration-authfusion.md) — AuthFusion Ledger 통합
- [ADR-019: Infrastructure Sharing](./019-infrastructure-sharing.md) — Redis·KeyHub·Ledger 가용성 의존
- WiSentinel `packages/dlp-core` (외부) — 룰 source

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted (M1 PII 공백 해결)
