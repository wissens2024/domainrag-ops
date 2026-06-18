"""AssessmentSimilarityChecker — ADR-014 §2 similarity check.

신규 question_text가 기존 same-subject items와 얼마나 유사한지 cosine 산출.
duplicate(>= threshold_duplicate)면 reject, similar(>= threshold_similar)는
참고 후보로 표기.

본 모듈은 *순수* 함수형. Qdrant 호출은 caller(backend orchestrator)가 candidates를
미리 가져와 전달한다. InMemory에서는 repository.list_by_tenant + same-subject 필터.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from rag_core.interfaces.embedder import Embedder
from rag_core.interfaces.assessment_item_repository import (
    AssessmentItemRecord,
)


@dataclass
class SimilarityThresholds:
    duplicate: float = 0.85
    similar: float = 0.65


@dataclass
class SimilarityCheckResult:
    new_question_text: str
    max_similarity: float = 0.0
    is_duplicate: bool = False
    similar_candidates: list[dict[str, Any]] = field(default_factory=list)
    # similar_candidates[i]: {item_id, similarity}


class AssessmentSimilarityChecker:
    def __init__(
        self,
        *,
        embedder: Embedder,
        thresholds: SimilarityThresholds,
    ) -> None:
        self._embedder = embedder
        self._thresholds = thresholds

    @property
    def thresholds(self) -> SimilarityThresholds:
        return self._thresholds

    async def check(
        self,
        *,
        new_question_text: str,
        candidates: list[AssessmentItemRecord],
    ) -> SimilarityCheckResult:
        result = SimilarityCheckResult(new_question_text=new_question_text)
        if not candidates:
            return result
        texts = [new_question_text] + [c.question_text for c in candidates]
        embeddings = await self._embedder.embed_batch(texts)
        new_vec = _dense(embeddings[0])
        for i, cand in enumerate(candidates):
            sim = _cosine(new_vec, _dense(embeddings[i + 1]))
            if sim > result.max_similarity:
                result.max_similarity = sim
            if sim >= self._thresholds.similar:
                result.similar_candidates.append(
                    {"item_id": cand.item_id, "similarity": round(sim, 4)}
                )
        result.is_duplicate = result.max_similarity >= self._thresholds.duplicate
        return result


def _dense(embedding) -> list[float]:
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
