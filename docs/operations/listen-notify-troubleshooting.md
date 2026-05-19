# LISTEN/NOTIFY 운영·트러블슈팅 (ADR-021 §2)

## 개요

multi-instance backend에서 `tenant_config_overrides` 또는 `tenant_input_schemas` PUT 직후 모든 instance가 즉시 새 값을 사용하도록, PostgreSQL `pg_notify` 3채널을 구독한다.

| Channel | Producer | Consumer 동작 |
|---|---|---|
| `tenant_config_changed` | `TenantConfigOverrideService.patch` (application) + 마이그레이션 001 trigger (보조) | `reload_tenant_config(tid)` — runtime layer 갱신 |
| `tenant_schema_changed` | 마이그레이션 008 trigger | `reload_tenant_schema(tid)` |
| `tenant_lifecycle_changed` | 마이그레이션 008 trigger | `TenantConfigService.invalidate(tid)` |

## 정상 동작 확인

```bash
# backend 1: tenant_config PUT
curl -X PATCH http://localhost:8001/api/security/admin/configs/routing \
  -H "Authorization: Bearer <admin>" \
  -d '{"key":"","value":{"default_route":{"model":"shared_llm"}}}'

# backend 2 로그에 즉시 표시되어야 함:
# {"event":"config_listener.applied","tenant_id":"security"}
```

수동 발사 테스트:
```sql
SELECT pg_notify('tenant_config_changed', '{"tenant_id":"security","category":"routing"}');
```

## 운영 확인 SQL

```sql
-- 1) 트리거 함수 존재
SELECT proname FROM pg_proc
 WHERE proname IN ('notify_tenant_config_changed',
                   'notify_tenant_schema_changed',
                   'notify_tenant_lifecycle_changed');

-- 2) 트리거 활성
SELECT tgname, tgrelid::regclass, tgenabled FROM pg_trigger
 WHERE tgname LIKE 'trg_tenant%';

-- 3) 현재 LISTEN 중인 channel
SELECT pg_listening_channels();
```

## 장애 시나리오 + 대응

### consumer가 NOTIFY를 못 받는다

원인:
- backend가 listener task 시동 실패 (lifespan 로그 `pg_notify_listener.started` 부재)
- DB connection 끊김 (5초 backoff 재연결 시도)

대응:
1. `docker compose logs backend | grep pg_notify_listener` — connected/disconnected 시점 확인
2. 강제 reload — backend 재시작 시 lifespan startup hook이 `preload_tenant_configs` 호출, 누락분 보전
3. 운영 수동 reload — `POST /api/{tid}/admin/configs/reload` 호출

### payload가 8KB 초과

PostgreSQL NOTIFY는 8KB 한계. payload는 항상 `{tenant_id, category, key, schema_version}` 같은 짧은 dict — 본 시스템 trigger·service 모두 dict만 보낸다. 운영자가 직접 큰 payload를 보내지 않도록 주의.

### multi-instance reload 폭증

대량의 patch가 짧은 시간에 발생하면 모든 backend가 동시에 reload SQL 실행 → 부하 증가. 대응:
- consumer 측 debounce(현재 미구현, 후속 ADR 후보)
- DB connection pool size 확대

## 자기 publish 자기 수신

NOTIFY는 발신자에게도 broadcast된다. consumer는 동일 payload를 두 번 받을 수 있으나, `apply_runtime_override`는 멱등 — deep merge 후 invalidate. 안전.

## DDL trigger vs application path

ADR-021 §2 결정: **application path가 진실 소스**. trigger는 운영자가 raw SQL로 row를 변경하는 비정상 경로 대비.

application path가 commit 직후 명시 `pg_notify`를 보낸다 (`TenantConfigOverrideService.patch:209` 등). trigger는 같은 commit에서도 발사되므로 consumer는 같은 알림을 2번 받을 수 있음 — idempotent하게 처리.

## Related

- [ADR-009 §5](../adr/009-tenant-control-plane.md) — LISTEN/NOTIFY 채택
- [ADR-021 §2](../adr/021-operational-bootstrap.md) — 채널·payload·재연결
- [migration 001](../../backend/alembic/versions/001_initial_schema.py) — tenant_config_changed trigger
- [migration 008](../../backend/alembic/versions/008_notify_schema_lifecycle.py) — schema/lifecycle trigger
