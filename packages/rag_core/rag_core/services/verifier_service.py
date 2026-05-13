"""VerifierService — ADR-010 Tier 1/2/3 + assemble + confidence + Gate 2.

본 세션 범위:
  - Tier 1: marker 정합성 (citation index가 [1..len(contexts)])
  - Tier 2: claim·chunk embedding cosine로 support_level (strong/medium/weak)
  - Tier 3: structured 모드 unsupported segment 탐지
  - assemble_citations: 검증 결과를 ADR-017 §3.1 citation 객체로 합성
  - compute_confidence: citation.yaml.confidence_weights 가중합
  - gate_2: citation.yaml.gates.generation 임계 평가

inference·conflict 타입 fail-safe (ADR-010 §5):
  - inference: judge_enabled=False면 모든 citation 제거 + limitations caveat
  - conflict: conflict_groups 누락 시 다운그레이드. groups 존재 시 각 그룹 medium+ 검증
              후 양측 모두 통과 시 support_level=medium cap으로 인정.
              conflict_groups는 LLM Primary 또는 ConflictDetector 휴리스틱이 채운다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ..interfaces.embedder import Embedder
from ..interfaces.retriever import RetrievedChunk

# --------------------------------------------------------------------------- #
# Thresholds / weights (citation.yaml 매핑)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerifierThresholds:
    """citation.yaml의 verification·gates·confidence_weights를 한 객체로 흡수."""

    strong_threshold: float = 0.75
    medium_threshold: float = 0.55

    # gates.generation (Gate 2)
    min_verified_count: int = 1
    max_unsupported_ratio: float = 0.5
    min_confidence: float = 0.5

    # confidence_weights — 합 1.0
    weight_retrieval: float = 0.30
    weight_verified: float = 0.30
    weight_supported: float = 0.20
    weight_coverage: float = 0.20

    # Inference judge 정책 (judge 미결선 동안 fail-safe 작동)
    inference_judge_enabled: bool = False

    @classmethod
    def from_config(cls, citation_config: dict | None) -> "VerifierThresholds":
        cfg = citation_config or {}
        verification = cfg.get("verification") or {}
        tier2 = verification.get("tier2") or {}
        thresholds = tier2.get("thresholds") or {}
        gates = (cfg.get("gates") or {}).get("generation") or {}
        weights = cfg.get("confidence_weights") or {}
        judge = verification.get("inference_judge") or {}
        return cls(
            strong_threshold=float(thresholds.get("strong", 0.75)),
            medium_threshold=float(thresholds.get("medium", 0.55)),
            min_verified_count=int(gates.get("min_verified_count", 1)),
            max_unsupported_ratio=float(gates.get("max_unsupported_ratio", 0.5)),
            min_confidence=float(gates.get("min_confidence", 0.5)),
            weight_retrieval=float(weights.get("retrieval", 0.30)),
            weight_verified=float(weights.get("verified", 0.30)),
            weight_supported=float(weights.get("supported", 0.20)),
            weight_coverage=float(weights.get("coverage", 0.20)),
            inference_judge_enabled=bool(judge.get("enable", False)),
        )


# --------------------------------------------------------------------------- #
# Stage results
# --------------------------------------------------------------------------- #


@dataclass
class VerifiedCitation:
    """단일 segment의 단일 cited chunk에 대한 검증 결과."""

    segment_index: int
    chunk_index: int  # 1-indexed (segment.citations 표기)
    similarity: float
    support_level: str  # strong | medium | weak
    verified: bool


@dataclass
class VerificationResult:
    """assemble_response가 LangGraph state에 채울 최종 결과."""

    segments: list[dict[str, Any]]                   # parse_response에서 normalize된 segment + citation_meta
    citations: list[dict[str, Any]] = field(default_factory=list)  # ADR-017 §3.1 citation 객체
    citation_types: list[str] = field(default_factory=list)
    unsupported_segment_indices: list[int] = field(default_factory=list)
    unsupported_ratio: float = 0.0
    tier1_markers_removed: int = 0
    tier2_avg_similarity: float = 0.0
    confidence: float = 0.0
    gate2_passed: bool = False
    gate2_reason: str | None = None
    final_answer: str = ""
    limitations: str | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _classify(similarity: float, t: VerifierThresholds) -> tuple[str, bool]:
    if similarity >= t.strong_threshold:
        return "strong", True
    if similarity >= t.medium_threshold:
        return "medium", True
    return "weak", False


def _normalize_support_type(seg: dict) -> str:
    raw = seg.get("support_type") or seg.get("citation_type") or "direct"
    if raw == "none":
        return "direct"  # citations 빈 배열로 나중에 unsupported 처리
    return raw


# --------------------------------------------------------------------------- #
# Core service
# --------------------------------------------------------------------------- #


class VerifierService:
    """ADR-010 Tier 1/2/3 + assemble + confidence + Gate 2 orchestrator.

    Args:
        embedder: Tier 2 시 claim·chunk content를 임베딩 (chunks 인덱싱과 동일 모델)
    """

    def __init__(self, *, embedder: Embedder) -> None:
        self._embedder = embedder

    # ----- parse_response ------------------------------------------------ #

    @staticmethod
    def parse_segments(raw_segments: list[dict]) -> list[dict]:
        """LLM raw segments를 검증 파이프라인이 사용하는 정규 dict로 변환.

        - text, citations(list[int]), support_type(str), 선택 inference_chain·conflict_groups
        - citations이 None이면 [], support_type 누락 시 'direct'로 가정
        """
        normalized: list[dict] = []
        for i, seg in enumerate(raw_segments or []):
            text = str(seg.get("text", ""))
            citations = list(seg.get("citations") or [])
            support_type = _normalize_support_type(seg)
            normalized.append(
                {
                    "index": i,
                    "text": text,
                    "citations": [int(c) for c in citations if isinstance(c, int)],
                    "support_type": support_type,
                    "inference_chain": seg.get("inference_chain"),
                    "conflict_groups": seg.get("conflict_groups"),
                }
            )
        return normalized

    # ----- Tier 1 -------------------------------------------------------- #

    @staticmethod
    def tier1_filter(
        segments: list[dict], num_contexts: int
    ) -> tuple[list[dict], int]:
        """citation index가 [1..num_contexts] 범위 밖이면 제거."""
        removed = 0
        out: list[dict] = []
        for seg in segments:
            valid = [c for c in seg["citations"] if 1 <= c <= num_contexts]
            removed += len(seg["citations"]) - len(valid)
            new = dict(seg)
            new["citations"] = valid
            out.append(new)
        return out, removed

    # ----- Tier 2 -------------------------------------------------------- #

    async def tier2_classify(
        self,
        segments: list[dict],
        contexts: list[RetrievedChunk],
        thresholds: VerifierThresholds,
    ) -> tuple[list[dict], list[VerifiedCitation], float]:
        """각 (segment, citation idx) 쌍의 cosine similarity → support_level.

        반환:
          - segments: weak citation 제거되고 segment_assessments 부착된 리스트
          - assessments: 모든 (seg, chunk) 평가 결과 목록 (Tier 3·assemble용)
          - avg_similarity: Tier 2 통계
        """
        # 인덱스 0: claim_text, 1+: chunk content (1-indexed citation과 정렬)
        unique_texts: list[str] = []
        text_to_pos: dict[str, int] = {}

        def _intern(t: str) -> int:
            if t not in text_to_pos:
                text_to_pos[t] = len(unique_texts)
                unique_texts.append(t)
            return text_to_pos[t]

        # 검증 대상이 되는 (seg_idx, chunk_idx) 쌍.
        # inference: judge_enabled일 때만 Tier 2에 포함 (ADR-010 §5: 모든 chunk medium+ 요구).
        # conflict: conflict_groups가 있으면 각 그룹의 chunk를 검증. 누락 시 다운그레이드.
        pairs: list[tuple[int, int, int, int]] = []  # (seg_idx, citation_idx, claim_pos, chunk_pos)
        for seg in segments:
            if seg["support_type"] == "conflict" and not seg.get("conflict_groups"):
                continue
            if (
                seg["support_type"] == "inference"
                and not thresholds.inference_judge_enabled
            ):
                continue
            for cidx in seg["citations"]:
                if not (1 <= cidx <= len(contexts)):
                    continue
                claim_pos = _intern(seg["text"])
                chunk_pos = _intern(contexts[cidx - 1].content)
                pairs.append((seg["index"], cidx, claim_pos, chunk_pos))

        embeddings: list[list[float]] = []
        if unique_texts:
            embedded = await self._embedder.embed_batch(unique_texts)
            embeddings = [dense for dense, _sparse in embedded]

        assessments: list[VerifiedCitation] = []
        sims_for_avg: list[float] = []
        # seg_idx -> {citation_idx -> assessment}
        seg_assessments: dict[int, dict[int, VerifiedCitation]] = {}
        for seg_idx, cidx, cp, kp in pairs:
            sim = _cosine(embeddings[cp], embeddings[kp])
            sims_for_avg.append(sim)
            level, verified = _classify(sim, thresholds)
            a = VerifiedCitation(
                segment_index=seg_idx,
                chunk_index=cidx,
                similarity=sim,
                support_level=level,
                verified=verified,
            )
            assessments.append(a)
            seg_assessments.setdefault(seg_idx, {})[cidx] = a

        # 각 segment의 citation 리스트에서 weak 제거 + meta 부착
        new_segments: list[dict] = []
        for seg in segments:
            assess_map = seg_assessments.get(seg["index"], {})

            # conflict: conflict_groups가 있을 때만 검증 (ADR-010 §5).
            # 양측 그룹 모두 medium+ 통과해야 인정. 한쪽이라도 weak → 다운그레이드.
            if seg["support_type"] == "conflict":
                groups = seg.get("conflict_groups") or []
                if len(groups) < 2:
                    ns = dict(seg)
                    ns["citations"] = []
                    ns["citation_meta"] = {}
                    ns["downgraded"] = True
                    ns["downgrade_reason"] = "conflict_groups_missing"
                    new_segments.append(ns)
                    continue
                all_groups_ok = all(
                    group
                    and all(
                        assess_map.get(c) and assess_map[c].verified for c in group
                    )
                    for group in groups
                )
                if not all_groups_ok:
                    ns = dict(seg)
                    ns["citations"] = []
                    ns["citation_meta"] = {c: assess_map[c] for c in assess_map}
                    ns["downgraded"] = True
                    ns["downgrade_reason"] = "conflict_group_weak"
                    new_segments.append(ns)
                    continue
                # 인정: support_level은 medium에서 cap (충돌은 결정 보류)
                new_meta: dict[int, VerifiedCitation] = {}
                for c in seg["citations"]:
                    m = assess_map.get(c)
                    if m is None:
                        continue
                    capped = "medium" if m.support_level == "strong" else m.support_level
                    new_meta[c] = VerifiedCitation(
                        segment_index=m.segment_index,
                        chunk_index=m.chunk_index,
                        similarity=m.similarity,
                        support_level=capped,
                        verified=True,
                    )
                ns = dict(seg)
                ns["citation_meta"] = new_meta
                new_segments.append(ns)
                continue

            # inference: judge_enabled에 따라 분기
            if seg["support_type"] == "inference":
                if not thresholds.inference_judge_enabled:
                    ns = dict(seg)
                    ns["citations"] = []
                    ns["citation_meta"] = {}
                    ns["downgraded"] = True
                    ns["downgrade_reason"] = "judge_not_enabled"
                    new_segments.append(ns)
                    continue
                # ADR-010 §5: 모든 cited chunk가 medium+ 통과해야 judge 단계로 진입
                if not seg["citations"] or not all(
                    assess_map.get(c) and assess_map[c].verified
                    for c in seg["citations"]
                ):
                    ns = dict(seg)
                    ns["citations"] = []
                    ns["citation_meta"] = {c: assess_map[c] for c in assess_map}
                    ns["downgraded"] = True
                    ns["downgrade_reason"] = "tier2_chunk_weak"
                    new_segments.append(ns)
                    continue
                # 통과: judge_inference 노드가 최종 판정. citation_meta 부착.
                ns = dict(seg)
                ns["citation_meta"] = {c: assess_map[c] for c in seg["citations"]}
                ns["pending_judge"] = True
                new_segments.append(ns)
                continue

            # synthesis: 모든 cited chunk가 medium 이상이어야 인정 (ADR-010 §5)
            if seg["support_type"] == "synthesis":
                if not seg["citations"] or not all(
                    assess_map.get(c) and assess_map[c].verified
                    for c in seg["citations"]
                ):
                    ns = dict(seg)
                    ns["citations"] = []
                    ns["citation_meta"] = {c: assess_map[c] for c in assess_map}
                    ns["downgraded"] = True
                    ns["downgrade_reason"] = "synthesis_chunk_weak"
                    new_segments.append(ns)
                    continue

            # direct(또는 fallback to direct): weak citation만 제거
            kept = [
                c for c in seg["citations"]
                if assess_map.get(c) and assess_map[c].verified
            ]
            ns = dict(seg)
            ns["citations"] = kept
            ns["citation_meta"] = {c: assess_map[c] for c in kept}
            new_segments.append(ns)

        avg = sum(sims_for_avg) / len(sims_for_avg) if sims_for_avg else 0.0
        return new_segments, assessments, avg

    # ----- Inference judge 후처리 ---------------------------------------- #

    @staticmethod
    def apply_judge_result(
        segment: dict,
        *,
        judge_passes: bool,
        reasoning: str | None = None,
        caveat: str | None = None,
    ) -> dict:
        """judge 결과를 inference segment에 반영 (ADR-010 §5).

        통과:
          - pending_judge 제거, citation_meta의 support_level을 "medium"으로 cap
          - judge_reasoning / judge_caveat 부착
        거절:
          - citations 비움, downgraded=True, downgrade_reason="judge_rejected"
        """
        ns = dict(segment)
        ns.pop("pending_judge", None)
        if judge_passes:
            new_meta: dict[int, VerifiedCitation] = {}
            for cidx, meta in (ns.get("citation_meta") or {}).items():
                # cap at medium — strong을 medium으로 강등
                level = "medium" if meta.support_level == "strong" else meta.support_level
                new_meta[cidx] = VerifiedCitation(
                    segment_index=meta.segment_index,
                    chunk_index=meta.chunk_index,
                    similarity=meta.similarity,
                    support_level=level,
                    verified=True,
                )
            ns["citation_meta"] = new_meta
            if reasoning:
                ns["judge_reasoning"] = reasoning
            if caveat:
                ns["judge_caveat"] = caveat
        else:
            ns["citations"] = []
            ns["citation_meta"] = {}
            ns["downgraded"] = True
            ns["downgrade_reason"] = "judge_rejected"
            if reasoning:
                ns["judge_reasoning"] = reasoning
        return ns

    # ----- Tier 3 -------------------------------------------------------- #

    @staticmethod
    def tier3_unsupported(segments: list[dict]) -> tuple[list[int], float]:
        """citations이 비어 있는 segment를 unsupported로 분류."""
        unsupported_idx = [
            seg["index"] for seg in segments if not seg["citations"]
        ]
        ratio = (len(unsupported_idx) / len(segments)) if segments else 0.0
        return unsupported_idx, ratio

    # ----- Assemble + confidence + gate_2 -------------------------------- #

    @staticmethod
    def assemble_citations(
        segments: list[dict],
        contexts: list[RetrievedChunk],
        *,
        tenant_id: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """ADR-017 §3.1 citation 객체 + citation_types 배열 생성.

        chat_logs.citation_types에 사용할 수 있도록 정렬된 type 배열도 반환.
        """
        out: list[dict[str, Any]] = []
        types: list[str] = []
        for seg in segments:
            if not seg["citations"]:
                continue
            for cidx in seg["citations"]:
                if not (1 <= cidx <= len(contexts)):
                    continue
                ctx = contexts[cidx - 1]
                meta: VerifiedCitation | None = (
                    seg.get("citation_meta") or {}
                ).get(cidx)
                out.append(
                    {
                        "citation_id": f"cite-{uuid4().hex[:12]}",
                        "marker": f"[{cidx}]",
                        "tenant_id": tenant_id,
                        "support_type": seg["support_type"],
                        "support_level": meta.support_level if meta else None,
                        "verified": meta.verified if meta else None,
                        "claim_text": seg["text"],
                        "doc_id": ctx.payload.get("doc_id"),
                        "chunk_id": ctx.chunk_id,
                        "title": ctx.title,
                        "page_number": ctx.page_number,
                        "section_title": ctx.section_title,
                        "excerpt": (ctx.content or "")[:200],
                        "score": ctx.fused_score,
                        "rerank_score": ctx.rerank_score,
                        "similarity": meta.similarity if meta else None,
                    }
                )
                types.append(seg["support_type"])
        return out, types

    @staticmethod
    def compute_confidence(
        *,
        segments: list[dict],
        citations: list[dict[str, Any]],
        contexts: list[RetrievedChunk],
        unsupported_ratio: float,
        thresholds: VerifierThresholds,
    ) -> float:
        """citation.yaml.confidence_weights 가중합. 모든 항 [0,1]."""
        # retrieval: top1 rerank_score(없으면 fused_score) 정규화 — 0~1로 가정
        if contexts:
            top1 = max(
                (
                    c.rerank_score if c.rerank_score is not None else c.fused_score
                )
                for c in contexts
            )
        else:
            top1 = 0.0
        retrieval = max(0.0, min(1.0, top1))

        marker_count = sum(len(seg["citations"]) for seg in segments)
        verified_count = sum(
            1 for c in citations if c.get("verified")
        )
        verified = (verified_count / marker_count) if marker_count else 0.0

        supported = max(0.0, 1.0 - unsupported_ratio)

        # coverage: segment 중 citation을 가진 비율
        seg_with_cite = sum(1 for seg in segments if seg["citations"])
        coverage = (seg_with_cite / len(segments)) if segments else 0.0

        score = (
            retrieval * thresholds.weight_retrieval
            + verified * thresholds.weight_verified
            + supported * thresholds.weight_supported
            + coverage * thresholds.weight_coverage
        )
        return max(0.0, min(1.0, score))

    @staticmethod
    def evaluate_gate_2(
        *,
        citations: list[dict[str, Any]],
        unsupported_ratio: float,
        confidence: float,
        thresholds: VerifierThresholds,
    ) -> tuple[bool, str | None]:
        verified_count = sum(1 for c in citations if c.get("verified"))
        if verified_count < thresholds.min_verified_count:
            return False, "below_min_verified_count"
        if unsupported_ratio > thresholds.max_unsupported_ratio:
            return False, "above_max_unsupported_ratio"
        if confidence < thresholds.min_confidence:
            return False, "below_min_confidence"
        return True, None

    # ----- 한 번에 전체 (편의) ------------------------------------------- #

    async def run(
        self,
        *,
        raw_segments: list[dict],
        contexts: list[RetrievedChunk],
        tenant_id: str,
        thresholds: VerifierThresholds,
        limitations: str | None = None,
    ) -> VerificationResult:
        segments = self.parse_segments(raw_segments)
        segments, t1_removed = self.tier1_filter(segments, len(contexts))
        segments, _assess, avg_sim = await self.tier2_classify(
            segments, contexts, thresholds
        )
        unsupported_idx, unsupported_ratio = self.tier3_unsupported(segments)

        citations, types = self.assemble_citations(
            segments, contexts, tenant_id=tenant_id
        )
        confidence = self.compute_confidence(
            segments=segments,
            citations=citations,
            contexts=contexts,
            unsupported_ratio=unsupported_ratio,
            thresholds=thresholds,
        )
        passed, reason = self.evaluate_gate_2(
            citations=citations,
            unsupported_ratio=unsupported_ratio,
            confidence=confidence,
            thresholds=thresholds,
        )

        # final_answer: segment text 결합 + 마커 부착 (간단형: text 그대로)
        final_answer = "".join(seg["text"] for seg in segments)

        # Inference/conflict 다운그레이드 segment가 있으면 limitations에 caveat
        downgraded = [
            seg for seg in segments if seg.get("downgraded")
        ]
        merged_limitations = limitations
        if downgraded:
            caveat = (
                "일부 답변은 추론·충돌 검증이 결선되지 않아 인용을 제거했습니다. "
                "담당 부서 확인을 권장합니다."
            )
            merged_limitations = (
                f"{limitations}\n{caveat}" if limitations else caveat
            )

        return VerificationResult(
            segments=segments,
            citations=citations,
            citation_types=types,
            unsupported_segment_indices=unsupported_idx,
            unsupported_ratio=unsupported_ratio,
            tier1_markers_removed=t1_removed,
            tier2_avg_similarity=avg_sim,
            confidence=confidence,
            gate2_passed=passed,
            gate2_reason=reason,
            final_answer=final_answer,
            limitations=merged_limitations,
        )
