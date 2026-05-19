"""CronScheduler — lifespan background task scheduler (ADR-021 §3).

운영 모드 두 가지:
  - OPS_CRON_MODE=internal (default): lifespan에서 asyncio Task로 주기 실행.
    PostgreSQL advisory lock으로 multi-instance 중복 실행 차단(각 service 내부에서).
  - OPS_CRON_MODE=external: lifespan에서 등록 skip — 운영자가 k8s CronJob /
    crontab으로 `python -m app.services.archival_worker` 등을 명시 호출.

본 scheduler는 *interval 기반*이다. 정밀 cron(`0 3 * * *` 같은)을 원하면 external 모드
권장 — 본 모듈은 운영 단순화·dev 데모에 적합.

각 job은 다음을 만족해야 한다:
  - idempotent (재시도해도 의미적 동일)
  - 짧음 (CronScheduler는 단일 instance가 다 처리하리라 가정 안 함)
  - 자체 advisory lock 으로 동시 실행 차단 (현재 partition·archival 모두 보유)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


JobFn = Callable[[], Awaitable[None]]


@dataclass
class Job:
    name: str
    interval_seconds: float
    run: JobFn
    # 시작 직후 첫 실행 지연(초) — 모든 job이 startup에 동시 폭주 방지
    initial_delay_seconds: float = 0.0
    # 실행 통계 (테스트·관제용)
    runs_completed: int = 0
    runs_failed: int = 0
    last_run_started_at: float | None = None
    last_run_finished_at: float | None = None
    last_error: str | None = None


class CronScheduler:
    """단일 process 내 background scheduler.

    Args:
        clock: time.monotonic 또는 asyncio.get_event_loop().time. 테스트 주입용.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    def register(
        self,
        *,
        name: str,
        interval_seconds: float,
        run: JobFn,
        initial_delay_seconds: float = 0.0,
    ) -> None:
        if name in self._jobs:
            raise ValueError(f"job already registered: {name}")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._jobs[name] = Job(
            name=name,
            interval_seconds=interval_seconds,
            run=run,
            initial_delay_seconds=max(0.0, initial_delay_seconds),
        )

    def start(self) -> None:
        """등록된 모든 job을 asyncio Task로 시작."""
        if self._tasks:
            return  # already started
        self._stopping = False
        for job in self._jobs.values():
            task = asyncio.create_task(
                self._run_job_loop(job),
                name=f"cron:{job.name}",
            )
            self._tasks.append(task)
        logger.info(
            "cron_scheduler.started jobs=%s",
            [j.name for j in self._jobs.values()],
        )

    async def stop(self) -> None:
        """모든 job task cancel + cleanup."""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    def stats(self) -> list[dict]:
        return [
            {
                "name": j.name,
                "interval_seconds": j.interval_seconds,
                "runs_completed": j.runs_completed,
                "runs_failed": j.runs_failed,
                "last_run_started_at": j.last_run_started_at,
                "last_run_finished_at": j.last_run_finished_at,
                "last_error": j.last_error,
            }
            for j in self._jobs.values()
        ]

    async def _run_job_loop(self, job: Job) -> None:
        if job.initial_delay_seconds > 0:
            try:
                await asyncio.sleep(job.initial_delay_seconds)
            except asyncio.CancelledError:
                return
        while not self._stopping:
            job.last_run_started_at = time.time()
            try:
                await job.run()
                job.runs_completed += 1
                job.last_error = None
                logger.debug("cron job ok: %s", job.name)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                job.runs_failed += 1
                job.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("cron job %s failed: %s", job.name, exc)
            job.last_run_finished_at = time.time()
            try:
                await asyncio.sleep(job.interval_seconds)
            except asyncio.CancelledError:
                return
