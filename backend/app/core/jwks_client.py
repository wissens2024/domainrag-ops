"""JWKSClient — AuthFusion JWKS 조회·캐시·자동 refresh (ADR-018 §3).

- in-memory cache (TTL=1h 기본, configs/platform/auth.yaml.jwks_cache_ttl_seconds)
- kid 미스 시 강제 refetch (key rotation 대응)
- async lock으로 동시 fetch 1회로 합치기
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx


class JWKSFetchError(RuntimeError):
    """JWKS endpoint 도달 실패 또는 응답 부적절."""


class JWKSClient:
    """JWKS endpoint에서 JWK set을 가져와 kid → JWK dict 매핑을 캐시."""

    def __init__(
        self,
        jwks_uri: str,
        cache_ttl_seconds: int = 3600,
        http_timeout: float = 5.0,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.jwks_uri = jwks_uri
        self.cache_ttl = float(cache_ttl_seconds)
        self.http_timeout = http_timeout
        self._client = http_client
        self._owned_client = http_client is None

        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_key(self, kid: str, *, force_refresh: bool = False) -> dict[str, Any]:
        """kid에 해당하는 JWK 반환. 캐시 미스 또는 force_refresh 시 refetch."""
        if not force_refresh:
            cached = self._keys.get(kid)
            if cached is not None and not self._is_stale():
                return cached

        async with self._lock:
            # 다른 coroutine이 이미 갱신했으면 재조회
            if not force_refresh:
                cached = self._keys.get(kid)
                if cached is not None and not self._is_stale():
                    return cached
            await self._refresh_locked()

        cached = self._keys.get(kid)
        if cached is not None:
            return cached

        # kid 자체가 없으면 키 rotation 가능성 — 한 번 더 강제 refresh
        async with self._lock:
            await self._refresh_locked(force=True)
        cached = self._keys.get(kid)
        if cached is None:
            raise JWKSFetchError(f"unknown kid in JWKS: {kid}")
        return cached

    def _is_stale(self) -> bool:
        return (time.time() - self._fetched_at) > self.cache_ttl

    async def _refresh_locked(self, force: bool = False) -> None:
        if not force and not self._is_stale() and self._keys:
            return
        client = self._client or httpx.AsyncClient(timeout=self.http_timeout)
        try:
            resp = await client.get(self.jwks_uri)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise JWKSFetchError(f"JWKS fetch failed: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        keys = data.get("keys") or []
        new_map: dict[str, dict[str, Any]] = {}
        for key in keys:
            kid = key.get("kid")
            if kid:
                new_map[kid] = key
        if not new_map:
            raise JWKSFetchError("JWKS response empty or missing kid fields")
        self._keys = new_map
        self._fetched_at = time.time()
