# ADR-012: Document/Chunk Lifecycle v2 — Multi-tenant + Complete Design

## Status
**Accepted** (2026-05-08)

> **Supersedes ADR-007**. ADR-007의 8가지 lifecycle 결정(chunk_id 재현성, 재업로드 idempotent, 버전 전이, 부분 실패, soft/hard delete, approval 자동화, 임베딩 alias swap, 메타데이터-only 갱신)을 흡수하고 multi-tenant 운영(ADR-008) + 이전 deferred 항목들(chunks archival, old collection 보관 기간, schema 진화 migration) 완전 설계 + 신규 항목(tenant 자체 lifecycle, cross-system 일관성, re-index 트리거 명세)을 추가한다. 본 ADR이 lifecycle 단일 진실 소스.

---

## Context

### 배경 (사실)

- ADR-007이 단일 tenant 가정으로 작성되었고, 여러 항목이 phasing(Phase 2 후보)으로 deferred됨.
- ADR-008(Multi-Tenant Architecture)이 tenant scope를 도입하면서 모든 lifecycle 작업에 tenant_id 1차 분기 필요.
- 본 프로젝트 정책: 완제품 단일 설계, MVP/Phase 분리 금지 (메모리 참조).
- 도메인 특성: 보안·법무는 audit 보존이 강하게 요구되며, 시험 문제·설비는 사용 빈도·정확도 추적이 중요.
- chat_logs.citations[].excerpt가 audit truth source (ADR-005/010) — chunk가 archive되어도 audit replay 가능.

### 가정

- PostgreSQL Alembic migration 도구 사용 가능
- Qdrant collection snapshot/alias 운영 가능
- MinIO `mc cp`로 prefix 백업·복구 가능
- 운영자가 90일/30일 정책을 configs로 조정 가능 (ADR-009 control plane)

가정 중 하나라도 깨지면 재검토 — 특히 PostgreSQL이 Alembic 외 도구 강제면 migration 절차 재설계.

---

## Decision

### 1. tenant scope (모든 lifecycle 작업의 1차 분기)

- 모든 lifecycle 작업이 `tenant_id` 명시 필수: 재업로드, 재색인, 삭제, approval 전이, 모델 교체.
- chunk_id 형식: `<doc_id>:<doc_version>:<parser_version>:chunk:<index>` (ADR-007 그대로). tenant 격리는 collection 이름(`chunks_<tenant_id>`) + PostgreSQL `tenant_id` 컬럼이 책임. chunk_id에 tenant_id 추가하지 않음 (collection prefix가 자연스럽게 tenant 격리).
- chunks 테이블 UNIQUE 제약: `(tenant_id, doc_id, doc_version, parser_version, chunk_index)`.
- Cross-tenant lifecycle 작업은 hard delete API 한정 (tenant 자체 삭제). 그 외는 정확히 한 tenant scope.

### 2. Tenant 자체의 lifecycle

`tenants.status` enum 확장:

| 상태 | 의미 | 검색 가능 | 복구 가능 | 권한 |
|---|---|---|---|---|
| `active` | 정상 운영 | yes | — | tenant admin |
| `suspended` | 일시 정지 (점검·보안 사고) | no | platform_admin | platform_admin |
| `archived` | 비활성, 데이터 보존 | no | platform_admin | platform_admin |
| `deleted` | hard delete 완료, 복구 불가 | no | no | platform_admin |

전이 절차:
- `active → suspended`: 검색 즉시 차단, chat_logs 보존
- `active → archived`: 검색 차단, chat_logs 보존, 평가·관리 콘솔에서 비활성 tenant 표시
- `archived → deleted`: ADR-008 §5 hard delete API 호출 (cross-system 일관성 §6 참조)
- 모든 전이는 `platform_admin` role만 가능, `tenant_lifecycle_logs` 테이블에 자동 기록

### 3. chunks archival 정책

```yaml
# configs/platform/lifecycle.yaml (모든 tenant 기본)
chunks_archival:
  enable: true
  archive_after_days: 90        # soft delete (valid_until 만료)된 chunks를 90일 후 archive
  archive_target: postgres_archive_table   # postgres_archive_table | minio_cold
  delete_after_archive_days: null          # null = archive 영구 보존, 정수면 N일 후 hard delete

# configs/tenants/<id>/overrides.yaml
# chunks_archival.archive_after_days: 30   # 도메인이 audit 요구 약하면 단축
```

**저장소 옵션**:
- `postgres_archive_table`: `chunks_archive` 테이블로 row 이동 (RLS 동일 적용)
- `minio_cold`: `<tenant_id>/_archive/<chunk_id>.json` Parquet 또는 JSONL로 export

**chat_logs 영향**: `excerpt`가 audit truth (ADR-010)이므로 archive로 이동해도 chat_logs 정상. 단 archive 검색은 admin 콘솔에서만 가능.

**자동화**: 매일 자정 실행되는 `archival_worker` 잡:
```text
1. WHERE valid_until < NOW() - INTERVAL '90 days' AND archived_at IS NULL
2. Move chunks to chunks_archive (또는 export to MinIO)
3. UPDATE chunks SET archived_at = NOW(), payload removed in Qdrant (set_payload)
4. Audit log
```

> **[ADR-021 §3](./021-operational-bootstrap.md) 정합**: cron 스케줄·advisory lock id·`OPS_CRON_MODE` (internal/external) 전이는 ADR-021 §3가 진실 소스. 본 절은 정책(90일)·잡 로직만 결정한다.

### 4. Old Collection 보관 기간 (임베딩 모델 교체 후)

```yaml
# configs/platform/lifecycle.yaml
old_collection_retention:
  hold_days: 30                  # alias swap 후 30일 보관
  require_explicit_delete: true  # platform_admin 명시 승인 필요 (자동 삭제 안 함)
  dual_read: false               # 보관 기간 내 dual-read 활성 여부 — 기본 비활성
```

운영 절차:
1. 새 모델 collection 생성 (`chunks_<tenant_id>__v2`)
2. Batch 재인덱싱
3. 평가 통과 검증 (ADR-009 promotion_gate)
4. Qdrant alias swap (`chunks_<tenant_id>` → v2)
5. 30일 hold 기간 — `chunks_<tenant_id>__v1`은 보관, dual-read 비활성
6. 30일 후 platform_admin이 `DELETE` API 호출 → old collection drop
7. `tenant_collection_history` 테이블에 모든 swap 이력 기록

> **[ADR-021 §3](./021-operational-bootstrap.md) 정합**: 30일 경과 collection의 *자동 drop 검토 cron*은 ADR-021 §3 "임베딩 마이그레이션 후 old collection drop" 잡으로 운영된다. 본 절은 hold_days·require_explicit_delete 정책 결정만 정의하고, drop 실제 실행은 platform_admin 명시 호출이 우선이며 cron은 알림·후보 추출만 책임.

### 5. Schema 진화 — Alembic 기반 hot migration

**도구**: Alembic. `alembic/versions/` 디렉터리에 migration script 누적.

**규칙**:
- 모든 핵심 테이블에 `schema_version VARCHAR(20)` 컬럼 (chunks, documents, tenants, conversations, messages, chat_logs)
- **Hot migration 원칙**: NOT NULL 추가 시 default 동반 또는 nullable → backfill → NOT NULL 단계 분리
- **RLS 영향**: migration은 RLS bypass role(`migration_role`)로 실행. tenant 간 일관 적용 보장.
- **Rollback**: `alembic downgrade <revision>`. 각 migration은 reversible 작성 의무 (코드 리뷰 가이드)
- **Schema version 검증**: 애플리케이션 시작 시 `chunks.schema_version`과 코드의 expected version 비교, 불일치 시 startup 실패

**예시 migration script**:
```python
def upgrade():
    op.add_column('chunks', sa.Column('new_field', sa.String(100), nullable=True))
    op.execute("UPDATE chunks SET new_field = 'default_value' WHERE new_field IS NULL")
    op.alter_column('chunks', 'new_field', nullable=False)
    op.execute("UPDATE chunks SET schema_version = '2026-06'")

def downgrade():
    op.drop_column('chunks', 'new_field')
    op.execute("UPDATE chunks SET schema_version = '2026-05'")
```

### 6. Hard Delete Cross-System 일관성

> **[ADR-021 §5](./021-operational-bootstrap.md) 정합**: 4개 시스템(PostgreSQL/Qdrant/MinIO/chat_logs) 단계별 atomic rollback·dead-letter 누적·Ledger publish 절차는 ADR-021 §5 `tenant_register_failures` / hard_delete 대칭 구조와 동일 패턴. 본 절은 책임·순서를 명시하고 부분 실패 처리·관제는 ADR-021을 참조한다.

**Audit truth 보장 (원칙 10 + CLAUDE.md)**: hard_delete가 어느 step에서 실패하든 `chat_logs.excerpt` 컬럼은 **보존된다**. excerpt는 답변 시점의 인용 원문이며 chunk 비활성·archive·hard delete와 무관하게 audit replay의 단일 진실 소스다. `chat_logs_action='delete_logs'`를 운영자가 명시 선택해도 *해당 doc 참조 chat_log row 자체가 사라질 뿐* 다른 row의 excerpt에는 영향이 없다. 운영자는 right-to-erasure(ADR-020 §10) 외에는 excerpt를 임의로 지우지 않는다.

PostgreSQL + Qdrant + MinIO + chat_logs 4개 시스템 일관성:

```text
hard_delete_workflow(tenant_id, options):
  state = "started"
  
  1. tenants.delete_status := "in_progress"
  2. PostgreSQL: 
       a. soft tombstone (status='deleted', deleted_at)
       b. RLS bypass + DELETE FROM chunks/documents/etc WHERE tenant_id=...
       c. options.chat_logs_action 처리 (keep_excerpts | mask_excerpts | delete_logs)
  3. Qdrant: client.delete_collection(f"chunks_{tenant_id}")
  4. MinIO: mc rm --recursive bucket/<tenant_id>/
  5. tenants.delete_status := "completed", deleted_at := NOW()
  
  실패 처리:
  - 각 step에 retry 3회
  - retry 실패 시 dead-letter queue (tenant_delete_failures 테이블)
  - 운영 알람 + platform_admin 수동 개입
  - tenants.delete_status가 "in_progress" 상태로 stuck → 자동 cleanup worker가 6시간마다 점검
```

`tenant_delete_failures` 테이블:
```sql
CREATE TABLE tenant_delete_failures (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    failed_step VARCHAR(50),  -- postgres | qdrant | minio | chat_logs
    error_message TEXT,
    retry_count INT DEFAULT 0,
    last_attempt_at TIMESTAMP,
    resolved_at TIMESTAMP
);
```

### 7. Tenant 간 데이터 이관 — 명시적 미지원

> 본 시스템은 tenant 간 데이터 이관을 지원하지 않는다. 필요 시 source tenant에서 export → target tenant에서 정상 import lifecycle (재업로드 + 재인덱싱)으로 처리한다. 직접 row migration 같은 기능은 ACL·격리 의미를 흐리므로 제공하지 않는다.

### 8. Backup/Restore per Tenant + tenant_register vs tenant_restore (CLAUDE.md Y5)

신규 tenant 등록과 archived/deleted tenant 복원은 **다른 스크립트**다. 둘을 하나로 합치면 status 분기 책임이 모호해진다.

| Script | Precondition | Action |
|---|---|---|
| `infra/scripts/tenant_register.sh` | tenants 테이블에 해당 id가 *존재하지 않을 때만* (`SELECT … = 0`) | 새 row INSERT (status='active') + collection 생성 + configs/tenants/<id> seed (template 복사) + MinIO prefix 생성 + AuthFusion client 등록 (운영자 수동) |
| `infra/scripts/tenant_restore.sh` | tenant row가 *archived/deleted 상태이거나 부재할 때만* (`SELECT status` ≠ 'active') | Manifest 검증 → PostgreSQL data 복원 (BYPASSRLS) → Qdrant snapshot 복원 → MinIO mirror → configs 복원 → status 'active'로 갱신 |

두 스크립트 모두 시작 직후 status check를 통과하지 못하면 **early exit + non-zero return code**로 사람의 실수 차단. 운영자는 의도가 모호하면 platform_admin Console에서 status를 먼저 확인한다.

**Backup**:
```bash
# tenant_backup.sh <tenant_id> <output_dir>
1. PostgreSQL: pg_dump --where="tenant_id='<id>'" → schema + tenant rows
2. Qdrant: client.create_snapshot(f"chunks_{tenant_id}") → snapshot 파일 다운로드
3. MinIO: mc mirror minio/documents/<tenant_id>/ <output_dir>/minio/
4. configs: cp -r configs/tenants/<tenant_id>/ <output_dir>/configs/
5. Manifest: 모든 객체의 hash + timestamp + tenant 메타
```

**Restore**:
```bash
# tenant_restore.sh <backup_dir>
0. tenants.status 검증 — 'active'이면 거부 (충돌 위험). 'archived'/'deleted'/'미존재'만 허용
1. Manifest 검증 (무결성·테넌트 일치성)
2. tenants 테이블 갱신 (status=active, deleted_at=NULL)
3. PostgreSQL: psql -f schema.sql + 데이터 복원 (BYPASSRLS, platform_admin role)
4. Qdrant: client.recover_from_snapshot
5. MinIO: mc mirror
6. configs 복원
7. 검증 — chunk count + 임의 query 정합 확인
```

이 스크립트들은 day 1 제공.

### 9. Doc-level approval만 — Chunk-level 명시적 미지원

> 본 시스템은 doc-level approval만 지원한다 (`documents.approval_status`). chunk별 다른 승인 상태는 지원하지 않는다. 도메인이 chunk-level 승인을 요구하면 input schema에서 sub-document로 분리하여 등록 (예: 100조 약관을 1조-1문서 단위로 등록).

이 명시적 결정은 **input_schema 설계(ADR-015)와의 정합** 보장.

### 10. Indexing 동시성

- 같은 `(tenant_id, doc_id, doc_version)` 조합에 동시 indexing job 1개만 허용
- 동시 요청 시 409 Conflict + 기존 job_id 안내
- 다른 doc_id 또는 다른 tenant 간은 동시 가능 — `worker_pool` (configs로 동시성 제한)

```sql
-- indexing_jobs에 unique constraint
CREATE UNIQUE INDEX idx_active_indexing
  ON indexing_jobs (tenant_id, doc_id, doc_version)
  WHERE status IN ('pending', 'parsing', 'chunking', 'embedding', 'indexing');
```

### 11. Re-index 트리거 — 4가지 명세

관리자 콘솔의 "재색인 버튼"이 4가지 트리거 모드 제공:

| 모드 | 동작 | 사용 시점 |
|---|---|---|
| `parser_only` | parser 재실행, 메타데이터(page_number/section_title) 재추출, chunks UPDATE | parser 버그 수정 |
| `chunk_re_split` | chunking 재실행, chunks 새 chunk_index로 생성, 임베딩 재계산 | chunking 알고리즘 변경 |
| `embedding_only` | chunks 보존, vectors만 재계산 (Qdrant point upsert) | 임베딩 모델 교체 |
| `full` | parser → chunker → embedding 전부 | 문서 자체 변경 또는 전반 갱신 |

`parser_version`, `chunk_strategy`, `embedding_model` 컬럼이 모드별 변경 추적. chunk_id가 mode에 따라 변경 (parser_version 또는 chunk_index 변경 시).

### 12. ADR-007 모든 결정의 multi-tenant 적용 (요약)

ADR-007의 결정들은 본 ADR에서 tenant scope 추가 외 변경 없이 유효:

- **재업로드 idempotent**: tenant scope 내 file_hash dedup
- **버전 전이**: tenant scope 내 v1→v2, valid_until 만료
- **부분 실패**: tenant scope, 30% 실패율 fail-fast
- **임베딩 alias swap**: per-tenant collection alias swap (§4 정책)
- **메타데이터-only 갱신**: per-tenant chunks UPDATE + Qdrant set_payload
- **Soft delete**: per-tenant `documents.approval_status='archived'`
- **Hard delete (document)**: per-tenant scope (별도, tenant hard delete와 다름)

### 13. tenant_lifecycle_logs 테이블 (감사)

```sql
CREATE TABLE tenant_lifecycle_logs (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    -- created | activated | suspended | archived | deleted | model_swap | restored | ...
    actor VARCHAR(255),         -- platform_admin user_id
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);
```

모든 status 전이·model swap·hard delete가 자동 기록.

---

## Consequences

### 긍정적 영향

- ADR-007 모든 결정 + multi-tenant + 이전 deferred 항목들이 한 ADR에 정합
- 4가지 tenant status로 운영 단계별 명확한 권한·동작 정의
- chunks archival 정책으로 테이블 무한 증가 차단 (tenant 대형화 시점에도 안전)
- Schema 진화 절차 day 1 정립 — 운영 중 추가 컬럼 도입 부담 ↓
- Hard delete cross-system 일관성 절차로 데이터 누수·tombstone 위험 차단
- Backup/Restore per tenant로 tenant 단위 마이그레이션·DR 가능

### 부정적 영향 / 부채

- 구현 표면적 ↑ (archival worker, dead-letter queue, 4가지 reindex 모드, alembic migrations)
- `tenant_lifecycle_logs`, `tenant_delete_failures`, `chunks_archive`, `tenant_collection_history` 등 신규 테이블 다수
- Backup/Restore 스크립트 day 1 작성 부담
- Old collection 30일 보관이 Qdrant 디스크 비용 ↑ (tenant 다수 + 빈번한 모델 교체 시)

### 후속 작업

- ADR-007에 supersede 노트 추가
- 신규 테이블 마이그레이션 (Alembic): chunks_archive, tenant_lifecycle_logs, tenant_delete_failures, tenant_collection_history
- `archival_worker` 구현 (cron 또는 Celery beat)
- `hard_delete_workflow` 구현 (PostgreSQL → Qdrant → MinIO 순)
- `tenant_backup.sh`, `tenant_restore.sh` day 1 제공
- `configs/platform/lifecycle.yaml` 시드
- 관리자 콘솔에 4가지 reindex 모드 UI + tenant lifecycle 관리 메뉴 (ADR-009 admin console과 통합)
- alembic 환경 초기화 + 첫 baseline migration

---

## Alternatives Considered

### 1. Hard delete 즉시 실행 (cross-system 동기 호출)
- **장점**: 단순
- **기각 사유**: 일부 시스템 실패 시 inconsistent state. dead-letter queue + 명시적 retry가 안전.

### 2. Tenant migration 지원 (cross-tenant row 이동)
- **장점**: 조직 변경(부서 통합) 시 편의
- **기각 사유**: ACL 의미 흐려짐, RLS 가정 깨짐. export-import 우회로 충분.

### 3. Chunk-level approval 지원
- **장점**: 세밀한 거버넌스
- **기각 사유**: 데이터 모델 복잡도 ↑, input_schema와 모순. sub-document 분리로 우회 가능.

### 4. Schema 진화에 SQL migration 직접 (Alembic 미사용)
- **장점**: 외부 의존 0
- **기각 sayp**: rollback·version 추적·CI 통합 부담 ↑. Alembic이 표준.

### 5. archival 없이 chunks 영구 보존
- **장점**: 운영 단순, audit 보장
- **기각 사유**: tenant 대형화 시 chunks 테이블 무한 증가. 운영 비용 폭증.

### 6. archival을 MinIO Parquet 단독
- **장점**: 저장 비용 ↓
- **기각 사유**: archive 검색 시 Parquet 파싱 오버헤드. PostgreSQL `chunks_archive` 테이블이 admin 콘솔 친화. configs로 두 옵션 선택 가능하게 두는 것이 절충.

### 7. Old collection 30일이 아닌 dual-read 영구
- **장점**: 즉시 fallback 가능
- **기각 사유**: 두 collection 동시 검색은 score 정합 어려움 + 디스크 비용 2배. alias swap이 atomic 단순.

### 8. 4가지 reindex 모드를 단일 `full`로 통일
- **장점**: 단순
- **기각 사유**: 임베딩만 교체하는데 parser 재실행은 비용·시간 ↑. 모드별 trigger가 운영 효율 ↑.

---

## Related

- [ADR-001: Citation 메타데이터](./001-citation-metadata-design.md) — chunk/citation 필드 스키마
- [ADR-007: Document/Chunk Lifecycle v1](./007-document-chunk-lifecycle.md) — **superseded by 본 ADR**
- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — tenant scope 정의
- [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md) — lifecycle.yaml configs 위치
- [ADR-010: Citation Trust v2](./010-citation-trust-v2.md) — chat_logs.excerpt가 audit truth
- [ADR-011: Hybrid Retrieval v2](./011-hybrid-retrieval-v2.md) — collection 생성·alias swap 정합
- ADR-013 (예정): Model Routing — 임베딩 모델 교체 절차 정합
- ADR-015 (예정): Tenant Input Schema — sub-document 분리 정합
- [ADR-021: Operational Bootstrap](./021-operational-bootstrap.md) — 본 ADR §3·§4·§6 cron·atomic rollback·dead-letter 운영 책임
- SPEC.md §7, §8.4~8.8 — 폐기 (본 ADR + [ADR-017](./017-api-specification.md)이 흡수)

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted (supersedes ADR-007)
