"""CitationReverifier — ADR-010 §4 Tier 2 재검증 단일 진입점 서비스.

운영자가 threshold(citation.yaml.verification.tier2)를 변경한 뒤 과거 chat_logs에
새 threshold를 적용하려는 경우 사용. claim_text + 저장된 excerpt(audit truth)를
재임베딩하여 cosine similarity를 계산하고 support_level을 재산출한다.

본 모듈은 *순수* 재계산 로직만 제공. 실제 chat_logs UPDATE는 backend의
ChatLogCitationUpdater에서 수행 (RLS context + JSONB CAST 책임).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from rag_core.interfaces.embedder import Embedder


@dataclass
class ReverifyThresholds:
    strong: float
    medium: float


@dataclass
class ReverifyResult:
    request_id: str
    citations_before: list[dict[str, Any]]
    citations_after: list[dict[str, Any]]
    avg_similarity_before: float | None
    avg_similarity_after: float
    upgraded: int  # support_level 상향 (weak→medium 등)
    downgraded: int


class CitationReverifier:
    """Tier 2 재검증 순수 함수.

    Args:
        embedder: chunks 인덱싱과 동일 모델 (audit truth 재현용)
        thresholds: 현재 citation.yaml.verification.tier2.thresholds
    """

    _ORDER = {"strong": 2, "medium": 1, "weak": 0}

    def __init__(self, *, embedder: Embedder, thresholds: ReverifyThresholds) -> None:
        self._embedder = embedder
        self._thresholds = thresholds

    async def reverify_one(
        self, *, request_id: str, citations: list[dict[str, Any]]
    ) -> ReverifyResult:
        """citations 내 (claim_text, excerpt) 쌍에 대해 similarity 재계산.

        excerpt가 비어 있으면 해당 citation은 변경 없이 통과.
        """
        targets: list[tuple[int, str, str]] = []  # (citation index, claim, excerpt)
        for i, c in enumerate(citations):
            claim = (c.get("claim_text") or "").strip()
            excerpt = (c.get("excerpt") or "").strip()
            if claim and excerpt:
                targets.append((i, claim, excerpt))

        new_citations = [dict(c) for c in citations]
        if not targets:
            return ReverifyResult(
                request_id=request_id,
                citations_before=list(citations),
                citations_after=new_citations,
                avg_similarity_before=_avg([c.get("similarity") for c in citations]),
                avg_similarity_after=_avg([c.get("similarity") for c in citations]),
                upgraded=0,
                downgraded=0,
            )

        texts = []
        for _, claim, excerpt in targets:
            texts.append(claim)
            texts.append(excerpt)
        embeddings = await self._embedder.embed_batch(texts)

        upgraded = 0
        downgraded = 0
        similarities: list[float] = []
        for offset, (idx, _claim, _excerpt) in enumerate(targets):
            claim_emb = _dense(embeddings[offset * 2])
            chunk_emb = _dense(embeddings[offset * 2 + 1])
            sim = _cosine(claim_emb, chunk_emb)
            similarities.append(sim)
            new_level = _classify(sim, self._thresholds)
            old_level = new_citations[idx].get("support_level")
            new_citations[idx]["similarity"] = round(sim, 4)
            new_citations[idx]["support_level"] = new_level
            new_citations[idx]["verified"] = new_level in {"strong", "medium"}
            if old_level in self._ORDER and new_level in self._ORDER:
                if self._ORDER[new_level] > self._ORDER[old_level]:
                    upgraded += 1
                elif self._ORDER[new_level] < self._ORDER[old_level]:
                    downgraded += 1

        return ReverifyResult(
            request_id=request_id,
            citations_before=list(citations),
            citations_after=new_citations,
            avg_similarity_before=_avg([c.get("similarity") for c in citations]),
            avg_similarity_after=_avg(similarities) if similarities else 0.0,
            upgraded=upgraded,
            downgraded=downgraded,
        )


def _classify(sim: float, t: ReverifyThresholds) -> str:
    if sim >= t.strong:
        return "strong"
    if sim >= t.medium:
        return "medium"
    return "weak"


def _dense(embedding) -> list[float]:
    """Embedder.embed_batch가 (dense, sparse) 튜플을 반환할 수도, dense list만 반환할
    수도 있음. ADR-011 운영 구현은 튜플, _FixedEmbedder 같은 단순 mock은 dense만.
    """
    if isinstance(embedding, tuple):
        return list(embedding[0])
    return list(embedding)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _avg(values) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return None
    return sum(nums) / len(nums)


# --------------------------------------------------------------------------- #
# Audit timestamp helper
# --------------------------------------------------------------------------- #


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
