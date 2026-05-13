"""Embedder Protocol — bge-m3 dense+sparse (ADR-011)."""

from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    """bge-m3는 dense+sparse를 한 호출로 출력 (ADR-011 §1).

    구현체:
      - BgeM3Embedder (TEI server 또는 자체)
      - MockEmbedder
    """

    async def embed_batch(
        self, texts: list[str]
    ) -> list[tuple[list[float], dict[int, float]]]:
        """반환: 각 텍스트마다 (dense_vector, sparse_vector_dict).

        sparse_vector_dict: {token_id: weight}
        """
        ...

    async def embed_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        """단일 query 임베딩. embed_batch와 분리는 latency 최적화 여지."""
        ...

    @property
    def model_name(self) -> str:
        """ADR-007/012 chunks.embedding_model 추적용."""
        ...

    @property
    def dense_dim(self) -> int: ...
