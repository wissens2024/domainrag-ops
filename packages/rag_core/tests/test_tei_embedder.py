"""TEIBgeM3Embedder — dense+sparse 병렬 호출 + 응답 정규화."""

from __future__ import annotations

import json

import httpx
import pytest

from rag_core.clients.tei_embedder import TEIBgeM3Embedder


def _make_handler(dense_resp: list, sparse_resp: list, *, captured: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if captured is not None:
            captured.setdefault("paths", []).append(request.url.path)
            captured.setdefault("inputs", []).append(body.get("inputs"))
        if request.url.path == "/embed":
            return httpx.Response(200, json=dense_resp)
        if request.url.path == "/embed_sparse":
            return httpx.Response(200, json=sparse_resp)
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_embed_batch_zips_dense_and_sparse(mock_async_client):
    captured: dict = {}
    handler = _make_handler(
        dense_resp=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
        sparse_resp=[
            [{"index": 10, "value": 0.7}, {"index": 20, "value": 0.3}],
            [{"index": 99, "value": 1.0}],
        ],
        captured=captured,
    )
    async with mock_async_client(handler) as client:
        emb = TEIBgeM3Embedder(
            base_url="http://embedder", dense_dim=3, client=client
        )
        out = await emb.embed_batch(["foo", "bar"])

    assert out[0][0] == [0.1, 0.2, 0.3]
    assert out[0][1] == {10: 0.7, 20: 0.3}
    assert out[1][0] == [0.4, 0.5, 0.6]
    assert out[1][1] == {99: 1.0}
    # dense + sparse 두 path 모두 호출
    assert sorted(captured["paths"]) == ["/embed", "/embed_sparse"]


@pytest.mark.asyncio
async def test_embed_query_calls_batch(mock_async_client):
    handler = _make_handler(
        dense_resp=[[1.0, 0.0]],
        sparse_resp=[[{"index": 5, "value": 0.5}]],
    )
    async with mock_async_client(handler) as client:
        emb = TEIBgeM3Embedder(base_url="http://embedder", dense_dim=2, client=client)
        dense, sparse = await emb.embed_query("hi")
    assert dense == [1.0, 0.0]
    assert sparse == {5: 0.5}


@pytest.mark.asyncio
async def test_embed_batch_empty_returns_empty(mock_async_client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called for empty input")

    async with mock_async_client(handler) as client:
        emb = TEIBgeM3Embedder(base_url="http://embedder", client=client)
        assert await emb.embed_batch([]) == []


@pytest.mark.asyncio
async def test_embed_batch_length_mismatch_raises(mock_async_client):
    handler = _make_handler(
        dense_resp=[[0.1]],  # 1개
        sparse_resp=[[], []],  # 2개 — mismatch
    )
    async with mock_async_client(handler) as client:
        emb = TEIBgeM3Embedder(base_url="http://embedder", dense_dim=1, client=client)
        with pytest.raises(RuntimeError, match="returned 1 for 2 inputs"):
            await emb.embed_batch(["a", "b"])


@pytest.mark.asyncio
async def test_parses_indices_values_dict_form(mock_async_client):
    """일부 TEI 빌드는 sparse를 {"indices":[...],"values":[...]} 형식으로 반환."""
    handler = _make_handler(
        dense_resp=[[0.0]],
        sparse_resp=[{"indices": [1, 2], "values": [0.4, 0.6]}],
    )
    async with mock_async_client(handler) as client:
        emb = TEIBgeM3Embedder(base_url="http://embedder", dense_dim=1, client=client)
        _, sparse = await emb.embed_query("x")
    assert sparse == {1: 0.4, 2: 0.6}


def test_properties():
    emb = TEIBgeM3Embedder(base_url="http://embedder", model_name="bge-m3", dense_dim=1024)
    assert emb.model_name == "bge-m3"
    assert emb.dense_dim == 1024
