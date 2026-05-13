"""JWKSClient — 캐시·refresh·kid 미스 자동 refetch."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.core.jwks_client import JWKSClient, JWKSFetchError


class _FakeJWKSServer:
    """ASGI-style transport — keys/responses를 세트로 노출."""

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = responses
        self.call_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        idx = min(self.call_count, len(self.responses) - 1)
        body = self.responses[idx]
        self.call_count += 1
        return httpx.Response(200, json=body)


def _make_client(server: _FakeJWKSServer, ttl: int = 3600) -> JWKSClient:
    transport = httpx.MockTransport(server)
    httpx_client = httpx.AsyncClient(transport=transport)
    return JWKSClient(
        jwks_uri="https://sso.example/.well-known/jwks.json",
        cache_ttl_seconds=ttl,
        http_client=httpx_client,
    )


def _jwks(*kids: str) -> dict:
    return {"keys": [{"kid": kid, "kty": "RSA", "n": "x", "e": "AQAB"} for kid in kids]}


@pytest.mark.asyncio
async def test_first_get_fetches_and_caches():
    server = _FakeJWKSServer([_jwks("kid-1", "kid-2")])
    client = _make_client(server)
    try:
        k = await client.get_key("kid-1")
        assert k["kid"] == "kid-1"
        # 같은 kid 다시 → 캐시 적중, fetch 횟수 그대로
        await client.get_key("kid-1")
        await client.get_key("kid-2")
        assert server.call_count == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unknown_kid_triggers_force_refresh():
    """첫 응답에는 kid-1만, 두 번째 응답엔 kid-2 추가."""
    server = _FakeJWKSServer([_jwks("kid-1"), _jwks("kid-1", "kid-2")])
    client = _make_client(server)
    try:
        await client.get_key("kid-1")
        # kid-2 미스 → 강제 refresh → 두 번째 응답에 있으므로 성공
        k = await client.get_key("kid-2")
        assert k["kid"] == "kid-2"
        assert server.call_count >= 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unknown_kid_after_refresh_raises():
    server = _FakeJWKSServer([_jwks("kid-1")])
    client = _make_client(server)
    try:
        with pytest.raises(JWKSFetchError):
            await client.get_key("kid-nonexistent")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_get_collapses_to_single_fetch():
    server = _FakeJWKSServer([_jwks("kid-1", "kid-2")])
    client = _make_client(server)
    try:
        results = await asyncio.gather(
            client.get_key("kid-1"),
            client.get_key("kid-1"),
            client.get_key("kid-2"),
        )
        assert all(r["kid"] in {"kid-1", "kid-2"} for r in results)
        # asyncio.Lock 보호로 1회만 fetch
        assert server.call_count == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_error_raises_jwksfetcherror():
    def _bad(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(_bad)
    client = JWKSClient(
        jwks_uri="https://sso.example/.well-known/jwks.json",
        cache_ttl_seconds=3600,
        http_client=httpx.AsyncClient(transport=transport),
    )
    try:
        with pytest.raises(JWKSFetchError):
            await client.get_key("kid-1")
    finally:
        await client.aclose()
