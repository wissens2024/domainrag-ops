# 115번 서버 부트스트랩 (기존 ai_aware_sse_postgres 공유)

ADR-019 §1 + `feedback_115_postgres_shared` 메모리 정합. DomainRAG가 *기존* `ai_aware_sse_postgres` 컨테이너의 PostgreSQL에 별도 `domainrag` database를 추가해 공유.

## 사전 조건 확인

```bash
# 1) ai_aware_sse_postgres 컨테이너 실행 중인지
docker ps --filter "name=ai_aware_sse_postgres" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 2) 컨테이너 network 확인 (compose 115 override의 external network 이름과 일치해야)
docker inspect ai_aware_sse_postgres --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}'

# 3) PostgreSQL version + 접근 가능 여부 (superuser credential은 ai-aware-sse 운영자가 보유)
docker exec -it ai_aware_sse_postgres psql -U postgres -c "SELECT version();"
```

## 1회만 적용: domainrag DB + 두 role 생성

```bash
# domainrag database 생성 (없을 때만)
docker exec -it ai_aware_sse_postgres psql -U postgres -c "CREATE DATABASE domainrag;"

# init_postgres.sql 적용 — 두 role + extension + grants
docker exec -i ai_aware_sse_postgres psql -U postgres -d domainrag \
  < infra/scripts/init_postgres.sql

# 확인
docker exec -it ai_aware_sse_postgres psql -U postgres -d domainrag -c "\du"
# domainrag_app (NOBYPASSRLS) + domainrag_platform_admin (BYPASSRLS) 확인
```

## docker compose 시동 (postgres 제외)

```bash
cd ~/workspace/domainrag-ops
git pull origin main  # 최신 코드 동기화

# docker-compose.115.yml override로 postgres service 비활성 + 외부 PG 가리킴
docker compose -f docker-compose.yml -f docker-compose.115.yml up -d \
  qdrant minio backend frontend
```

**중요**: `docker-compose.115.yml`의 `external: true` network 이름(`ai-aware-sse_default`)을 사전 조건 step 2에서 확인한 실제 이름으로 정정. 운영자가 직접 편집 후 commit.

## Alembic migration 적용

backend 컨테이너가 시동되면 자동으로 `alembic upgrade head` 실행하지 않는다(현재 entrypoint는 uvicorn 단독). 명시 실행:

```bash
docker compose -f docker-compose.yml -f docker-compose.115.yml exec backend \
  alembic upgrade head
```

또는 outside-of-container:
```bash
# host에서 직접 (psycopg + alembic 설치 필요)
POSTGRES_HOST=<ai_aware_sse_postgres-host-ip> \
POSTGRES_USER=domainrag_platform_admin \
POSTGRES_PASSWORD=changeme_admin \
POSTGRES_DB=domainrag \
  alembic -c backend/alembic.ini upgrade head
```

## 검증

```bash
# 1) 010개 migration 모두 적용됐는지
docker exec -it ai_aware_sse_postgres psql -U postgres -d domainrag \
  -c "SELECT version_num FROM alembic_version;"
# 기대: 010_tenant_register_failures

# 2) 핵심 테이블 + 신규 trigger 존재
docker exec -it ai_aware_sse_postgres psql -U postgres -d domainrag -c "
  SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;
"

docker exec -it ai_aware_sse_postgres psql -U postgres -d domainrag -c "
  SELECT proname FROM pg_proc
   WHERE proname IN ('notify_tenant_config_changed',
                     'notify_tenant_schema_changed',
                     'notify_tenant_lifecycle_changed');
"

# 3) backend lifespan 로그
docker compose -f docker-compose.yml -f docker-compose.115.yml logs backend | tail -30

# 4) health endpoints
curl http://115:8001/api/health/live
curl http://115:8001/api/health/ready
curl http://115:8001/api/health

# 5) Frontend
# http://115:3000 → /security/chat
```

## Integration test (testcontainers는 사용 안 함)

`tests/integration/test_postgres_smoke.py`는 testcontainers로 *임시* PostgreSQL을 띄운다. 115 환경에선 의미가 약하므로 *115용 별도 smoke test* 작성 또는 위 6단계 수동 검증으로 대체.

## 회수·정리

```bash
docker compose -f docker-compose.yml -f docker-compose.115.yml down
# ai_aware_sse_postgres는 *건드리지 않는다*. domainrag database·role만 보존됨.

# 혹시 완전 정리 필요 시 (주의)
docker exec -it ai_aware_sse_postgres psql -U postgres -c "DROP DATABASE domainrag;"
docker exec -it ai_aware_sse_postgres psql -U postgres -c "DROP ROLE domainrag_app;"
docker exec -it ai_aware_sse_postgres psql -U postgres -c "DROP ROLE domainrag_platform_admin;"
```

## 흔한 실패

| 증상 | 원인 | 대응 |
|---|---|---|
| `connection refused` backend → postgres | docker network 이름 불일치 | `docker network ls` 확인 + `docker-compose.115.yml` external network 이름 정정 |
| `password authentication failed` | init_postgres.sql 미적용 또는 password 변경 | `\du` 확인 후 `ALTER ROLE ... WITH PASSWORD ...` |
| alembic `relation "tenant_config_overrides" already exists` | 기존 migration이 남아 있음 | `SELECT version_num FROM alembic_version`로 현재 상태 확인 후 stamp |
| 010 dead-letter 테이블 부재 | migration 이전 버전에서 멈춤 | `alembic upgrade head` 재실행 |

## Related

- [ADR-019 §1·§2](../adr/019-infrastructure-sharing.md) — PostgreSQL 공유 인스턴스 + 두 role RLS
- [feedback_115_postgres_shared](../../../.claude/.../feedback_115_postgres_shared.md) — 새 PG 컨테이너 금지
- [docker-compose.115.yml](../../docker-compose.115.yml) — override 파일
