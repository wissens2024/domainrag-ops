"""TEIReranker — score 부착·정렬·top_k 절단."""

from __future__ import annotations

import json

import httpx
import pytest

from rag_core.clients.tei_reranker import TEIReranker
from rag_core.interfaces.retriever import RetrievedChunk


def _chunk(cid: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        doc_id="d",
        title="t",
        content=content,
        page_number=None,
        section_title=None,
        dense_score=0.0,
        sparse_score=0.0,
        fused_score=0.0,
        payload={},
    )


@pytest.mark.asyncio
async def test_rerank_attaches_score_and_sorts(mock_async_client):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured["body"] = body
        return httpx.Response(
            200,
            json=[
                {"index": 0, "score": 0.1},
                {"index": 1, "score": 0.9},
                {"index": 2, "score": 0.5},
            ],
        )

    async with mock_async_client(handler) as client:
        reranker = TEIReranker(base_url="http://rr", client=client)
        candidates = [_chunk("a", "x"), _chunk("b", "y"), _chunk("c", "z")]
        out = await reranker.rerank("question", candidates, top_k=10)

    assert [c.chunk_id for c in out] == ["b", "c", "a"]
    assert out[0].rerank_score == 0.9
    assert out[1].rerank_score == 0.5
    assert out[2].rerank_score == 0.1
    assert captured["body"]["query"] == "question"
    assert captured["body"]["texts"] == ["x", "y", "z"]


@pytest.mark.asyncio
async def test_rerank_truncates_to_top_k(mock_async_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"index": 0, "score": 0.5},
                {"index": 1, "score": 0.4},
                {"index": 2, "score": 0.3},
            ],
        )

    async with mock_async_client(handler) as client:
        reranker = TEIReranker(base_url="http://rr", client=client)
        candidates = [_chunk("a", "x"), _chunk("b", "y"), _chunk("c", "z")]
        out = await reranker.rerank("q", candidates, top_k=2)

    assert [c.chunk_id for c in out] == ["a", "b"]


@pytest.mark.asyncio
async def test_rerank_empty_candidates(mock_async_client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not call TEI when no candidates")

    async with mock_async_client(handler) as client:
        reranker = TEIReranker(base_url="http://rr", client=client)
        assert await reranker.rerank("q", [], top_k=5) == []
