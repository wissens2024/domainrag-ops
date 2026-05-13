"""VerifierService — Tier 1/2/3 + assemble + confidence + Gate 2."""

from __future__ import annotations

import pytest

from rag_core.clients.in_memory import InMemoryEmbedder
from rag_core.interfaces.retriever import RetrievedChunk
from rag_core.services.verifier_service import (
    VerifierService,
    VerifierThresholds,
)


def _ctx(cid: str, content: str, *, fused=0.5, rerank=0.7) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        doc_id=f"d-{cid}",
        title=f"T-{cid}",
        content=content,
        page_number=1,
        section_title="S",
        dense_score=0.0,
        sparse_score=0.0,
        fused_score=fused,
        payload={"doc_id": f"d-{cid}"},
        rerank_score=rerank,
    )


def test_thresholds_from_config_defaults():
    t = VerifierThresholds.from_config(None)
    assert t.strong_threshold == 0.75
    assert t.medium_threshold == 0.55
    assert t.weight_retrieval + t.weight_verified + t.weight_supported + t.weight_coverage == pytest.approx(1.0)


def test_thresholds_from_citation_yaml_shape():
    cfg = {
        "verification": {
            "tier2": {"thresholds": {"strong": 0.8, "medium": 0.6}},
            "inference_judge": {"enable": True},
        },
        "gates": {
            "generation": {
                "min_verified_count": 2,
                "max_unsupported_ratio": 0.3,
                "min_confidence": 0.7,
            }
        },
        "confidence_weights": {
            "retrieval": 0.4,
            "verified": 0.3,
            "supported": 0.2,
            "coverage": 0.1,
        },
    }
    t = VerifierThresholds.from_config(cfg)
    assert t.strong_threshold == 0.8
    assert t.medium_threshold == 0.6
    assert t.min_verified_count == 2
    assert t.max_unsupported_ratio == 0.3
    assert t.min_confidence == 0.7
    assert t.weight_retrieval == 0.4
    assert t.inference_judge_enabled is True


def test_parse_segments_normalizes_defaults():
    raw = [
        {"text": "a", "citations": [1], "support_type": "direct"},
        {"text": "b"},  # 누락
        {"text": "c", "citation_type": "synthesis", "citations": [1, 2]},
    ]
    out = VerifierService.parse_segments(raw)
    assert out[0]["index"] == 0 and out[0]["citations"] == [1]
    assert out[1]["citations"] == [] and out[1]["support_type"] == "direct"
    assert out[2]["support_type"] == "synthesis"


def test_tier1_strips_out_of_range_indices():
    segs = VerifierService.parse_segments(
        [
            {"text": "a", "citations": [1, 9, 2], "support_type": "direct"},
            {"text": "b", "citations": [3], "support_type": "direct"},
        ]
    )
    out, removed = VerifierService.tier1_filter(segs, num_contexts=2)
    assert out[0]["citations"] == [1, 2]
    assert out[1]["citations"] == []
    assert removed == 2  # 9, 3 둘 다 제거


async def test_tier2_classifies_and_drops_weak():
    embedder = InMemoryEmbedder(dense_dim=64)
    svc = VerifierService(embedder=embedder)
    contexts = [
        _ctx("c1", "패스워드는 12자 이상이어야 합니다"),
        _ctx("c2", "전혀 무관한 다른 주제 텍스트입니다"),
    ]
    segs = VerifierService.parse_segments(
        [
            {
                "text": "패스워드는 12자 이상이어야 합니다",
                "citations": [1, 2],
                "support_type": "direct",
            }
        ]
    )
    # 임계 낮춤 — InMemory 임베더 cosine이 1.0/낮음 분리되도록
    t = VerifierThresholds(strong_threshold=0.99, medium_threshold=0.85)
    out, _, avg = await svc.tier2_classify(segs, contexts, t)
    # 동일 문자열이므로 cosine ~= 1.0 → strong 통과 (citation 1 유지)
    # 무관 텍스트는 medium 미만 → 제거 (citation 2 빠짐)
    assert 1 in out[0]["citations"]
    assert 2 not in out[0]["citations"]
    assert out[0]["citation_meta"][1].support_level in {"strong", "medium"}
    assert avg > 0.0


async def test_tier2_synthesis_requires_all_verified():
    embedder = InMemoryEmbedder(dense_dim=64)
    svc = VerifierService(embedder=embedder)
    contexts = [
        _ctx("c1", "패스워드는 12자 이상"),
        _ctx("c2", "전혀 무관한 텍스트"),
    ]
    segs = VerifierService.parse_segments(
        [
            {
                "text": "패스워드는 12자 이상",
                "citations": [1, 2],
                "support_type": "synthesis",
            }
        ]
    )
    t = VerifierThresholds(strong_threshold=0.99, medium_threshold=0.85)
    out, _, _ = await svc.tier2_classify(segs, contexts, t)
    # synthesis는 한 chunk(2)가 weak이므로 segment 자체 다운그레이드 (citations 모두 제거)
    assert out[0]["citations"] == []
    assert out[0]["downgraded"] is True
    assert out[0]["downgrade_reason"] == "synthesis_chunk_weak"


async def test_tier2_inference_downgraded_without_judge():
    embedder = InMemoryEmbedder(dense_dim=64)
    svc = VerifierService(embedder=embedder)
    contexts = [_ctx("c1", "패스워드는 12자 이상")]
    segs = VerifierService.parse_segments(
        [
            {
                "text": "이로부터 보안성이 높다고 추론됩니다",
                "citations": [1],
                "support_type": "inference",
            }
        ]
    )
    out, _, _ = await svc.tier2_classify(segs, contexts, VerifierThresholds())
    assert out[0]["citations"] == []
    assert out[0]["downgraded"] is True
    assert out[0]["downgrade_reason"] == "judge_not_enabled"


def test_tier3_unsupported_ratio():
    segs = [
        {"index": 0, "text": "a", "citations": [1], "support_type": "direct"},
        {"index": 1, "text": "b", "citations": [], "support_type": "direct"},
        {"index": 2, "text": "c", "citations": [], "support_type": "direct"},
    ]
    idxs, ratio = VerifierService.tier3_unsupported(segs)
    assert idxs == [1, 2]
    assert ratio == pytest.approx(2 / 3)


def test_assemble_citations_includes_meta():
    from rag_core.services.verifier_service import VerifiedCitation

    contexts = [_ctx("c1", "내용", fused=0.6, rerank=0.8)]
    segs = [
        {
            "index": 0,
            "text": "내용",
            "citations": [1],
            "support_type": "direct",
            "citation_meta": {
                1: VerifiedCitation(
                    segment_index=0, chunk_index=1, similarity=0.85,
                    support_level="strong", verified=True
                )
            },
        }
    ]
    citations, types = VerifierService.assemble_citations(
        segs, contexts, tenant_id="security"
    )
    assert len(citations) == 1
    c = citations[0]
    assert c["tenant_id"] == "security"
    assert c["support_level"] == "strong"
    assert c["verified"] is True
    assert c["similarity"] == 0.85
    assert c["chunk_id"] == "c1"
    assert c["rerank_score"] == 0.8
    assert types == ["direct"]


def test_compute_confidence_weighted_sum():
    contexts = [_ctx("c1", "x", fused=0.8, rerank=0.9)]
    segs = [
        {"index": 0, "text": "a", "citations": [1], "support_type": "direct"},
        {"index": 1, "text": "b", "citations": [], "support_type": "direct"},
    ]
    citations = [{"verified": True}]
    t = VerifierThresholds()
    score = VerifierService.compute_confidence(
        segments=segs,
        citations=citations,
        contexts=contexts,
        unsupported_ratio=0.5,
        thresholds=t,
    )
    # retrieval=0.9*0.30 + verified=1.0*0.30 + supported=0.5*0.20 + coverage=0.5*0.20
    expected = 0.9 * 0.30 + 1.0 * 0.30 + 0.5 * 0.20 + 0.5 * 0.20
    assert score == pytest.approx(expected, abs=1e-9)


def test_gate_2_pass():
    t = VerifierThresholds(min_verified_count=1, max_unsupported_ratio=0.5, min_confidence=0.5)
    citations = [{"verified": True}]
    passed, reason = VerifierService.evaluate_gate_2(
        citations=citations, unsupported_ratio=0.3, confidence=0.7, thresholds=t,
    )
    assert passed is True
    assert reason is None


def test_gate_2_fail_min_verified_count():
    t = VerifierThresholds(min_verified_count=2)
    citations = [{"verified": True}]
    passed, reason = VerifierService.evaluate_gate_2(
        citations=citations, unsupported_ratio=0.0, confidence=1.0, thresholds=t,
    )
    assert passed is False
    assert reason == "below_min_verified_count"


def test_gate_2_fail_unsupported_ratio():
    t = VerifierThresholds(max_unsupported_ratio=0.3)
    citations = [{"verified": True}]
    passed, reason = VerifierService.evaluate_gate_2(
        citations=citations, unsupported_ratio=0.5, confidence=1.0, thresholds=t,
    )
    assert passed is False
    assert reason == "above_max_unsupported_ratio"


def test_gate_2_fail_confidence():
    t = VerifierThresholds(min_confidence=0.7)
    citations = [{"verified": True}]
    passed, reason = VerifierService.evaluate_gate_2(
        citations=citations, unsupported_ratio=0.0, confidence=0.5, thresholds=t,
    )
    assert passed is False
    assert reason == "below_min_confidence"


async def test_run_full_path_happy():
    embedder = InMemoryEmbedder(dense_dim=64)
    svc = VerifierService(embedder=embedder)
    contexts = [
        _ctx("c1", "패스워드는 12자 이상", fused=0.6, rerank=0.85),
        _ctx("c2", "만료 주기는 90일", fused=0.6, rerank=0.80),
    ]
    raw = [
        {"text": "패스워드는 12자 이상", "citations": [1], "support_type": "direct"},
        {"text": "만료 주기는 90일", "citations": [2], "support_type": "direct"},
    ]
    t = VerifierThresholds(strong_threshold=0.99, medium_threshold=0.85)
    res = await svc.run(
        raw_segments=raw,
        contexts=contexts,
        tenant_id="security",
        thresholds=t,
    )
    assert res.gate2_passed is True
    assert len(res.citations) == 2
    assert res.unsupported_ratio == 0.0
    assert res.tier1_markers_removed == 0
    assert res.tier2_avg_similarity > 0.0
    assert res.confidence >= 0.5
    assert "패스워드는 12자 이상" in res.final_answer
    assert res.limitations is None


async def test_tier2_conflict_with_groups_kept_at_medium():
    """ADR-010 §5 — conflict_groups 양측 모두 medium+ 통과 시 인정, support_level=medium cap."""
    embedder = InMemoryEmbedder(dense_dim=64)
    svc = VerifierService(embedder=embedder)
    contexts = [
        _ctx("c1", "패스워드 만료 주기 30일"),
        _ctx("c2", "패스워드 만료 주기 90일"),
    ]
    segs = VerifierService.parse_segments(
        [
            {
                "text": "패스워드 만료 주기 30일",  # claim·c1과 동일
                "citations": [1, 2],
                "support_type": "conflict",
                "conflict_groups": [[1], [2]],
            }
        ]
    )
    # medium 임계 낮춤 — c2(90일)도 cosine ~0.85 이상 나오게
    t = VerifierThresholds(strong_threshold=0.99, medium_threshold=0.50)
    out, _, _ = await svc.tier2_classify(segs, contexts, t)
    seg = out[0]
    assert seg.get("downgraded") is not True
    assert set(seg["citations"]) == {1, 2}
    # support_level은 medium으로 cap (strong이어도 medium으로 내려야 함)
    for cidx in (1, 2):
        assert seg["citation_meta"][cidx].support_level == "medium"
        assert seg["citation_meta"][cidx].verified is True


async def test_tier2_conflict_with_weak_group_downgraded():
    """한쪽 그룹의 chunk가 weak이면 conflict 인정 안 됨 → 다운그레이드."""
    embedder = InMemoryEmbedder(dense_dim=64)
    svc = VerifierService(embedder=embedder)
    contexts = [
        _ctx("c1", "패스워드 만료 주기 30일"),
        _ctx("c2", "전혀 무관한 텍스트"),  # weak
    ]
    segs = VerifierService.parse_segments(
        [
            {
                "text": "패스워드 만료 주기 30일",
                "citations": [1, 2],
                "support_type": "conflict",
                "conflict_groups": [[1], [2]],
            }
        ]
    )
    t = VerifierThresholds(strong_threshold=0.99, medium_threshold=0.85)
    out, _, _ = await svc.tier2_classify(segs, contexts, t)
    seg = out[0]
    assert seg["citations"] == []
    assert seg["downgraded"] is True
    assert seg["downgrade_reason"] == "conflict_group_weak"


async def test_tier2_conflict_without_groups_downgraded():
    """conflict_groups 누락 시 다운그레이드 (Primary가 잘못 마킹한 case)."""
    embedder = InMemoryEmbedder(dense_dim=64)
    svc = VerifierService(embedder=embedder)
    contexts = [_ctx("c1", "x"), _ctx("c2", "y")]
    segs = VerifierService.parse_segments(
        [
            {
                "text": "claim",
                "citations": [1, 2],
                "support_type": "conflict",
                # conflict_groups 의도적으로 누락
            }
        ]
    )
    out, _, _ = await svc.tier2_classify(segs, contexts, VerifierThresholds())
    seg = out[0]
    assert seg["citations"] == []
    assert seg["downgraded"] is True
    assert seg["downgrade_reason"] == "conflict_groups_missing"


async def test_run_inference_segment_limitations_caveat():
    embedder = InMemoryEmbedder(dense_dim=64)
    svc = VerifierService(embedder=embedder)
    contexts = [_ctx("c1", "패스워드는 12자 이상", rerank=0.85)]
    raw = [
        {"text": "12자 이상이 보안에 강하다고 추론됩니다",
         "citations": [1], "support_type": "inference"},
        {"text": "패스워드는 12자 이상",
         "citations": [1], "support_type": "direct"},
    ]
    t = VerifierThresholds(strong_threshold=0.99, medium_threshold=0.85)
    res = await svc.run(
        raw_segments=raw,
        contexts=contexts,
        tenant_id="security",
        thresholds=t,
    )
    # inference segment는 다운그레이드되어 citation 0개
    inf_seg = next(s for s in res.segments if s["support_type"] == "inference")
    assert inf_seg["citations"] == []
    assert inf_seg["downgraded"] is True
    # caveat이 limitations에 포함
    assert res.limitations is not None
    assert "추론" in res.limitations or "충돌" in res.limitations
