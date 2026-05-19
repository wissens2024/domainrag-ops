"""PostgreSQL LISTEN/NOTIFY consumer — ADR-021 §2.

asyncpg connection으로 3개 channel(tenant_config_changed, tenant_schema_changed,
tenant_lifecycle_changed)을 구독한다. backend lifespan에서 background task로 실행
되며 shutdown 시 cancel.

연결 끊김(서버 재시작, 네트워크 단절)에는 5초 backoff 후 재연결 + 직후 단일 tenant
reload가 아닌 *full preload* 1회로 NOTIFY 누락 보전.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import asyncpg

logger = logging.getLogger(__name__)


# Handler signature: (payload_dict) -> awaitable
NotifyHandler = Callable[[dict[str, Any]], Awaitable[None]]


class PostgresNotifyListener:
    """asyncpg LISTEN consumer — 3개 channel 동시 구독.

    Public API:
      - start() — background task 생성. 이미 실행 중이면 무시.
      - stop() — task cancel + cleanup.

    `dsn`은 raw asyncpg DSN (e.g., ``postgresql://user:pw@host:5432/db``)이어야 한다.
    SQLAlchemy의 ``postgresql+asyncpg://`` 형식이라면 ``+asyncpg`` 부분을 제거해 전달.
    """

    def __init__(
        self,
        *,
        dsn: str,
        handlers: dict[str, NotifyHandler],
        on_reconnect: NotifyHandler | None = None,
        reconnect_backoff_seconds: float = 5.0,
    ) -> None:
        self._dsn = self._normalize_dsn(dsn)
        self._handlers = handlers
        self._on_reconnect = on_reconnect
        self._backoff = reconnect_backoff_seconds
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._connection: asyncpg.Connection | None = None

    @staticmethod
    def _normalize_dsn(dsn: str) -> str:
        return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="pg_notify_listener")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._connection is not None and not self._connection.is_closed():
            try:
                await self._connection.close()
            except Exception:  # noqa: BLE001
                pass
            self._connection = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pg_notify_listener disconnected: %s — retry in %ss",
                    exc,
                    self._backoff,
                )
                await asyncio.sleep(self._backoff)
                # 재연결 직후 NOTIFY 누락 보전 — full reload trigger
                if self._on_reconnect is not None and not self._stopping:
                    try:
                        await self._on_reconnect({"event": "reconnect"})
                    except Exception as reload_exc:  # noqa: BLE001
                        logger.warning("on_reconnect handler failed: %s", reload_exc)

    async def _connect_and_listen(self) -> None:
        self._connection = await asyncpg.connect(self._dsn)
        try:
            for channel, handler in self._handlers.items():
                await self._connection.add_listener(
                    channel,
                    self._make_listener_callback(channel, handler),
                )
            logger.info(
                "pg_notify_listener connected", channels=list(self._handlers.keys())
            )
            # 무한 대기 — connection이 살아있는 한 add_listener callback이 호출된다.
            while not self._stopping:
                # 연결 살아있는지 30초마다 ping. asyncpg는 socket close 시 첫 query에서 알림.
                await asyncio.sleep(30)
                try:
                    await asyncio.wait_for(
                        self._connection.fetchval("SELECT 1"), timeout=5
                    )
                except Exception:  # noqa: BLE001
                    raise ConnectionError("listener connection ping failed")
        finally:
            if self._connection is not None and not self._connection.is_closed():
                try:
                    await self._connection.close()
                except Exception:  # noqa: BLE001
                    pass
            self._connection = None

    def _make_listener_callback(
        self, channel: str, handler: NotifyHandler
    ) -> Callable[..., None]:
        def callback(connection, pid, channel_name, payload):  # noqa: ARG001
            try:
                data = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                logger.warning("pg_notify bad payload on %s: %r", channel, payload)
                return
            # asyncpg callback은 sync — handler는 task로 schedule
            asyncio.create_task(_invoke_safely(channel, handler, data))

        return callback


async def _invoke_safely(
    channel: str, handler: NotifyHandler, data: dict[str, Any]
) -> None:
    try:
        await handler(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pg_notify handler %s failed: %s", channel, exc)
