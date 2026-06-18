"""TEIBgeM3Embedder — HuggingFace TEI(Text Embeddings Inference) 기반 bge-m3.

ADR-011 정합:
  - dense (bge-m3 1024) + sparse (token_id → weight)를 한 호출씩.
  - TEI bge-m3 endpoint:
      POST /embed         → list[list[float]] (dense)
      POST /embed_sparse  → list[list[{"index": int, "value": float}]]
  - dense / sparse 두 호출은 병렬 실행 (asyncio.gather)
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


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
        max_client_batch: int = 32,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._dense_dim = dense_dim
        self._timeout = timeout_seconds
        # TEI는 한 요청당 입력 수를 --max-client-batch-size(기본 32)로 제한한다.
        # 초과 시 413을 반환하므로 embed_batch가 이 크기로 청킹한다.
        self._max_client_batch = max(1, max_client_batch)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        # ADR-011 hybrid은 sparse가 있으면 DBSF fusion, 없으면 dense-only로 degrade한다.
        # TEI 임베더가 /embed_sparse(SPLADE)를 지원 안 하면 첫 실패 후 비활성화하여
        # dense-only로 동작 (매 호출 sparse 에러 반복 방지).
        self._sparse_enabled = True

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
        # TEI max-client-batch-size(기본 32) 초과 시 413 → 청크 단위로 분할 호출.
        out: list[tuple[list[float], dict[int, float]]] = []
        for start in range(0, len(texts), self._max_client_batch):
            out.extend(await self._embed_one_batch(texts[start : start + self._max_client_batch]))
        return out

    async def _embed_one_batch(
        self, texts: list[str]
    ) -> list[tuple[list[float], dict[int, float]]]:
        payload = {"inputs": texts}
        # dense는 필수. sparse는 best-effort — 임베더가 미지원이면 dense-only로 degrade.
        dense_resp = await self._post("/embed", payload)
        if len(dense_resp) != len(texts):
            raise RuntimeError(
                f"TEI /embed returned {len(dense_resp)} for {len(texts)} inputs"
            )
        sparse_resp = await self._maybe_embed_sparse(payload, len(texts))

        out: list[tuple[list[float], dict[int, float]]] = []
        for i, d in enumerate(dense_resp):
            s = self._parse_sparse(sparse_resp[i]) if sparse_resp is not None else {}
            out.append(([float(x) for x in d], s))
        return out

    async def _maybe_embed_sparse(
        self, payload: dict, expected: int
    ) -> list | None:
        """sparse 임베딩을 best-effort로 가져온다. 미지원/실패 시 None(→dense-only).

        TEI가 SPLADE pooling 미지원이면 /embed_sparse가 에러를 반환한다. 이때 한 번
        경고 후 sparse를 비활성화하고 dense-only로 degrade한다(ADR-011 §3 hybrid은
        sparse 복구 시 자동 재개). 검색 자체가 깨지는 것보다 dense-only가 낫다.
        """
        if not self._sparse_enabled:
            return None
        try:
            sparse_resp = await self._post("/embed_sparse", payload)
        except Exception as exc:  # noqa: BLE001
            self._sparse_enabled = False
            logger.warning(
                "TEI /embed_sparse unavailable (%s) — dense-only retrieval로 degrade",
                exc,
            )
            return None
        if len(sparse_resp) != expected:
            self._sparse_enabled = False
            logger.warning(
                "TEI /embed_sparse returned %d for %d inputs — dense-only degrade",
                len(sparse_resp), expected,
            )
            return None
        return sparse_resp

    async def embed_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        result = await self.embed_batch([text])
        return result[0]
