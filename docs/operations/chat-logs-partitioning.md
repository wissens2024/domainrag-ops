# chat_logs 월별 partition 운영

ADR-019 §2 — `chat_logs`는 `PARTITION BY RANGE (created_at)` 월 단위. 매월 1일 자정에
다음 달 partition을 미리 생성하지 않으면 새 달 첫 INSERT가 실패한다 (`no partition of
relation "chat_logs" found for row`).

운영 의무: 본 문서의 cron이 매월 1일 00:30 KST 시점에 실행되도록 등록한다.

## 실행 방식

### 옵션 A — cron (단일 host)

```cron
# /etc/cron.d/domainrag-chat-logs-partition
SHELL=/bin/bash
30 0 1 * * domainrag cd /opt/domainrag/backend && \
    /opt/domainrag/.venv/bin/python -m app.services.chat_logs_partition_service \
    >> /var/log/domainrag/partition.log 2>&1
```

### 옵션 B — Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: domainrag-chat-logs-partition
spec:
  schedule: "30 15 1 * *"   # UTC 15:30 = KST 00:30 (1일)
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: partition
              image: domainrag/backend:<version>
              command:
                - python
                - -m
                - app.services.chat_logs_partition_service
              env:
                - { name: POSTGRES_HOST, value: pg-115.internal }
                - { name: POSTGRES_ADMIN_USER, value: domainrag_platform_admin }
                - { name: POSTGRES_ADMIN_PASSWORD, valueFrom: { secretKeyRef: { name: domainrag-db, key: admin-password } } }
```

### 옵션 C — systemd timer

```ini
# /etc/systemd/system/domainrag-partition.service
[Service]
Type=oneshot
User=domainrag
WorkingDirectory=/opt/domainrag/backend
ExecStart=/opt/domainrag/.venv/bin/python -m app.services.chat_logs_partition_service

# /etc/systemd/system/domainrag-partition.timer
[Timer]
OnCalendar=*-*-01 00:30:00 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
```

## 동작

`python -m app.services.chat_logs_partition_service` 1회 실행 시:

1. `pg_try_advisory_lock(70190612)` 획득 — 같은 lock key는 multiple instance(K8s CronJob 동시 트리거 등)를 서로 차단.
2. **현재달** partition도 함께 보장 (운영 처음 setup 시 누락 대비).
3. **다음달** partition을 idempotent CREATE.
4. `pg_advisory_unlock` 후 종료.

다른 instance가 lock을 잡고 있으면 본 instance는 `skipped_reason="advisory_lock_busy"`로
조용히 종료. Redis나 분산 lock 인프라 불필요.

## 결과 확인

```sql
SELECT child.relname, pg_get_expr(child.relpartbound, child.oid) AS bounds
  FROM pg_inherits i
  JOIN pg_class parent ON i.inhparent = parent.oid
  JOIN pg_class child  ON i.inhrelid  = child.oid
 WHERE parent.relname = 'chat_logs'
 ORDER BY child.relname;
```

예상:
```
chat_logs_y2026m05 | FOR VALUES FROM ('2026-05-01') TO ('2026-06-01')
chat_logs_y2026m06 | FOR VALUES FROM ('2026-06-01') TO ('2026-07-01')
chat_logs_y2026m07 | FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')   ← cron 실행 후
```

## 장애 시 복구

cron 누락으로 새 달 INSERT가 실패하면:

```bash
# 즉시 생성 (멱등)
python -m app.services.chat_logs_partition_service
```

또는 특정 month를 backfill하려면 Python REPL:

```python
import asyncio
from app.core.db import AdminSessionLocal
from app.services.chat_logs_partition_service import ChatLogsPartitionService

asyncio.run(
    ChatLogsPartitionService(admin_session_factory=AdminSessionLocal)
        .ensure_partition(year=2026, month=7)
)
```

## 권한

- 사용 DB role: `domainrag_platform_admin` (BYPASSRLS, CREATE TABLE 권한)
- AppSessionLocal(`domainrag_app`)은 CREATE 권한 없음 — 본 worker는 admin engine 전용
- ADR-019 §1 PostgreSQL role 분리와 정합

## 모니터링

- log: `chat_logs partition ensured: this=…(created=…), next=…(created=…)`
- 실패 시: stderr + 다음 로그 등급은 ERROR (cron이 알람 시스템에 연계되면 자동 통보)
- 검증 query (위 결과 확인) 매월 첫째 주 운영 점검 항목에 포함
