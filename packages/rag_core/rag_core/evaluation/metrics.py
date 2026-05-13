"""Evaluation metrics — retrieval recall, citation accuracy, unsupported ratio, fallback rate.

각 함수는 case 단위 입력을 받아 0.0~1.0 점수를 반환. 집계는 aggregate_metrics가 담당.
ADR-009 §7 promotion_gate.yaml 키와 정확히 일치한다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


def retrieval_recall_at_k(
    *,
    retrieved_chunk_ids: list[str],
    expected_chunk_ids: list[str],
    k: int = 5,
) -> float:
    """top-k 검색 결과에 expected chunk가 얼마나 포함되었는지.

    expected가 비어있으면 평가 제외 의미로 1.0 반환 (downstream 집계가 평균에 영향 없도록).
    """
    if not expected_chunk_ids:
        return 1.0
    if not retrieved_chunk_ids:
        return 0.0
    topk = set(retrieved_chunk_ids[:k])
    hits = sum(1 for cid in expected_chunk_ids if cid in topk)
    return hits / len(expected_chunk_ids)


def citation_accuracy(
    *,
    cited_chunk_ids: list[str],
    expected_chunk_ids: list[str],
) -> float:
    """응답 citation이 expected chunk를 얼마나 정확히 참조했는지 (precision-style).

    expected가 비어있으면 1.0. citation이 없으면 0.0.
    """
    if not expected_chunk_ids:
        return 1.0
    if not cited_chunk_ids:
        return 0.0
    expected_set = set(expected_chunk_ids)
    correct = sum(1 for c in cited_chunk_ids if c in expected_set)
    return correct / len(cited_chunk_ids)


def unsupported_ratio(
    *,
    answer_segments: list[dict],
) -> float:
    """unsupported segment 수 / 총 segment 수. answer_segments가 없으면 0.0."""
    if not answer_segments:
        return 0.0
    total = len(answer_segments)
    unsupported = sum(
        1 for seg in answer_segments if seg.get("support_type") == "unsupported"
    )
    return unsupported / total if total else 0.0


def fallback_rate(case_outcomes: Iterable[bool]) -> float:
    """fallback이 발생한 case 비율. 빈 입력은 0.0."""
    outcomes = list(case_outcomes)
    if not outcomes:
        return 0.0
    return sum(1 for x in outcomes if x) / len(outcomes)


@dataclass
class EvaluationSummary:
    """dataset 단위 집계 결과 — promotion_gate 비교에 사용."""

    total_cases: int
    retrieval_recall_at_5: float
    citation_accuracy: float
    unsupported_ratio: float
    fallback_rate: float
    pass_count: int  # must_include + expected_fallback 검증 통과 케이스 수
    fail_count: int

    def to_dict(self) -> dict[str, float | int]:
        return {
            "total_cases": self.total_cases,
            "retrieval_recall_at_5": self.retrieval_recall_at_5,
            "citation_accuracy": self.citation_accuracy,
            "unsupported_ratio": self.unsupported_ratio,
            "fallback_rate": self.fallback_rate,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
        }


def aggregate_metrics(
    *,
    per_case_recall: list[float],
    per_case_citation: list[float],
    per_case_unsupported: list[float],
    per_case_fallback: list[bool],
    per_case_pass: list[bool],
) -> EvaluationSummary:
    """case별 metric 리스트를 dataset 단위로 평균."""
    total = len(per_case_recall)

    def _avg(values: list[float]) -> float:
        if not values:
            return 0.0
        return sum(values) / len(values)

    return EvaluationSummary(
        total_cases=total,
        retrieval_recall_at_5=_avg(per_case_recall),
        citation_accuracy=_avg(per_case_citation),
        unsupported_ratio=_avg(per_case_unsupported),
        fallback_rate=fallback_rate(per_case_fallback),
        pass_count=sum(1 for x in per_case_pass if x),
        fail_count=sum(1 for x in per_case_pass if not x),
    )
