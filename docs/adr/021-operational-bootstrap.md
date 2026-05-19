# ADR-021: Operational Bootstrap (lifespan · LISTEN/NOTIFY · cron · Docker 시동)

## Status
**Accepted** (2026-05-15)

> 본 ADR은 ADR-009 §5 LISTEN/NOTIFY, ADR-012 §3 archival·§4 old collection hold, ADR-019 docker-compose 부트스트랩, ADR-020 §8 audit publish가 흩어 둔 운영 부트스트랩 결선을 단일 진실 소스로 통합한다. 백엔드 lifespan에서 무엇을 어떤 순서로 시동하는지·multi-instance 동기화 채널/페이로드·cron 스케줄·Docker 의존 그래프·tenant 등록 atomic rollback을 결정한다.

---

## Context

### 배경 (사실)

- ADR-009 §5는 "LISTEN/NOTIFY로 caches invalidate"를 결정만 했고 channel/payload는 비어 있다. `001_initial_schema.py`는 `pg_notify('tenant_config_changed')` 트리거를 *발사*하지만 backend에 LISTEN consumer가 없어 trigger가 dead상태다.
- `TenantConfigService._runtime_overrides` 와 `InputSchemaLoader._runtime_yaml`는 in-memory dict로 같은 프로세스만 즉시 반영되고 multi-instance·재시작 시 소실된다.
- `chunks` archival(ADR-012 §3, 90일)·`chat_logs` 월별 partition 생성·old Qdrant collection 30일 hold(ADR-012 §4) 모두 cron 의무가 있으나 ADR 어디에도 스케줄·lock 정책이 없다. 운영 가이드(`docs/operations/chat-logs-partitioning.md`)에 partition만 있고 다른 것은 없다.
- `docker-compose.yml`의 서비스 의존 그래프·healthcheck·alembic migration 실행 시점이 ADR에 명시되어 있지 않아 신규 환경 부트스트랩이 사람의 암묵 지식에 의존했다.
- 신규 tenant 등록은 5단계(tenants INSERT → AuthFusion client 등록 → Qdrant collection → MinIO prefix → configs seed)이며 도중 실패 시 rollback 책임이 정해져 있지 않다.

### 가정

- PostgreSQL 16 LISTEN/NOTIFY는 single-instance fan-out에 안정적이다 (검증된 사실, payload ≤8KB).
- backend replica는 단일 cluster·동일 PostgreSQL을 본다 — LISTEN을 모든 replica가 같이 듣는다.
- archival·partition 같은 *비 idempotent 또는 일회성* 작업은 PostgreSQL advisory lock으로 다중 instance 동시 실행을 차단할 수 있다 (검증된 사실).
- Docker Desktop / k8s deployment 모두 docker-compose healthcheck 의미와 동등한 readiness probe를 제공한다.

가정이 깨지면 — 특히 NOTIFY payload가 8KB 초과로 잘리거나 publish 폭증이 발생하면 — Redis Pub/Sub 또는 outbox + worker poll 패턴으로 별도 ADR.

---

## Decision

### 1. lifespan startup hook 책임

`backend/app/main.py:lifespan` 은 다음을 **이 순서로** 수행한다. 각 단계 실패는 fail-fast(앱 기동 중단).

```text
1. structured logging 초기화           (현재 작동)
2. settings + db_engine readiness probe (asyncpg.connect, 5초 안에 성공해야 함)
3. tenant_config_overrides 일괄 preload
   admin_engine으로 SELECT (BYPASSRLS): tenant_id/category/key/value/schema_version
   → 각 row를 TenantConfigService.apply_runtime_override(tid, category, deep_dict) 로 주입
   → 같은 프로세스 재기동 시 동일 상태 회복
4. tenant_input_schemas 일괄 preload (status='active' 만)
   admin_engine SELECT → InputSchemaLoader.apply_runtime_override(tid, yaml_content)
5. LISTEN consumer 등록 (asyncio Task로 백그라운드 실행)
   - LISTEN tenant_config_changed
   - LISTEN tenant_schema_changed
   - LISTEN tenant_lifecycle_changed (선택)
6. ArchivalWorker scheduler 등록 (config로 enable/disable 가능, 기본 enable)
7. ChatLogsPartitionService scheduler 등록 (월 1회)
8. health endpoint ready=True 게시
```

5번이 import 직후 동기 실행되지 않게 한다 — `asyncio.create_task` 로 분리, `lifespan` 종료 시 task cancel.

CLI/스크립트 실행(`python -m app.services.archival_worker`)은 lifespan을 거치지 않고 단독 진입 가능. lifespan task와 CLI는 advisory lock으로 상호 배제(§4 참조).

### 2. LISTEN/NOTIFY 채널·payload 스키마

| Channel | Trigger 위치 | Payload (JSON) | Consumer 책임 |
|---|---|---|---|
| `tenant_config_changed` | `PostgresTenantConfigOverrideService.patch` 트랜잭션 commit 직후 (`pg_notify`) | `{"tenant_id": str, "category": str, "key": str, "schema_version": int}` | `TenantConfigService.invalidate(tenant_id)` + `apply_runtime_override` 재호출 (admin engine 재조회) |
| `tenant_schema_changed` | `PostgresTenantInputSchemaRepository.insert_new_active` 트랜잭션 commit 직후 | `{"tenant_id": str, "schema_version": int}` | `InputSchemaLoader.clear_runtime_override(tenant_id)` + `apply_runtime_override` 재호출 |
| `tenant_lifecycle_changed` | `register/update_status/hard_delete` 트랜잭션 commit 직후 | `{"tenant_id": str, "old_status": str, "new_status": str}` | RAGService deps 재구성 — collection·storage prefix·configs 캐시 무효화 |

payload는 JSON 직렬화한 짧은 dict만 보낸다 (PostgreSQL NOTIFY payload 8KB 한계). 큰 값(yaml content 전체 등)은 *보내지 않고* consumer가 admin engine으로 재 SELECT.

**Consumer 동작 보장**:
- 동일 payload 중복 수신은 deep-merge·재로드의 멱등성으로 안전.
- LISTEN socket 단절 시 5초 backoff 후 재연결 + 재연결 직후 full preload(§1의 3·4단계) 1회 재실행으로 NOTIFY 누락 보전.
- 자기 자신이 발사한 NOTIFY도 자기가 다시 받는다 — 무방향. 같은 인스턴스도 LISTEN을 받으면 그냥 갱신하면 된다(이미 in-memory에 적용됐어도 멱등).

**Publish 책임**:
- in-process 발신 측은 PUT 트랜잭션 commit 직후 `await conn.execute("SELECT pg_notify($1, $2)", channel, json.dumps(payload))` 호출.
- migration의 PostgreSQL trigger(`001_initial_schema.py`에 존재)는 *보조 안전망* — application path가 비정상이거나 운영자가 DDL을 직접 변경했을 때 backup. 일반 운영 path는 application notify를 단일 진실 소스로 본다.

### 3. archival · partition · old collection cron

각 작업은 PostgreSQL advisory lock으로 single-runner 보장(다중 instance 또는 in-app schedule + CLI 동시 실행 안전).

| 작업 | 스케줄 | Advisory lock id | Lock 미획득 시 |
|---|---|---|---|
| `chunks` archival (ADR-012 §3) | 매일 03:00 KST (`0 3 * * *`) | `0x7019_0301` (70190301) | silent skip — 다른 instance가 처리 중 |
| `chat_logs` 다음달 partition 생성 | 매월 1일 00:30 KST (`30 0 1 * *`) | `0x7019_0612` (70190612, 기존 코드) | silent skip |
| 임베딩 마이그레이션 후 old collection drop 후보 알림 (ADR-012 §4) | 매주 일요일 04:00 KST (`0 4 * * 0`) | `0x7019_0401` | silent skip — 자동 drop 아님, platform_admin 알림 |
| `chat_logs` PII 자동 mask 전환 (ADR-020 §9) | 매일 04:00 KST (`0 4 * * *`) | `0x7019_0202` | silent skip — `pii_storage_policy='plain' AND created_at < NOW() - pii_masked_after_days` 행을 `mask`로 전이 |

**`chat_logs` partition self-heal**: cron 실패로 다음달 partition이 누락된 채 다음달 1일을 맞으면 INSERT가 partition 부재로 실패한다. 대비:
- backend `health/ready` (§6) 의 `partition` check가 *현재달 + 다음 1개월* partition 존재 여부 확인. 누락이면 503 → k8s가 traffic 차단.
- 또는 `PostgresChatLogWriter.write` 첫 진입에서 `pg_get_partitions` 검증 + 누락 시 `ensure_partition()` 호출(idempotent + advisory lock 보호) — fail-safe.
- 결정: backend 첫 write에서 self-heal 시도(application-level 안전망) + cron 실패 시 health/ready로 표면화.

scheduler 채택:
- **default**: lifespan에서 `asyncio.create_task(periodic_runner(...))` 로 등록. 각 task는 다음 실행 시각까지 sleep 후 advisory lock 시도. 단순함과 외부 의존 0이 장점.
- **외부 cron / k8s CronJob 우선 모드**: settings `OPS_CRON_MODE=external` 일 때 lifespan 등록 skip — 운영자가 CronJob으로 `python -m app.services.archival_worker` 등을 호출.

#### 환경변수 명세

| 환경변수 | 값·의미 | 기본값 |
|---|---|---|
| `OPS_CRON_MODE` | `internal` (lifespan task 등록) / `external` (외부 cron 단독) | `internal` |
| `OPS_CRON_ARCHIVAL_INTERVAL_SECONDS` | `internal` 모드의 archival 잡 호출 간격(초). 정밀 cron은 `external` 권장 | `86400` (24h) |
| `OPS_CRON_PARTITION_INTERVAL_SECONDS` | `internal` 모드의 chat_logs 파티션 잡 호출 간격(초) | `86400` (24h) |

`internal` 모드는 interval 기반(시간 정밀도 ≒ process 시작 시각 + N·interval)이라 운영(precise cron 필요)에선 `external` 권장. 다중 backend instance여도 advisory lock으로 단일 실행 보장.

archival worker는 tenant별 `lifecycle.yaml`의 `chunks_archive_days` 를 read해 threshold 결정 (ADR-012 §3·§13).

### 4. Docker 시동 순서·의존 그래프

`docker-compose.yml`(workspace root)의 서비스 의존:

```text
postgres (healthcheck: pg_isready)
   ↓
   ├─ init_postgres.sql (docker-entrypoint-initdb.d, 1회만)
   │     - pgcrypto / pg_trgm extension
   │     - domainrag_app / domainrag_platform_admin role 생성
   │     - DB grants
   │
   ↓ (healthy)
qdrant, minio  (병렬, 모두 healthcheck)
   ↓
backend (compose 'depends_on: condition: service_healthy')
   ├─ entrypoint: alembic upgrade head  (admin role로 실행)
   │     - 모든 핵심 테이블·인덱스·RLS policy
   │     - chat_logs partitioning 부모 + 현재달 partition
   ├─ uvicorn 시동 → lifespan §1
   └─ ready
   ↓
frontend (depends_on: backend, healthcheck)

vllm·tei-embedder·tei-reranker (profile: gpu)
   - GPU 자원이 있는 호스트에서만 `--profile gpu` 로 활성
   - backend는 RAG_BACKEND 환경변수로 분기 — gpu profile 없으면 RAG_BACKEND=inmemory
```

**alembic upgrade는 backend entrypoint 책임**. Dockerfile은 alembic을 실행하지 않고 image build에 포함만. compose에서 `command:` 또는 별도 init container(`migrator` profile)로 가동. 운영(k8s)에서는 `kubectl apply -f migration-job.yaml` 별도 step.

### 5. tenant 등록 atomic rollback ([ADR-018 §10](./018-sso-integration-authfusion.md) 정합)

`infra/scripts/tenant_register.sh` 단계와 실패 시 rollback (역순). ADR-018 §10이 AuthFusion client 자동 등록을 진실 소스로 정의했으므로 본 ADR은 그 절차를 흡수·확장한다.

```text
Step 0. SELECT tenants WHERE id=… (must be 0건)  — 조건 미달 시 early exit (Y5)
Step 1. AuthFusion OAuth2 client 자동 등록 (Admin API: POST {authfusion}/api/v1/clients) — ADR-018 §10
Step 2. tenants INSERT (status='active', delete_status=NULL)  — DB transaction begin
Step 3. configs/tenants/<id>/ seed (template 복사 + input_schema·model.yaml·routing.yaml + auth.yaml(client_id 포함))
Step 4. Qdrant client.create_collection(f"chunks_{tid}") + items_{tid} (assessment 모듈 활성 시)
Step 5. MinIO client.make_prefix(<id>/) — bucket 단일, prefix 키 등록만
Step 6. tenant_input_schemas v1 active 시드
Step 7. DB transaction commit + pg_notify('tenant_lifecycle_changed', {…})
Step 8. tenant_lifecycle_logs INSERT 'tenant_registered' (admin engine) + Ledger publish
```

**중간 실패 시 rollback (rollback_order = [step6 → step1])**:
- Step 6 실패 → DELETE FROM tenant_input_schemas WHERE tenant_id=…
- Step 5 실패 → `mc rb --force` 또는 marker 객체 제거
- Step 4 실패 → Qdrant collection drop 시도 (idempotent)
- Step 3 실패 → `rm -rf configs/tenants/<id>` (git untracked 한정 — 이미 commit된 경우 별도 PR)
- Step 2 실패 → DB transaction abort + rollback
- Step 1 실패 → AuthFusion DELETE /api/v1/clients/<client_id> (best-effort, swallow + dead-letter)

스크립트는 `set -euo pipefail` + 각 step에 `trap` 등록으로 자동 rollback. 부분 성공으로 끝나면 `tenant_register_failures` dead-letter(별도 테이블) 누적 + platform_admin 알림.

**사람의 수동 단계**: 사용자 IdP 등록(어느 user를 어느 client에 묶을지)은 AuthFusion 운영자 책임 — DomainRAG가 자동화하지 않는다 (ADR-018 §10 §"사후 작업").

### 6. health endpoint 의미

`GET /health` 는 다음 모두 OK일 때만 200:

| 항목 | 조건 |
|---|---|
| db | admin engine connect ≤ 1초 |
| qdrant | client.health() ≤ 1초 |
| storage | minio bucket exists ≤ 1초 |
| config_listener | LISTEN task 활성 (last NOTIFY received OR socket alive) |
| migration | alembic_version 테이블의 현재 version ≥ codebase 기대 version |

`/health/ready` 와 `/health/live` 분리(k8s 패턴):
- `/health/live`: process 자체 살아있음 — DB 검증 없음
- `/health/ready`: 위 모든 항목 OK — 트래픽 라우팅 가능

### 7. KeyHub·vLLM·Ledger 외부 의존성

이들은 lifespan에서 *connect 시도하지 않는다*. 첫 실제 호출 시 lazy connect + retry (ADR-019 §8 KeyHub, ADR-020 §8 Ledger). 이유:
- AuthFusion이 가동 중이지 않은 staging에서도 DomainRAG backend 자체는 기동 가능.
- vLLM은 GPU 부팅에 분 단위 걸린다. backend가 vLLM ready를 기다리면 cold start 문제.

대신 lifespan 직후 *background probe task* 가 30초 간격으로 endpoint health를 polling + `/health/ready` 의 상세 정보에 표기.

---

## Consequences

### 긍정적 영향

- 운영 부트스트랩 결정이 한 ADR에 모임 — 다음 세션에서 onboarding하는 사람이 ADR-021 한 편만 보면 절차 전체 파악.
- LISTEN/NOTIFY 결선으로 multi-instance config·schema 동기화 완성. PUT 후 다른 instance에서도 즉시 반영.
- archival·partition·collection-drop cron이 advisory lock으로 안전하게 분산 운영 가능 — multi-instance 환경에서 중복 실행 위험 차단.
- Docker 시동 순서가 명문화되어 신규 환경 부트스트랩 실패가 reproducible.
- tenant 등록 atomic rollback으로 부분 성공 상태(예: collection은 생겼는데 DB는 없는) 발생 시 정확한 응급 처치 가능.

### 부정적 영향 / 부채

- lifespan이 무거워진다 — preload SELECT가 tenant·schema 수에 비례. 1000 tenant 기준 ≤2초 예상이나 초대형 운영 시 startup batch 분할 필요.
- LISTEN consumer를 단순 background task로 둠 — process가 SIGKILL되면 다음 startup에 NOTIFY 누락 가능. preload 재실행으로 cover하지만 실시간 미반영 윈도우(최대 재시작 시간) 존재.
- archival worker가 lifespan task일 때 backend 재시작이 작업 중단 = 안전. 단, 다음 instance가 받아 처리 — advisory lock 자동 해제로 자연 복구.

### 후속 작업

- ChatLogPartitionService와 ArchivalWorker가 lifespan task로도 외부 CronJob으로도 양쪽 다 호환되는지 시연 환경 점검.
- LISTEN payload schema가 8KB 한계를 넘는 케이스 (대량 schema migration?) — 모니터링 후 outbox 패턴 ADR 후보.
- KeyHub·Ledger lazy connect의 첫 호출 응답 시간 증가 측정 — 임계치 초과 시 lifespan에서 warm-up.

---

## Alternatives Considered

### 1. Redis Pub/Sub 도입

PostgreSQL NOTIFY 대신 Redis 사용. **거절** — 외부 의존 추가 가치 작음. NOTIFY로 충분히 처리 가능한 부하·payload 크기.

### 2. LISTEN consumer 없이 polling

backend가 5초 간격 SELECT로 변경 감지. **거절** — multi-tenant·multi-category로 query 부하 누적, 반영 지연.

### 3. archival/partition을 외부 CronJob 단일화

lifespan task 옵션 제거. **거절** — 단일 호스트 dev 환경에서 cron 등록 추가 부담. 두 모드 모두 지원하되 advisory lock으로 안전 보장이 더 합리적.

### 4. tenant 등록을 단일 saga 트랜잭션화

분산 saga 패턴으로 전 step 자동 retry. **거절** — 복잡도 대비 가치 작음. 등록은 빈번 작업 아니고 운영자 수동 개입이 자연.

### 5. health endpoint를 단순 200

ready/live 분리 없이 단일 endpoint. **거절** — k8s rolling deploy에서 false-ready로 traffic 라우팅 위험.

---

## Related

- [ADR-009: Tenant Control Plane §5](./009-tenant-control-plane.md) — LISTEN/NOTIFY 채널이 본 ADR §2로 흡수
- [ADR-012: Lifecycle v2 §3·§4·§6](./012-lifecycle-v2.md) — archival·old collection·hard delete cross-system 일관성, 본 ADR §3에서 cron 결선
- [ADR-015: Tenant Input Schema §9](./015-tenant-input-schema.md) — schema 변경 NOTIFY 채널이 본 ADR §2에 정의
- [ADR-018: SSO §10](./018-sso-integration-authfusion.md) — tenant 등록 시 AuthFusion client 등록 step 책임 분담
- [ADR-019: Infrastructure §2·§4·§8](./019-infrastructure-sharing.md) — RLS · vLLM · KeyHub 운영, 본 ADR이 Docker 시동 그래프로 흡수
- [ADR-020: PII & Audit §8](./020-pii-and-audit-integration.md) — Ledger publish, lifespan에서 lazy connect

---

**작성자**: AI Assistant + Project Owner
**최종 승인**: 2026-05-15
**변경 이력**: 초안 작성, 즉시 Accepted (운영 부트스트랩 결정을 단일 진실 소스로 통합)
