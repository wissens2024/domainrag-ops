"""CitationReverifier unit tests — ADR-010 §4 Tier 2 재계산 순수 로직.

Embedder를 Mock으로 주입해 cosine similarity 산출 결과를 결정론적으로 검증.
"""

from __future__ import annotations

import pytest

from rag_core.services.citation_reverifier import (
    CitationReverifier,
    ReverifyThresholds,
)


class _FixedEmbedder:
    """텍스트→벡터 사전 매핑. 호출 시 embed_batch 결과를 매핑에서 조회."""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._m = mapping
        self.dense_dim = next(iter(mapping.values())).__len__() if mapping else 0

    async def embed_batch(self, texts: list[str]):
        out = []
        for t in texts:
            if t not in self._m:
                raise KeyError(f"unmapped: {t}")
            out.append(list(self._m[t]))
        return out

    async def embed(self, text: str):
        return list(self._m[text])


async def test_reverify_upgrades_when_threshold_relaxed():
    """기존 weak이지만 similarity 0.7 → 새 threshold(medium=0.5)에서 medium으로 상향."""
    embedder = _FixedEmbedder({
        "claim text": [1.0, 0.0],
        "excerpt text": [0.7, 0.7],  # cos ≈ 0.7071
    })
    reverifier = CitationReverifier(
        embedder=embedder,
        thresholds=ReverifyThresholds(strong=0.9, medium=0.5),
    )
    out = await reverifier.reverify_one(
        request_id="r1",
        citations=[
            {
                "claim_text": "claim text",
                "excerpt": "excerpt text",
                "support_level": "weak",
                "similarity": 0.3,
            }
        ],
    )
    after = out.citations_after[0]
    assert after["support_level"] == "medium"
    assert after["verified"] is True
    assert 0.69 <= after["similarity"] <= 0.72
    assert out.upgraded == 1
    assert out.downgraded == 0


async def test_reverify_downgrades_when_threshold_tightened():
    """기존 strong이었지만 새 threshold(strong=0.95)에서 medium으로 하향."""
    embedder = _FixedEmbedder({
        "claim": [1.0, 0.0],
        "ex": [0.7, 0.7],  # ≈ 0.7071
    })
    reverifier = CitationReverifier(
        embedder=embedder,
        thresholds=ReverifyThresholds(strong=0.95, medium=0.5),
    )
    out = await reverifier.reverify_one(
        request_id="r2",
        citations=[
            {
                "claim_text": "claim",
                "excerpt": "ex",
                "support_level": "strong",
                "similarity": 0.99,
            }
        ],
    )
    after = out.citations_after[0]
    assert after["support_level"] == "medium"
    assert out.downgraded == 1
    assert out.upgraded == 0


async def test_reverify_skips_citations_without_excerpt():
    """excerpt가 없는 citation은 그대로 통과 (재임베딩 안 함)."""
    embedder = _FixedEmbedder({})  # 호출 안 됨
    reverifier = CitationReverifier(
        embedder=embedder,
        thresholds=ReverifyThresholds(strong=0.9, medium=0.5),
    )
    out = await reverifier.reverify_one(
        request_id="r3",
        citations=[
            {"claim_text": "claim", "excerpt": "", "support_level": "weak"}
        ],
    )
    assert out.upgraded == 0
    assert out.downgraded == 0
    assert out.citations_after[0]["support_level"] == "weak"


async def test_reverify_handles_orthogonal_vectors_as_weak():
    embedder = _FixedEmbedder({
        "c": [1.0, 0.0],
        "e": [0.0, 1.0],  # cos = 0
    })
    reverifier = CitationReverifier(
        embedder=embedder,
        thresholds=ReverifyThresholds(strong=0.9, medium=0.5),
    )
    out = await reverifier.reverify_one(
        request_id="r4",
        citations=[
            {"claim_text": "c", "excerpt": "e", "support_level": "strong"}
        ],
    )
    after = out.citations_after[0]
    assert after["support_level"] == "weak"
    assert after["verified"] is False
    assert out.downgraded == 1
