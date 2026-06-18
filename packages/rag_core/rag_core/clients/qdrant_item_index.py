"""AssessmentItemIndex — Qdrant items_{domain} 사전 인덱싱 (ADR-025 §5).

assessment item의 question_text를 dense 임베딩해 collection-per-domain으로 인덱싱한다.
item 수가 커지면 generate의 reference·dedup을 on-the-fly 임베딩(후보 전체 재임베딩)에서
이 사전 인덱스 검색으로 전환한다(ADR-025 §5 검색 전략 전환). near-dup(의역) 탐지도
이 인덱스로 수행한다.

chunks 인덱스(QdrantVectorStore, dense+sparse hybrid)와 분리한다 — items는 question_text
유사도만 필요하므로 dense-only로 단순화한다.
"""

from __future__ import annotations

import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from ..interfaces.embedder import Embedder

# item_id(문자열) → Qdrant point id(UUID) 결정적 변환용 고정 네임스페이스.
_ITEM_NS = uuid.UUID("6f2a7c1e-3b5d-4e8a-9c0f-1a2b3c4d5e6f")


def _collection(domain_id: str) -> str:
    return f"items_{domain_id}"


def _point_id(item_id: str) -> str:
    return str(uuid.uuid5(_ITEM_NS, item_id))


class AssessmentItemIndex:
    def __init__(
        self,
        *,
        client: AsyncQdrantClient,
        embedder: Embedder,
        distance: models.Distance = models.Distance.COSINE,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._distance = distance

    async def ensure_collection(self, *, domain_id: str, dense_dim: int = 1024) -> None:
        """없으면 생성(idempotent). 이미 있으면 그대로 둔다."""
        if await self._client.collection_exists(_collection(domain_id)):
            return
        await self._client.create_collection(
            collection_name=_collection(domain_id),
            vectors_config={
                "dense": models.VectorParams(size=dense_dim, distance=self._distance)
            },
        )

    async def index_items(
        self, *, domain_id: str, items: list[tuple[str, str, dict[str, Any]]]
    ) -> int:
        """items = [(item_id, question_text, payload), ...] 임베딩 후 upsert. 인덱싱 수 반환.

        같은 item_id는 결정적 point id로 매핑되므로 재인덱싱이 덮어쓴다(중복 누적 없음).
        """
        rows = [(iid, q, pl) for iid, q, pl in items if (q or "").strip()]
        if not rows:
            return 0
        embeddings = await self._embedder.embed_batch([q for _, q, _ in rows])
        points: list[models.PointStruct] = []
        for (iid, _q, payload), emb in zip(rows, embeddings):
            dense = emb[0] if isinstance(emb, tuple) else emb
            points.append(
                models.PointStruct(
                    id=_point_id(iid),
                    vector={"dense": list(dense)},
                    payload={"item_id": iid, **(payload or {})},
                )
            )
        await self._client.upsert(
            collection_name=_collection(domain_id), points=points, wait=True
        )
        return len(points)

    async def search_similar(
        self,
        *,
        domain_id: str,
        question_text: str,
        subject: str | None = None,
        top_k: int = 10,
        exclude_item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """질문과 유사한 기존 item 검색. [{item_id, subject, score(cosine)}] (score 내림차순).

        collection이 아직 없으면(미인덱싱) 빈 리스트로 degrade — caller(generate)는 dedup
        없이 진행한다.
        """
        if not (question_text or "").strip():
            return []
        if not await self._client.collection_exists(_collection(domain_id)):
            return []
        emb = (await self._embedder.embed_batch([question_text]))[0]
        dense = emb[0] if isinstance(emb, tuple) else emb
        flt = None
        if subject:
            flt = models.Filter(
                must=[models.FieldCondition(key="subject", match=models.MatchValue(value=subject))]
            )
        res = await self._client.query_points(
            collection_name=_collection(domain_id),
            query=list(dense),
            using="dense",
            query_filter=flt,
            limit=top_k + (1 if exclude_item_id else 0),
            with_payload=True,
        )
        out: list[dict[str, Any]] = []
        for p in res.points:
            iid = (p.payload or {}).get("item_id")
            if exclude_item_id and iid == exclude_item_id:
                continue
            out.append({
                "item_id": iid,
                "subject": (p.payload or {}).get("subject"),
                "score": float(p.score),
            })
        return out[:top_k]

    async def delete_item(self, *, domain_id: str, item_id: str) -> None:
        await self._client.delete(
            collection_name=_collection(domain_id),
            points_selector=models.PointIdsList(points=[_point_id(item_id)]),
            wait=True,
        )
