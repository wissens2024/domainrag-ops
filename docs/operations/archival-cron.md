# chunks Archival 운영 가이드 (ADR-012 §3 + ADR-021 §3)

## 개요

`chunks.valid_until < threshold AND archived_at IS NULL` 조건의 chunk를 매일 03:00 KST에 `chunks_archive` 테이블로 이동한다. tenant 별 `lifecycle.yaml.chunks_archive_days` 가 기본 90일.

## 운영 모드 (ADR-021 §3 결정)

### internal 모드 (OPS_CRON_MODE=internal, 기본)

backend lifespan이 `asyncio.create_task`로 ArchivalWorker를 등록한다.

```python
# lifespan에서 자동 등록 — 별도 설정 불필요
CronScheduler.register(
    name="chunks_archival",
    interval_seconds=86400,   # 24시간
    run=_archival_job,
    initial_delay_seconds=300,  # startup 후 5분
)
```

장점: 외부 cron 없이 동작. 단점: backend replica가 여러 개라도 advisory lock으로 단 1개만 작업하지만, 정확한 cron 시각이 아닌 process 시작 시간 + N\*24h 패턴.

### external 모드 (OPS_CRON_MODE=external)

`docker compose` 또는 k8s에서 별도 CronJob으로 실행:

```yaml
# k8s example
apiVersion: batch/v1
kind: CronJob
metadata:
  name: domainrag-archival
spec:
  schedule: "0 18 * * *"  # 03:00 KST = 18:00 UTC
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: archival
              image: domainrag-backend:latest
              command: ["python", "-m", "app.services.archival_worker"]
              envFrom:
                - secretRef:
                    name: domainrag-env
          restartPolicy: OnFailure
```

systemd timer 예시:

```ini
# /etc/systemd/system/domainrag-archival.service
[Unit]
Description=DomainRAG chunks archival

[Service]
Type=oneshot
WorkingDirectory=/opt/domainrag/backend
ExecStart=/usr/bin/python3 -m app.services.archival_worker
EnvironmentFile=/etc/domainrag/.env

# /etc/systemd/system/domainrag-archival.timer
[Unit]
Description=Daily chunks archival 03:00 KST

[Timer]
OnCalendar=*-*-* 18:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

활성:
```bash
systemctl enable --now domainrag-archival.timer
```

## Advisory Lock

archival worker는 `pg_try_advisory_lock(70190301)`로 멀티 인스턴스 동시 실행을 차단. 락 미획득 시 silent skip(다른 인스턴스가 처리 중).

## 모니터링

- `tenant_lifecycle_logs` 에 `chunk_archival_executed` action으로 매 batch 기록
- `chunks_archive` 행 수 증가 추세
- `chunks.archived_at IS NOT NULL` 카운트
- `/api/platform/admin/health/metrics` (chat_log_writer 영역과는 별개로 archival 자체 메트릭은 미구현 — 운영 중요 시 추가 ADR)

## 장애 대응

| 증상 | 원인 | 조치 |
|---|---|---|
| archived_at 갱신 후 Qdrant `archived: true` payload 미동기 | vector_store.set_payload 실패 (swallow됨) | tenant별 재실행: `python -m app.services.archival_worker --tenant <id> --force` |
| advisory lock 영구 보유 | 이전 worker process 강제 종료 | `SELECT pg_advisory_unlock(70190301)` 후 재실행 |
| chunks_archive 디스크 폭증 | retention 정책 미적용 | `lifecycle.yaml.archive_retention_days` 별도 결정 (ADR-012 후속) |

## Related

- [ADR-012 §3](../adr/012-lifecycle-v2.md) — archival 90일 정책
- [ADR-021 §3](../adr/021-operational-bootstrap.md) — cron 운영 모드
