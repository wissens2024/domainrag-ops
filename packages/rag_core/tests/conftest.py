"""rag_core tests — 공통 fixture.

httpx.MockTransport로 외부 의존(VllmLLMClient·TEI*)을 in-process로 차단.
qdrant 테스트는 별도 conftest에서 unittest.mock.AsyncMock 사용.
"""

from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest


@pytest.fixture
def mock_async_client() -> Callable[[Callable[[httpx.Request], httpx.Response]], httpx.AsyncClient]:
    """factory: handler(request)->Response를 받아 AsyncClient 생성."""

    def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        return httpx.AsyncClient(transport=transport)

    return _factory


@pytest.fixture
def json_response() -> Callable[..., httpx.Response]:
    """body를 JSON으로 직렬화하는 헬퍼."""

    def _response(body: dict | list, status: int = 200) -> httpx.Response:
        return httpx.Response(
            status_code=status,
            content=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    return _response
