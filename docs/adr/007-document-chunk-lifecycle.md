# ADR-007: Document/Chunk Lifecycle (재업로드·버전·재색인·삭제)

## Status
**Superseded by ADR-012** (2026-05-08)

> **본 ADR은 [ADR-012: Lifecycle v2](./012-lifecycle-v2.md)에 의해 supersede됨.** ADR-012가 본 ADR의 모든 결정(chunk_id 재현성, 재업로드 idempotent, 버전 전이, 부분 실패, soft/hard delete, approval 자동화, 임베딩 alias swap, 메타데이터-only 갱신)을 흡수하고 multi-tenant 적용 + 이전에 deferred 처리된 항목들(chunks archival, old collection 보관 기간, schema 진화 migration) 완전 설계 + 신규 항목(tenant 자체 lifecycle, hard delete cross-system 일관성, re-index 4-mode, backup/restore per tenant)을 추가한다. **단일 진실 소스는 ADR-012**.
>
> 본 ADR은 historical reference로 보존되며, 신규 코드/SPEC 갱신은 ADR-012를 인용해야 한다.

---

## Context

### 배경 (사실)

- SPEC.md v1 §6.2 chunk_id 형식 `<doc_id>:<doc_version>:chunk:<index>`은 **reparse·parser 변경에 대해 충돌 회피 보장이 없음**.
- chat_logs audit replay 요구 — 사용자가 답변 받은 시점의 chunk 내용을 추적할 수 있어야 함 (ADR-005가 `chat_logs.citations[].excerpt` 보관 정책 도입).
- 운영 시나리오가 SPEC에 미정의: 재업로드, 버전 갱신, 부분 실패, 비활성화/삭제, approval 전이.
- ADR-006 (bge-m3 dense+sparse 통합 + Qdrant 단일 collection)이 임베딩 모델 교체 부담을 단순화.

### 가정

- Parser/Chunker는 같은 (doc, doc_version, parser_version) 입력에 대해 **결정론적 출력**을 보장 (Phase 1 구현 시 검증)
- `chat_logs.citations[].excerpt`가 audit truth source로 동작 (ADR-005)
- chunks 테이블의 `valid_until`/`approval_status`로 검색 노출을 제어할 수 있음

---

## Decision

### 1. chunk_id 재현성 (③-5)

```text
chunk_id = "<doc_id>:<doc_version>:<parser_version>:chunk:<index>"
예: DOC-2026-0001:v1:p1:chunk:0012
```

- chunks 테이블에 UNIQUE 제약: `(doc_id, doc_version, parser_version, chunk_index)`
- audit truth는 chat_logs.citations[].excerpt가 책임. chunk_id는 의미 추적용 ID.
- parser_version이 v1 → v2로 바뀌면 chunk_id도 자동으로 다른 namespace가 되어 충돌 회피.

### 2. 재업로드 (③-1)
- 동일 `file_hash` + 동일 `doc_id` + 동일 `version` → **idempotent no-op**. 기존 doc_id/job_id 반환.
- 다른 `file_hash` + 동일 `doc_id` 같은 `version` → **거절 (409 Conflict)**. 사용자가 명시적으로 version을 올려야 새 색인.
- 새 `doc_id` 또는 새 `version` → 정상 신규 색인.
- 자동 v2 강제는 의도치 않은 버전 인플레를 일으키므로 명시적 사용자 입력만 허용.

### 3. 버전 전이 (③-2)
- v2 indexing **완료 시점**에:
  1. v1 chunks의 `valid_until`을 v2 활성 시각으로 UPDATE
  2. Qdrant payload `set_payload`로 동일하게 동기화
  3. 검색 필터(`valid_until >= today`)가 자동으로 v1 제외
- v1 chunks 자체는 **hard delete 안 함** — chat_logs audit replay 보존 목적
- v2 indexing 실패 시 v1은 유지 (정상 운영 지속)

### 4. 부분 실패 처리 (③-3)
- chunk 단위 partial commit: 각 chunk가 (chunks INSERT + Qdrant upsert) 트랜잭션
- `indexing_jobs.indexed_chunks`는 **누적값**
- 실패한 chunk는 `indexing_jobs.failed_chunks` JSONB에 기록 + 자동 retry up to 3회
- **실패율 ≥ 30%면 fail-fast** (전체 rollback) — parser/embedder 시스템 문제 가능성
- retry는 chunk 단위이지 문서 재업로드가 아님 (③-1과 분리)

### 5. 삭제 정책 (③-6)
- **Soft delete가 기본**: `documents.approval_status = 'archived'`
  - chunks 테이블의 행은 보존
  - Qdrant payload `set_payload`로 동기화 → 검색 필터(`approval_status='approved'`)로 자동 제외
  - chat_logs는 그대로 보존 (audit)
  - 관리자 콘솔 "비활성화 버튼"이 이 경로에 매핑
- **Hard delete는 별도 admin API** (`DELETE /api/admin/documents/{doc_id}/hard`)
  - chunks 테이블 + Qdrant 모두에서 제거
  - 인용 chat_logs 처리 옵션 (요청 파라미터로 명시):
    - `keep_excerpts: true` (기본): chat_logs 그대로 보존, excerpt 자체가 audit
    - `mask_excerpts: true`: chat_logs.citations[].excerpt만 `[삭제됨]`으로 마스킹
    - `delete_logs: true`: 인용 chat_logs 행도 삭제 (컴플라이언스 요구 시)

### 6. Approval 전이 자동화 (③-8)
- `approval_status` 변경 시 chunks UPDATE + Qdrant `set_payload` **자동 호출**
- content 불변이므로 reindex 불필요
- approval API: `PATCH /api/admin/documents/{doc_id} {"approval_status": "approved"}`
- 검색 필터(§11.1 5번)는 `approval_status='approved'`만 통과 → payload 동기화로 즉시 검색 노출/제외

### 7. 임베딩 모델 교체 (③-4, ADR-006 보완)
- 새 모델(예: bge-m4) 도입 시:
  1. 새 collection 생성 (`chunks_v2`)
  2. 모든 chunks를 새 모델로 dense+sparse 동시 재생성하여 batch 인덱싱
  3. **Qdrant collection alias swap**으로 atomic 전환 (`chunks` alias → `chunks_v2`)
  4. 기존 `chunks_v1` collection은 일정 보관 후 삭제
- **Dual-read 정책**: 기본은 alias 단일. 평가 검증 동안에만 dual (Phase 2 운영 절차)
- chunks 테이블의 `embedding_model` 컬럼이 모델 일관성 단일 진실 소스 — 미스매치 query 방지

### 8. 메타데이터-only 갱신 (③-7)
- documents/chunks의 tags/department/security_level/acl 변경 시:
  1. PostgreSQL UPDATE (chunks 테이블)
  2. Qdrant `set_payload` 단일 호출 (vectors 불변)
- 두 시스템 dual-write 일관성: PostgreSQL 트랜잭션 commit 후 Qdrant 호출. 실패 시 retry 큐 + 알람.

---

## Consequences

### 긍정적 영향
- chat_logs audit replay가 chunk_id namespace + excerpt 양쪽으로 보장됨
- 재인덱싱 비용 최소화 (메타데이터 변경 시 vectors 보존, approval 전이 시 reindex 회피)
- 컴플라이언스 hard delete 요구를 별도 API로 분리해 일상 운영과 격리
- bge-m3 통합 collection 덕에 임베딩 모델 교체가 collection swap 한 번

### 부정적 영향 / 부채
- chunk_id에 parser_version 포함으로 ID 길이 증가 (가독성 약간 ↓)
- soft delete 누적 시 chunks 테이블 크기 증가 — 정기 archival 정책 별도 필요 (Phase 2)
- file_hash 충돌(매우 드묾) 시 idempotent no-op이 부정확할 가능성 — sha256이 충분히 안전하다는 가정
- 부분 실패의 retry 큐 운영 인프라(예: 별도 워커) 필요

### 후속 작업
- SPEC §6.2 chunk_id 형식 갱신 + 산출 규칙 명문화
- SPEC §7.1/§7.2 인덱스: `(doc_id, doc_version, parser_version, chunk_index)` UNIQUE
- SPEC §11.1 ACL 필터 조건에 `approval_status='approved' AND valid_until` 명시화
- 신규 admin API 명세 (SPEC §8): hard delete, approval patch
- indexing_jobs 테이블에 `failed_chunks JSONB`, `retry_count INT` 컬럼 추가
- chunks archival 정책 ADR (Phase 2 후보)

---

## Alternatives Considered

### 1. chunk_id에 content_hash 기반 (`DOC-...:v1:c:<sha256>`)
- **장점**: 완전 일대일, 무결성 강력 보장
- **기각 사유**: 위치 정보(index) ID에서 손실, 가독성 ↓. excerpt 보관으로 audit이 충분함.

### 2. 즉시 삭제 (v1 hard delete on v2 활성)
- **장점**: 깨끗한 상태 유지
- **기각 사유**: chat_logs audit replay 시 chunks 테이블에서 v1 사라져 추적 끊김.

### 3. 재업로드 = 새 버전 강제
- **장점**: 모호성 0
- **기각 사유**: 의도치 않은 버전 인플레, file_hash 동일 시 의미 없는 버전 폭발.

### 4. fail-fast (모든 부분 실패에서 전체 rollback)
- **장점**: 단순
- **기각 사유**: 큰 문서에서 단일 chunk 실패로 전체 재시작 비용 큼.

### 5. Hard delete만 사용 (soft delete 없음)
- **장점**: 데이터 정합 단순
- **기각 사유**: audit 무력화, 실수 비활성화 복구 불가.

### 6. Approval 변경 시 content reindex
- **장점**: vectors까지 완전 갱신
- **기각 사유**: content 불변인데 vectors 재생성은 낭비.

### 7. 임베딩 교체 시 dual-write (구·신 collection 동시 인덱싱)
- **장점**: 즉시 검색 가능
- **기각 사유**: indexing 비용 2배, 일관성 검증 부담. alias swap 방식이 검증 분리 측면에서 단순.

---

## Related
- [ADR-001: Citation 메타데이터 설계](./001-citation-metadata-design.md) — chunk/citation 필드 스키마
- [ADR-005: Citation 신뢰 모델](./005-citation-trust-and-fallback.md) — chat_logs.excerpt audit truth source
- [ADR-006: Hybrid Retrieval](./006-hybrid-retrieval.md) — collection 통합으로 모델 교체 단순화
- SPEC.md §6.2 chunk metadata / §7 데이터베이스 / §8 API — 폐기 ([ADR-012](./012-lifecycle-v2.md) + [ADR-017](./017-api-specification.md)이 supersede)

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted
