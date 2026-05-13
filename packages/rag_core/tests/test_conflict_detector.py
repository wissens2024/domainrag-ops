"""ConflictDetector unit tests — ADR-010 §7 secondary 휴리스틱."""

from __future__ import annotations

from rag_core.interfaces.retriever import RetrievedChunk
from rag_core.services.conflict_detector import (
    ConflictDetector,
    ConflictDetectionResult,
)


def _ctx(cid: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        doc_id=f"d-{cid}",
        title=f"T-{cid}",
        content=content,
        page_number=1,
        section_title="S",
        dense_score=0.0,
        sparse_score=0.0,
        fused_score=0.5,
        payload={"doc_id": f"d-{cid}"},
        rerank_score=0.7,
    )


def _seg(citations: list[int], support_type: str = "synthesis") -> dict:
    return {
        "index": 0,
        "text": "claim",
        "citations": citations,
        "support_type": support_type,
        "inference_chain": None,
        "conflict_groups": None,
    }


def _all_enabled() -> ConflictDetector:
    return ConflictDetector(
        enabled_patterns={"date_diff", "numeric_diff", "rule_id_diff"}
    )


# --------------------------------------------------------------------------- #
# from_config / enable
# --------------------------------------------------------------------------- #


def test_from_config_disabled_by_default() -> None:
    d = ConflictDetector.from_config(None)
    assert d.enabled is False


def test_from_config_disabled_when_enable_false() -> None:
    d = ConflictDetector.from_config(
        {"verification": {"conflict_detection": {"heuristic": {"enable": False}}}}
    )
    assert d.enabled is False


def test_from_config_with_explicit_patterns() -> None:
    d = ConflictDetector.from_config(
        {
            "verification": {
                "conflict_detection": {
                    "heuristic": {
                        "enable": True,
                        "patterns": ["date_diff", "rule_id_diff"],
                    }
                }
            }
        }
    )
    assert d.enabled is True


def test_from_config_unknown_pattern_is_dropped() -> None:
    d = ConflictDetector.from_config(
        {
            "verification": {
                "conflict_detection": {
                    "heuristic": {
                        "enable": True,
                        "patterns": ["date_diff", "bogus"],
                    }
                }
            }
        }
    )
    assert d.enabled is True


# --------------------------------------------------------------------------- #
# Skip cases
# --------------------------------------------------------------------------- #


def test_no_detection_when_disabled() -> None:
    d = ConflictDetector(enabled_patterns=set())
    contexts = [_ctx("c1", "기준일 2024-01-01"), _ctx("c2", "기준일 2025-06-15")]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is False


def test_no_detection_for_single_citation() -> None:
    d = _all_enabled()
    contexts = [_ctx("c1", "기준일 2024-01-01")]
    result = d.detect_in_segment(_seg([1]), contexts)
    assert result.is_conflict is False


def test_no_detection_for_existing_conflict_segment() -> None:
    """LLM Primary가 이미 conflict로 마킹한 segment는 휴리스틱이 건드리지 않는다."""
    d = _all_enabled()
    contexts = [_ctx("c1", "2024-01-01"), _ctx("c2", "2025-06-15")]
    result = d.detect_in_segment(_seg([1, 2], support_type="conflict"), contexts)
    assert result.is_conflict is False


# --------------------------------------------------------------------------- #
# date_diff
# --------------------------------------------------------------------------- #


def test_date_diff_detected_iso() -> None:
    d = _all_enabled()
    contexts = [
        _ctx("c1", "시행일 2024-01-01 부터 적용"),
        _ctx("c2", "시행일 2025-06-15 부터 적용"),
    ]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is True
    assert result.signal == "date_diff"
    assert sorted(result.conflict_groups) == [[1], [2]]


def test_date_diff_detected_korean_format() -> None:
    d = _all_enabled()
    contexts = [
        _ctx("c1", "2024년 1월 1일부터 시행"),
        _ctx("c2", "2025년 6월 15일부터 시행"),
    ]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is True
    assert result.signal == "date_diff"


def test_date_same_no_detection() -> None:
    d = _all_enabled()
    contexts = [
        _ctx("c1", "2024-01-01 시행"),
        _ctx("c2", "2024-01-01 시행 본 정책"),
    ]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is False


def test_date_groups_collapse_when_two_chunks_share_date() -> None:
    d = _all_enabled()
    contexts = [
        _ctx("c1", "2024-01-01 시행"),
        _ctx("c2", "2024-01-01 시행"),
        _ctx("c3", "2025-06-15 시행"),
    ]
    result = d.detect_in_segment(_seg([1, 2, 3]), contexts)
    assert result.is_conflict is True
    assert result.signal == "date_diff"
    # c1·c2가 같은 그룹, c3 별도
    flat = [sorted(g) for g in result.conflict_groups]
    assert sorted(flat) == [[1, 2], [3]]


# --------------------------------------------------------------------------- #
# numeric_diff (단위별 비교)
# --------------------------------------------------------------------------- #


def test_numeric_diff_same_unit_different_values() -> None:
    d = _all_enabled()
    contexts = [
        _ctx("c1", "패스워드 만료 주기는 30일이다"),
        _ctx("c2", "패스워드 만료 주기는 90일이다"),
    ]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is True
    assert result.signal == "numeric_diff"
    assert result.details.get("unit") == "일"


def test_numeric_same_value_no_detection() -> None:
    d = _all_enabled()
    contexts = [
        _ctx("c1", "만료 주기 30일"),
        _ctx("c2", "만료 주기 30일 정책"),
    ]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is False


def test_numeric_only_one_chunk_has_unit_no_detection() -> None:
    """한쪽 chunk만 단위가 잡히면 conflict 아님 (비교 불가)."""
    d = _all_enabled()
    contexts = [
        _ctx("c1", "만료 30일"),
        _ctx("c2", "패스워드 정책 일반 안내"),
    ]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is False


# --------------------------------------------------------------------------- #
# rule_id_diff
# --------------------------------------------------------------------------- #


def test_rule_id_diff_korean() -> None:
    d = _all_enabled()
    contexts = [
        _ctx("c1", "본 절차는 제5조에 의거한다"),
        _ctx("c2", "본 절차는 제7조에 의거한다"),
    ]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is True
    assert result.signal == "rule_id_diff"


def test_rule_id_diff_canonical_across_languages() -> None:
    """제5조와 Article 5는 같은 canonical id로 매핑되어 conflict 아님."""
    d = _all_enabled()
    contexts = [
        _ctx("c1", "제5조에 의거"),
        _ctx("c2", "Article 5 governs this case"),
    ]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is False


def test_rule_id_diff_clause_difference() -> None:
    """제5조와 제5조제2항은 다른 canonical id."""
    d = _all_enabled()
    contexts = [
        _ctx("c1", "제5조"),
        _ctx("c2", "제5조 제2항"),
    ]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is True


# --------------------------------------------------------------------------- #
# Pattern enable filter
# --------------------------------------------------------------------------- #


def test_only_enabled_patterns_evaluated() -> None:
    d = ConflictDetector(enabled_patterns={"rule_id_diff"})
    contexts = [
        _ctx("c1", "기준일 2024-01-01 만료 30일"),
        _ctx("c2", "기준일 2025-06-15 만료 90일"),
    ]
    # date_diff·numeric_diff은 비활성 → conflict 아님
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is False


def test_first_signal_wins() -> None:
    """date_diff·numeric_diff 모두 발견 가능하면 date_diff(먼저 평가)이 반환."""
    d = _all_enabled()
    contexts = [
        _ctx("c1", "기준일 2024-01-01 만료 30일"),
        _ctx("c2", "기준일 2025-06-15 만료 90일"),
    ]
    result = d.detect_in_segment(_seg([1, 2]), contexts)
    assert result.is_conflict is True
    assert result.signal == "date_diff"


def test_result_dataclass_default_safe() -> None:
    """is_conflict=False일 때 conflict_groups는 빈 리스트로 안전."""
    r = ConflictDetectionResult(is_conflict=False)
    assert r.conflict_groups == []
    assert r.signal is None
    assert r.details == {}
