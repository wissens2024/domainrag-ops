"""TEIBgeM3Embedder — HuggingFace TEI(Text Embeddings Inference) 기반 bge-m3.

ADR-011 정합:
  - dense (bge-m3 1024) + sparse (token_id → weight)를 한 호출씩.
  - TEI bge-m3 endpoint:
      POST /embed         → list[list[float]] (dense)
      POST /embed_sparse  → list[list[{"index": int, "value": float}]]
  - dense / sparse 두 호출은 병렬 실행 (asyncio.gather)
"""

from __future__ import annotations

import asyncio

import httpx


class TEIBgeM3Embedder:
    """bge-m3 dense+sparse 동시 산출 (TEI 기반).

    Args:
        base_url:   예) "http://embedder:8080" (TEI HTTP)
        model_name: chunks.embedding_model 추적용 (ADR-007/012)
        dense_dim:  bge-m3 기본 1024
        timeout_seconds: 호출 timeout
        client: 외부 httpx.AsyncClient 주입 (테스트)
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str = "bge-m3",
        dense_dim: int = 1024,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._dense_dim = dense_dim
        self._timeout = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dense_dim(self) -> int:
        return self._dense_dim

    async def _post(self, path: str, payload: dict) -> list:
        resp = await self._client.post(
            f"{self._base_url}{path}",
            json=payload,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_sparse(items: list) -> dict[int, float]:
        """TEI sparse 응답을 dict[int, float]로 정규화.

        TEI는 보통 [{"index": int, "value": float}, ...] 또는
        {"indices": [...], "values": [...]} 형태로 반환. 두 형식 모두 수용.
        """
        if isinstance(items, dict) and "indices" in items and "values" in items:
            return {int(i): float(v) for i, v in zip(items["indices"], items["values"])}
        out: dict[int, float] = {}
        for it in items or []:
            if isinstance(it, dict):
                idx = it.get("index")
                if idx is None:
                    idx = it.get("token_id")
                val = it.get("value")
                if val is None:
                    val = it.get("weight")
                if idx is not None and val is not None:
                    out[int(idx)] = float(val)
        return out

    async def embed_batch(
        self, texts: list[str]
    ) -> list[tuple[list[float], dict[int, float]]]:
        if not texts:
            return []
        payload = {"inputs": texts}
        dense_task = self._post("/embed", payload)
        sparse_task = self._post("/embed_sparse", payload)
        dense_resp, sparse_resp = await asyncio.gather(dense_task, sparse_task)

        if len(dense_resp) != len(texts):
            raise RuntimeError(
                f"TEI /embed returned {len(dense_resp)} for {len(texts)} inputs"
            )
        if len(sparse_resp) != len(texts):
            raise RuntimeError(
                f"TEI /embed_sparse returned {len(sparse_resp)} for {len(texts)} inputs"
            )

        out: list[tuple[list[float], dict[int, float]]] = []
        for d, s in zip(dense_resp, sparse_resp):
            out.append(([float(x) for x in d], self._parse_sparse(s)))
        return out

    async def embed_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        result = await self.embed_batch([text])
        return result[0]
