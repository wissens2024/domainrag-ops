"""
4-type Citation verifier 구현 (ADR-010).

Tier 1: 마커 정합성
Tier 2: claim ↔ chunk embedding similarity
Tier 3: unsupported claim 탐지
LLM-as-judge: inference type 검증
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class CitationCandidate:
    marker: str  # "[1]"
    citation_indices: list[int]  # [1] or [1,3] for synthesis
    claim_text: str
    support_type: str  # direct | synthesis | inference | conflict
    cited_chunks: list[dict]


class Tier1MarkerVerifier:
    """ADR-010 Tier 1 — context 번호 범위 내 마커만 통과."""

    def verify(
        self,
        candidates: list[CitationCandidate],
        max_context_index: int,
    ) -> list[CitationCandidate]:
        return [
            c for c in candidates
            if all(1 <= i <= max_context_index for i in c.citation_indices)
        ]


class Tier2SimilarityVerifier:
    """ADR-010 Tier 2 — claim·chunk embedding cosine similarity로 support_level 분류."""

    def __init__(
        self,
        thresholds: dict[str, float] | None = None,
    ):
        self.thresholds = thresholds or {"strong": 0.75, "medium": 0.55}

    def classify(self, similarity: float) -> tuple[str, bool]:
        """반환: (support_level, verified)."""
        if similarity >= self.thresholds["strong"]:
            return "strong", True
        if similarity >= self.thresholds["medium"]:
            return "medium", True
        return "weak", False

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        va, vb = np.array(a), np.array(b)
        denom = np.linalg.norm(va) * np.linalg.norm(vb)
        if denom == 0:
            return 0.0
        return float(np.dot(va, vb) / denom)


class Tier3UnsupportedDetector:
    """ADR-010 Tier 3 — citation 마커 없는 factual segment 식별."""

    def detect(
        self,
        answer_segments: list[dict],
        *,
        mode: str = "structured",  # structured | heuristic
    ) -> tuple[list[int], float]:
        """반환: (unsupported segment 인덱스 목록, unsupported_ratio)."""
        if mode == "structured":
            unsupported = [
                i for i, seg in enumerate(answer_segments)
                if not seg.get("citations")
            ]
        else:
            # heuristic: factual 문장이지만 마커 없음 — Phase 후속에 정규화
            unsupported = []
        total = max(len(answer_segments), 1)
        return unsupported, len(unsupported) / total


# ADR-010 §4 LLM-as-judge 운영 구현은 `rag_core.services.judge_service.JudgeService`다.
# 이전에 본 모듈에 존재하던 `InferenceJudge` stub은 dead code였으므로 제거했다.
