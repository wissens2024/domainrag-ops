# DomainRAG Ops — Docker Compose 시동 가이드 (ADR-021 §4)

폐쇄망 운영 환경의 부트스트랩 순서를 단일 가이드로 정리. dev/staging/prod 공통.

## 사전 조건

| 항목 | 요구 |
|---|---|
| Docker Engine | 24+ |
| Docker Compose plugin | v2.20+ |
| GPU 운영 (vLLM·TEI) | NVIDIA Container Toolkit + RTX 3080 10GB ×2 이상 |
| 디스크 | postgres·minio·qdrant·vllm cache 합 200GB 이상 |

## 기본 시동 (GPU 없는 dev/staging)

```bash
cd domainrag-ops
docker compose up -d
```

기본 profile은 postgres + qdrant + minio + backend(`RAG_BACKEND=inmemory`) + frontend.

`RAG_BACKEND=inmemory` 모드는 vLLM/TEI 없이도 chat 데모 동작. backend lifespan이 외부 의존(DB/Qdrant/MinIO) 확인을 skip한다.

## GPU 프로필 (운영 + 통합 테스트)

```bash
docker compose --profile gpu up -d
```

추가 서비스: `vllm` + `tei-embedder` + `tei-reranker`. GPU 2장 이상 + 모델 다운로드 캐시 필요.

backend env를 `RAG_BACKEND=production`로 override 하려면:

```bash
RAG_BACKEND=production docker compose --profile gpu up -d
```

## 의존 그래프 (compose가 자동 healthcheck로 대기)

```
postgres ─┐
qdrant   ─┤── backend ── frontend
minio    ─┘     ↓
              gpu profile:
              [vllm + tei-embedder + tei-reranker]
```

각 healthcheck는 30s interval. 첫 부트스트랩은 vLLM 모델 로딩 때문에 backend가 약 3-5분간 not-ready 상태일 수 있다.

## Alembic 마이그레이션 시점

ADR-021 §4 — **backend 컨테이너 entrypoint가 `alembic upgrade head`를 실행**한다. 별도 init job 없음.

만약 분리하고 싶다면 `compose.override.yml`로 별도 `migrator` service를 추가하고 backend `command:`에서 alembic 단계를 빼면 된다.

## 첫 검증

```bash
# 1. 모든 서비스 healthy
docker compose ps

# 2. liveness probe
curl http://localhost:8001/api/health/live

# 3. readiness probe (DB·migration 확인)
curl http://localhost:8001/api/health/ready

# 4. 모킹 채팅 (inmemory mode)
curl -X POST http://localhost:8001/api/security/chat \
  -H "Authorization: Bearer mock-token" \
  -H "Content-Type: application/json" \
  -d '{"question":"보안 정책 알려줘"}'
```

## 시동 실패 진단

| 증상 | 원인 | 대응 |
|---|---|---|
| backend가 `preload.failed`로 종료 | `RAG_BACKEND=production`이나 PostgreSQL 미가용 | `docker compose logs postgres` 확인 |
| postgres가 `psql: FATAL: role "domainrag_app" does not exist` | init_postgres.sql 미실행 | `docker volume rm domainrag-ops_postgres_data && docker compose up -d postgres` (init 재실행) |
| backend가 alembic 충돌 | migration 일부만 적용 | `docker compose exec backend alembic stamp head` 후 재시도 |
| vllm container Healthy 안 됨 | 모델 다운로드 중 또는 GPU 부족 | `docker compose logs vllm` |
| frontend가 backend 응답 없음 | CORS 또는 NEXT_PUBLIC_API_URL 불일치 | `docker compose exec frontend env | grep NEXT_PUBLIC` |

## 종료·정리

```bash
# 단순 stop (데이터 보존)
docker compose down

# 데이터 포함 삭제 (개발 reset)
docker compose down -v
```

`-v`는 postgres/minio/qdrant volume까지 삭제 — 운영 환경에서는 절대 사용 금지.

## Related

- [ADR-019 §3·§4](../adr/019-infrastructure-sharing.md) — GPU 매핑·vLLM 인스턴스 운영
- [ADR-021 §4](../adr/021-operational-bootstrap.md) — 의존 그래프·alembic 시점
- [chat-logs-partitioning.md](./chat-logs-partitioning.md) — 월별 cron
- [archival-cron.md](./archival-cron.md) — chunks 90일 archival
- [listen-notify-troubleshooting.md](./listen-notify-troubleshooting.md) — multi-instance 동기화
