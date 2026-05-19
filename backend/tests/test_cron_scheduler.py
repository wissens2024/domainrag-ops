"""CronScheduler — lifespan scheduler 검증 (ADR-021 §3)."""

from __future__ import annotations

import asyncio

import pytest

from app.services.cron_scheduler import CronScheduler


@pytest.mark.asyncio
async def test_scheduler_runs_jobs_periodically():
    counter = {"n": 0}

    async def job():
        counter["n"] += 1

    scheduler = CronScheduler()
    scheduler.register(name="counter", interval_seconds=0.05, run=job)
    scheduler.start()
    await asyncio.sleep(0.18)  # 약 3-4회 실행 기회
    await scheduler.stop()
    # 3회 이상 실행됐어야 함 (정확한 횟수는 OS scheduling에 따라 변동)
    assert counter["n"] >= 3
    stats = scheduler.stats()
    assert stats[0]["runs_completed"] >= 3


@pytest.mark.asyncio
async def test_scheduler_continues_after_failure():
    calls = {"ok": 0, "fail": 0}

    async def flaky():
        if calls["fail"] < 1:
            calls["fail"] += 1
            raise RuntimeError("transient")
        calls["ok"] += 1

    scheduler = CronScheduler()
    scheduler.register(name="flaky", interval_seconds=0.02, run=flaky)
    scheduler.start()
    await asyncio.sleep(0.10)
    await scheduler.stop()
    assert calls["fail"] == 1
    assert calls["ok"] >= 1
    stats = scheduler.stats()[0]
    assert stats["runs_failed"] == 1


@pytest.mark.asyncio
async def test_scheduler_register_duplicate_raises():
    async def noop():
        pass

    scheduler = CronScheduler()
    scheduler.register(name="a", interval_seconds=1.0, run=noop)
    with pytest.raises(ValueError):
        scheduler.register(name="a", interval_seconds=1.0, run=noop)


@pytest.mark.asyncio
async def test_scheduler_initial_delay():
    calls = {"n": 0}

    async def job():
        calls["n"] += 1

    scheduler = CronScheduler()
    scheduler.register(
        name="delayed", interval_seconds=0.05, run=job,
        initial_delay_seconds=0.10,
    )
    scheduler.start()
    await asyncio.sleep(0.05)  # delay 안 끝남
    assert calls["n"] == 0
    await asyncio.sleep(0.10)  # 이제 첫 실행 후
    assert calls["n"] >= 1
    await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_stop_cancels_running_job():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_running():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    scheduler = CronScheduler()
    scheduler.register(name="long", interval_seconds=0.01, run=long_running)
    scheduler.start()
    await started.wait()
    await scheduler.stop()
    assert cancelled.is_set()
