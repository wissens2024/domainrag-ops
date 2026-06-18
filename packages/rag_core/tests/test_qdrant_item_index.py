"""AssessmentItemIndex — Qdrant items_{domain} 인덱싱·검색 (ADR-025 §5).

qdrant-client :memory: 로컬 모드(실제 Qdrant 인-프로세스) + 결정적 fake embedder로
인덱싱·유사검색·필터·중복탐지를 검증한다.
"""

from __future__ import annotations

import pytest
from qdrant_client import AsyncQdrantClient

from rag_core.clients.qdrant_item_index import AssessmentItemIndex


class _FakeEmbedder:
    """텍스트 → 고정 dense 벡터(3차원). 미등록 텍스트는 영벡터에 가깝게."""

    def __init__(self, vectors: dict[str, list[float]]):
        self._v = vectors

    @property
    def dense_dim(self) -> int:
        return 3

    async def embed_batch(self, texts):
        return [(self._v.get(t, [0.0, 0.0, 0.01]), {}) for t in texts]


_VECS = {
    "이 트리의 루트는?": [1.0, 0.0, 0.0],
    "이 트리의 루트 노드는 무엇인가?": [0.97, 0.24, 0.0],   # 위와 유사
    "네트워크 프로토콜의 계층은?": [0.0, 0.0, 1.0],          # 다름
}


def _index():
    client = AsyncQdrantClient(location=":memory:")
    return AssessmentItemIndex(client=client, embedder=_FakeEmbedder(_VECS))


async def test_ensure_collection_idempotent():
    idx = _index()
    await idx.ensure_collection(domain_id="t1", dense_dim=3)
    await idx.ensure_collection(domain_id="t1", dense_dim=3)  # 두 번 호출해도 에러 없음


async def test_index_and_search_ranks_by_similarity():
    idx = _index()
    await idx.ensure_collection(domain_id="t1", dense_dim=3)
    n = await idx.index_items(domain_id="t1", items=[
        ("Q-TREE", "이 트리의 루트는?", {"subject": "programming"}),
        ("Q-NET", "네트워크 프로토콜의 계층은?", {"subject": "data_communication"}),
    ])
    assert n == 2
    hits = await idx.search_similar(
        domain_id="t1", question_text="이 트리의 루트 노드는 무엇인가?", top_k=2
    )
    # 트리 문항이 네트워크 문항보다 위
    assert hits[0]["item_id"] == "Q-TREE"
    assert hits[0]["score"] > hits[1]["score"]
    assert hits[0]["subject"] == "programming"


async def test_search_subject_filter():
    idx = _index()
    await idx.ensure_collection(domain_id="t1", dense_dim=3)
    await idx.index_items(domain_id="t1", items=[
        ("Q-TREE", "이 트리의 루트는?", {"subject": "programming"}),
        ("Q-NET", "네트워크 프로토콜의 계층은?", {"subject": "data_communication"}),
    ])
    hits = await idx.search_similar(
        domain_id="t1", question_text="이 트리의 루트는?",
        subject="data_communication", top_k=5,
    )
    assert all(h["subject"] == "data_communication" for h in hits)
    assert "Q-TREE" not in [h["item_id"] for h in hits]


async def test_search_excludes_self():
    idx = _index()
    await idx.ensure_collection(domain_id="t1", dense_dim=3)
    await idx.index_items(domain_id="t1", items=[
        ("Q-TREE", "이 트리의 루트는?", {"subject": "programming"}),
    ])
    hits = await idx.search_similar(
        domain_id="t1", question_text="이 트리의 루트는?",
        top_k=5, exclude_item_id="Q-TREE",
    )
    assert "Q-TREE" not in [h["item_id"] for h in hits]


async def test_reindex_same_item_id_overwrites():
    idx = _index()
    await idx.ensure_collection(domain_id="t1", dense_dim=3)
    await idx.index_items(domain_id="t1", items=[("Q1", "이 트리의 루트는?", {})])
    await idx.index_items(domain_id="t1", items=[("Q1", "이 트리의 루트는?", {})])
    hits = await idx.search_similar(domain_id="t1", question_text="이 트리의 루트는?", top_k=10)
    # 같은 item_id는 한 point로 유지(중복 누적 없음)
    assert [h["item_id"] for h in hits].count("Q1") == 1


async def test_near_duplicate_high_score():
    idx = _index()
    await idx.ensure_collection(domain_id="t1", dense_dim=3)
    await idx.index_items(domain_id="t1", items=[("Q-TREE", "이 트리의 루트는?", {})])
    hits = await idx.search_similar(domain_id="t1", question_text="이 트리의 루트는?", top_k=1)
    assert hits[0]["score"] > 0.99  # 동일 벡터 → cosine ~1.0
