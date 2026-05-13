"""TEIReranker — HF TEI rerank endpoint(`/rerank`) 기반 cross-encoder.

ADR-011 §4·§10 정합:
  - 후보 list[RetrievedChunk]에 대해 (question, candidate.content) 쌍의
    relevance score를 계산하여 rerank_score 갱신 + 정렬 후 top_k 반환.
  - TEI 응답: [{"index": int, "score": float}, ...]
"""

from __future__ import annotations

import httpx

from ..interfaces.retriever import RetrievedChunk


class TEIReranker:
    """TEI cross-encoder reranker.

    Args:
        base_url: 예) "http://reranker:8080"
        model_name: 추적·로그용 식별자 (default: bge-reranker-v2-m3)
        timeout_seconds: 호출 timeout
        client: 외부 httpx.AsyncClient 주입 (테스트)
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str = "bge-reranker-v2-m3",
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._timeout = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def model_name(self) -> str:
        return self._model_name

    async def rerank(
        self,
        question: str,
        candidates: list[RetrievedChunk],
        top_k: int = 10,
    ) -> list[RetrievedChunk]:
        if not candidates:
            return []
        texts = [c.content for c in candidates]
        payload = {
            "query": question,
            "texts": texts,
            "raw_scores": False,
            "return_text": False,
        }
        resp = await self._client.post(
            f"{self._base_url}/rerank",
            json=payload,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # data: [{"index": int, "score": float}, ...] — TEI는 score 내림차순으로 반환하지만
        # 안전하게 명시 정렬한다.
        for item in data:
            idx = int(item.get("index", -1))
            score = float(item.get("score", 0.0))
            if 0 <= idx < len(candidates):
                candidates[idx].rerank_score = score
        ranked = sorted(
            candidates,
            key=lambda c: (c.rerank_score if c.rerank_score is not None else float("-inf")),
            reverse=True,
        )
        return ranked[:top_k]
