"""LangGraph 노드 — chat_structured vertical slice (ADR-013 §9 일부).

각 노드는 service class 호출 wrapper (ADR-003 원칙). 본 모듈은 노드 함수와 deps
번들을 제공하고, rag_graph.build_rag_graph(deps)가 이를 그래프에 결선한다.

이번 슬라이스 노드:
  - tenant_resolver         — state 검증 (UserContext는 backend dep가 주입)
  - load_tenant_config      — TenantConfigService 호출 (또는 호환 callable)
  - build_acl_filter_node   — ADR-004 §1·§2·§3·§4 ACL 필터 dict 생성
  - retrieve_context        — RetrievalService.retrieve 호출
  - gate_1                  — citation.yaml gates.retrieval 평가 → 통과/fallback 분기
  - generate_answer         — GenerationService.generate_structured 호출
  - fallback                — Gate 1 미통과 시 "확인 불가" 응답 + near_misses

본 슬라이스에 포함되지 않는 노드(parse_response, verify_*, judge_inference,
mask_response_pii, gate_2, save_chat_log 등)는 이후 ADR-010·013·020 후속 작업에서
같은 deps 패턴으로 추가된다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..services.acl_builder import build_qdrant_acl_filter
from ..services.chat_log_writer import ChatLogPayload, ChatLogWriter
from ..services.conflict_detector import ConflictDetector
from ..services.generation_service import GenerationService
from ..services.judge_service import JudgeService
from ..services.model_router import ModelRouter
from ..services.pii_service import PIIService
from ..services.query_classifier import (
    ClassifierConfig,
    QueryClassifier,
)
from ..services.query_rewriter import QueryRewriter
from ..services.retrieval_service import RetrievalConfig, RetrievalService
from ..services.verifier_service import VerifierService, VerifierThresholds
from .rag_graph import RAGState

ConfigLoader = Callable[[str], Awaitable[dict[str, Any]] | dict[str, Any]]

logger = logging.getLogger(__name__)


@dataclass
class RAGGraphDeps:
    """build_rag_graph에 주입되는 service 번들.

    Args:
        retrieval_service: RetrievalService 인스턴스
        generation_service: GenerationService 인스턴스
        config_loader: domain_id → effective config dict. async or sync 모두 허용.
        today_provider: 유효 기간 ACL 필터에 사용할 today 제공자. None이면 date 미적용.
        verifier_service: ADR-010 Tier 1/2/3 + assemble + confidence + Gate 2.
                          chat_structured_full 그래프에서만 사용 (slice는 None 허용).
    """

    retrieval_service: RetrievalService
    generation_service: GenerationService
    config_loader: ConfigLoader
    today_provider: Callable[[], date] | None = None
    verifier_service: VerifierService | None = None
    judge_service: JudgeService | None = None
    chat_log_writer: ChatLogWriter | None = None
    query_classifier: QueryClassifier | None = None
    model_router: ModelRouter | None = None
    pii_service: PIIService | None = None
    conflict_detector: ConflictDetector | None = None
    query_rewriter: QueryRewriter | None = None
    # ADR-027 — 대화형 출제. 둘 다 주입돼야 채팅 출제 분기가 활성(없으면 일반 RAG로 폴백).
    # 순환 의존(assessment service가 rag_service를 참조)을 피하려고 late-binding 허용:
    # 그래프 빌드 후 deps에 세팅돼도 노드가 호출 시점에 읽는다.
    assessment_generate_service: Any = None
    assessment_item_repo: Any = None


async def _load_config(deps: RAGGraphDeps, domain_id: str) -> dict[str, Any]:
    result = deps.config_loader(domain_id)
    if hasattr(result, "__await__"):
        result = await result  # type: ignore[assignment]
    return dict(result or {})


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #


async def tenant_resolver_node(state: RAGState) -> dict[str, Any]:
    """state.domain_id / user_context 무결성만 검증. 인증·매핑은 backend AuthAdapter 책임."""
    if not state.domain_id:
        return {"error": "domain_id missing in state", "fallback_reason": "auth_error"}
    if state.user_context is None:
        return {"error": "user_context missing in state", "fallback_reason": "auth_error"}
    return {}


async def load_tenant_config_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    config = await _load_config(deps, state.domain_id)
    return {"tenant_config": config}


async def build_acl_filter_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    user = state.user_context or {}
    today = deps.today_provider() if deps.today_provider else None
    tenant_config = state.tenant_config or {}
    retrieval_cfg = (tenant_config.get("retrieval") or {}) if isinstance(tenant_config, dict) else {}
    # ADR-012 §3 — archived chunk는 default로 검색에서 제외. tenant가 admin 흐름에서
    # 명시적으로 retrieval.exclude_archived=false로 override하면 archive까지 조회 가능.
    exclude_archived = bool(retrieval_cfg.get("exclude_archived", True))
    acl_filter = build_qdrant_acl_filter(
        user_id=str(user.get("user_id", "")),
        clearance=str(user.get("clearance", "internal")),
        department=user.get("department"),
        domain_groups=list(user.get("domain_groups") or []),
        today=today,
        exclude_archived=exclude_archived,
    )
    return {"acl_filter": acl_filter}


async def classify_query_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    """ADR-013 §3 — Tier 1 정규식 + Tier 2 LLM 분류.

    deps.query_classifier None이면 default decision (document_qa/direct/medium).
    classifier_decision dict는 chat_logs.classifier_decision에 그대로 적재.
    """
    if deps.query_classifier is None:
        return {
            "query_type": "document_qa",
            "support_type": "direct",
            "complexity": "medium",
            "classifier_decision": {
                "query_type": "document_qa",
                "support_type": "direct",
                "complexity": "medium",
                "tier1_matched": None,
                "tier2_called": False,
                "skipped": True,
            },
        }
    classifier_config = ClassifierConfig.from_dict(
        (state.tenant_config or {}).get("query_classifier")
    )
    result = await deps.query_classifier.classify(
        question=state.question, config=classifier_config
    )
    return {
        "query_type": result.query_type,
        "support_type": result.support_type,
        "complexity": result.complexity,
        "classifier_decision": result.to_log_dict(),
    }


async def model_router_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    """ADR-013 §1·§2 — routing.yaml 룰 평가.

    deps.model_router None이면 default 라우팅 (tenant_slm + use_lora=True + use_rag=True).
    matched_signals + matched_rule을 chat_logs.routing_decision에 적재.
    """
    if deps.model_router is None:
        return {
            "matched_rule": "default_skipped",
            "selected_model": "tenant_slm",
            "use_rag": True,
            "ui_mode": "chat_structured",
        }
    from ..services.query_classifier import ClassificationResult
    cls = ClassificationResult(
        query_type=state.query_type or "document_qa",
        support_type=state.support_type,
        complexity=state.complexity,
    )
    routing_cfg = (state.tenant_config or {}).get("routing")
    tenant_model_cfg = (state.tenant_config or {}).get("model") or {}
    decision = deps.model_router.decide(
        classification=cls,
        routing_config=routing_cfg,
        tenant_model_config=tenant_model_cfg,
    )
    out: dict[str, Any] = {
        "matched_rule": decision.matched_rule,
        "selected_model": decision.model,
        "selected_lora": decision.lora_adapter,
        "use_rag": decision.use_rag,
        "ui_mode": decision.ui_mode,
    }
    # action: fallback_refusal 등 — 본 라우팅이 모델 호출 자체를 skip해야 한다는 신호.
    # ADR-023: query_type=free_chat의 streaming redirect는 폐기됐다. 근거 유무는
    # Gate 1이 분기하며(grounded/ungrounded), 라우팅은 ui_mode로 경로를 바꾸지 않는다.
    if decision.action:
        out["fallback_reason"] = f"routing_{decision.action}"
    return out


def _retrieval_config_from_tenant(config: dict[str, Any]) -> RetrievalConfig:
    retrieval = config.get("retrieval") or {}
    top_k = retrieval.get("top_k") or {}
    reranker = retrieval.get("reranker") or {}
    return RetrievalConfig(
        fused_top_k=int(top_k.get("fused", 50)),
        rerank_top_k=int(top_k.get("rerank", 10)),
        context_top_k=int(top_k.get("context", 5)),
        reranker_bypass=bool(reranker.get("bypass", False)),
    )


async def query_rewrite_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    """ADR-011 §5 — tenant_config.retrieval.query_rewriting.enable=true 시 LLM rewrite.

    deps.query_rewriter None이면 no-op (state.rewritten_query는 빈 채로 두고 retrieve가
    state.question을 사용한다). 실패 시 graceful — 원 question을 그대로 통과.
    """
    if deps.query_rewriter is None:
        return {}
    rewrite_cfg = (state.tenant_config or {}).get("retrieval", {}).get(
        "query_rewriting"
    )
    result = await deps.query_rewriter.rewrite(state.question, rewrite_cfg)
    return {"rewritten_query": result.rewritten_query}


async def retrieve_context_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    cfg = _retrieval_config_from_tenant(state.tenant_config or {})
    # ADR-011 §5 — query_rewrite 노드가 채운 rewritten_query를 우선 사용 (없으면 원 question)
    query = state.rewritten_query or state.question
    try:
        result = await deps.retrieval_service.retrieve(
            domain_id=state.domain_id,
            question=query,
            acl_filter=state.acl_filter or {},
            config=cfg,
        )
    except Exception as exc:  # noqa: BLE001
        # ADR-023 §3 — 검색 인프라(임베더·리랭커·벡터스토어) 장애를 500으로 흘리지 않는다.
        # 빈 결과로 강등 → Gate 1 미통과 → ungrounded 경로(LLM 일반 대화 + '근거 없음' 배지).
        # 장애 자체는 logging + retrieval_error(state)로 관측. 임베더 다운 같은 인프라 이슈는
        # 사용자 경험을 깨지 않고 별도 모니터링으로 처리한다.
        logger.warning(
            "retrieve_context failed (%s: %s) — degrading to ungrounded path",
            type(exc).__name__, exc,
        )
        return {
            "retrieved_chunks": [],
            "reranked_chunks": [],
            "final_contexts": [],
            "retrieval_error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "retrieved_chunks": [_chunk_to_dict(c) for c in result.fused],
        "reranked_chunks": [_chunk_to_dict(c) for c in result.reranked],
        "final_contexts": [_chunk_to_dict(c) for c in result.contexts],
    }


def _chunk_to_dict(c) -> dict[str, Any]:
    return {
        "chunk_id": c.chunk_id,
        "doc_id": c.doc_id,
        "title": c.title,
        "content": c.content,
        "page_number": c.page_number,
        "section_title": c.section_title,
        "fused_score": c.fused_score,
        "rerank_score": c.rerank_score,
        "payload": c.payload,
    }


def _gate1_thresholds(config: dict[str, Any]) -> tuple[float, int, float]:
    """citation.yaml gates.retrieval 추출. 누락 시 ADR-010 §4 기본값."""
    citation = config.get("citation") or {}
    gates = citation.get("gates") or {}
    retrieval = gates.get("retrieval") or {}
    return (
        float(retrieval.get("min_top1_rerank", 0.6)),
        int(retrieval.get("min_strong_chunks", 2)),
        float(retrieval.get("strong_chunk_threshold", 0.5)),
    )


async def gate_1_node(state: RAGState) -> dict[str, Any]:
    """ADR-010 §4 Gate 1 — retrieval 결과의 품질을 검사 후 분기 신호 기록."""
    chunks = state.reranked_chunks or state.final_contexts or []
    min_top1, min_strong, strong_thr = _gate1_thresholds(state.tenant_config or {})

    if not chunks:
        return {
            "gate1_passed": False,
            "gate1_metrics": {"reason": "empty_retrieval"},
            "fallback_reason": "retrieval_unavailable",
            "near_misses": [],
        }

    def _score(c: dict) -> float:
        rs = c.get("rerank_score")
        return float(rs if rs is not None else c.get("fused_score") or 0.0)

    scores = [_score(c) for c in chunks]
    top1 = max(scores) if scores else 0.0
    strong_count = sum(1 for s in scores if s >= strong_thr)
    metrics = {
        "top1_score": top1,
        "strong_count": strong_count,
        "min_top1_threshold": min_top1,
        "min_strong_threshold": min_strong,
        "strong_chunk_threshold": strong_thr,
    }
    passed = top1 >= min_top1 and strong_count >= min_strong
    out: dict[str, Any] = {"gate1_passed": passed, "gate1_metrics": metrics}
    if not passed:
        out["fallback_reason"] = "gate1_failed"
        # ADR-010 fallback near_misses: top-3 chunks 그대로 노출 (사용자 힌트용)
        out["near_misses"] = chunks[:3]
    return out


def gate_1_router(state: RAGState) -> str:
    """conditional edge — pass → generate, fail → fallback."""
    return "generate_answer" if state.gate1_passed else "fallback"


async def generate_answer_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    """ADR-013 §7 fallback chain 실행.

    chain (configs/platform/model.yaml.fallback_chain):
      1. tenant_slm + selected_lora           (router 결정)
      2. tenant_slm_no_lora (lora_adapter=None) — same model, LoRA off
      3. shared_llm (다른 endpoint)
      4. 전부 실패 → fallback_reason=low_generation_quality

    각 단계 실패(예외 또는 parse_error)는 model_failure_chain에 누적된다.
    """
    contexts = [
        _dict_to_chunk(c) for c in (state.final_contexts or [])
    ]
    failure_chain: list[dict[str, Any]] = list(state.model_failure_chain or [])

    # chain 단계 정의 — selected_model/lora는 router 결과를 기본으로
    selected_model = state.selected_model or "tenant_slm"
    selected_lora = state.selected_lora
    steps: list[dict[str, Any]] = [
        {"model": selected_model, "lora": selected_lora, "label": "primary"},
    ]
    # selected_model이 tenant_slm이고 LoRA가 활성일 때만 no_lora 단계
    if selected_model == "tenant_slm" and selected_lora:
        steps.append({"model": "tenant_slm", "lora": None, "label": "tenant_slm_no_lora"})
    # shared_llm fallback — primary가 shared_llm이 아니라면
    if selected_model != "shared_llm":
        steps.append({"model": "shared_llm", "lora": None, "label": "shared_llm"})

    last_result = None
    for step in steps:
        try:
            result = await deps.generation_service.generate_structured(
                question=state.question,
                contexts=contexts,
                lora_adapter=step["lora"],
                domain_id=state.domain_id,
                model_override=step["model"],
            )
            last_result = result
            if result.parse_ok and result.answer_segments:
                # 성공 — failure_chain은 이전 단계 실패 목록만
                return {
                    "raw_response": result.raw_response,
                    "answer_segments": result.answer_segments,
                    "limitations": result.limitations,
                    "selected_model": step["model"],
                    "selected_lora": step["lora"],
                    "model_failure_chain": failure_chain,
                }
            # 파싱 실패 → 다음 단계
            failure_chain.append(
                {
                    "model": step["model"],
                    "lora": step["lora"],
                    "label": step["label"],
                    "reason": "parse_error",
                    "detail": result.parse_error,
                }
            )
        except Exception as exc:  # noqa: BLE001
            failure_chain.append(
                {
                    "model": step["model"],
                    "lora": step["lora"],
                    "label": step["label"],
                    "reason": type(exc).__name__,
                    "detail": str(exc),
                }
            )

    # chain 전체 실패 — ADR-010 fallback 응답 (Gate 2가 처리)
    return {
        "raw_response": last_result.raw_response if last_result else "",
        "answer_segments": [],
        "limitations": None,
        "fallback_reason": "low_generation_quality",
        "error": "all_models_failed",
        "model_failure_chain": failure_chain,
    }


async def generate_ungrounded_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    """ADR-023 §3 — Gate 1 미통과(근거 약/없음) 시 일반 대화형 답변.

    citable context 없이 GenerationService.generate_conversational을 호출해 자유
    텍스트 답변을 만든다. 인용 없음(citations=[]), verify/judge skip. grounding=
    'ungrounded'로 표시하고 Gate 1이 세팅한 fallback_reason은 해제한다 — 이것은
    거부(fallback)가 아니라 정상 success 응답이며, "근거 없음"은 UI 배지로 구분한다.

    대화형 생성마저 실패하면 그때는 진짜 fallback(low_generation_quality)으로 떨어진다.
    """
    selected_model = state.selected_model or "tenant_slm"
    selected_lora = state.selected_lora
    failure_chain = list(state.model_failure_chain or [])
    try:
        text = await deps.generation_service.generate_conversational(
            question=state.question,
            lora_adapter=selected_lora,
            domain_id=state.domain_id,
            model_override=selected_model,
        )
    except Exception as exc:  # noqa: BLE001 — graceful: 진짜 fallback으로 전환
        failure_chain.append(
            {
                "model": selected_model,
                "lora": selected_lora,
                "label": "ungrounded",
                "reason": type(exc).__name__,
                "detail": str(exc),
            }
        )
        return {
            "grounding": "ungrounded",
            "answer_segments": [],
            "final_answer": "",
            "citations": [],
            "citation_types": [],
            "confidence": 0.0,
            "fallback_reason": "low_generation_quality",
            "error": "ungrounded_generation_failed",
            "model_failure_chain": failure_chain,
        }

    body = (text or "").strip()
    return {
        "grounding": "ungrounded",
        "raw_response": text or "",
        "answer_segments": [{"text": body, "citations": []}],
        "final_answer": body,
        "citations": [],
        "citation_types": [],
        "confidence": 0.0,
        # Gate 1이 세팅한 retrieval_unavailable/gate1_failed를 해제 — success 응답.
        "fallback_reason": None,
        "model_failure_chain": failure_chain,
    }


# --------------------------------------------------------------------------- #
# ADR-027 — 대화형 근거기반 출제
# --------------------------------------------------------------------------- #

# 한국어 출제 의도에서 과목명을 영문 subject 키로 매핑하는 보조 별칭. 매칭 실패해도
# '과목별' 분배로 동작하므로 starter 수준이면 충분(향후 configs로 이전 가능).
_SUBJECT_ALIASES: dict[str, list[str]] = {
    "database": ["데이터베이스", "db", "디비"],
    "operating_system": ["운영체제", "os"],
    "software_engineering": ["소프트웨어공학", "소공"],
    "computer_structure": ["컴퓨터구조", "전산구조"],
    "data_communication": ["데이터통신", "통신", "네트워크"],
    "programming": ["프로그래밍", "프로그램언어", "언어"],
    "software_dev": ["소프트웨어개발", "개발"],
    "software_design": ["소프트웨어설계", "설계"],
    "info_system": ["정보시스템", "시스템관리"],
    "security": ["정보보안", "보안"],
}

_ALL_SUBJECTS_HINTS = ("과목별", "각 과목", "전 과목", "모든 과목", "과목마다", "모의고사")


def _assessment_defaults(tenant_config: dict | None) -> dict[str, int]:
    cfg = ((tenant_config or {}).get("assessment") or {}) if isinstance(tenant_config, dict) else {}
    chat = cfg.get("chat") if isinstance(cfg, dict) else {}
    chat = chat if isinstance(chat, dict) else {}
    return {
        "default_count": int(chat.get("default_count", 3)),
        "max_total": int(chat.get("max_total", 20)),
        "max_subjects": int(chat.get("max_subjects", 6)),
    }


def _plan_assessment(
    question: str, subjects: list[str], defaults: dict[str, int]
) -> list[dict[str, Any]]:
    """ADR-027 §3 — 자연어에서 출제 계획 추출(휴리스틱, 결정적).

    반환: [{subject, count, difficulty}] (subject는 영문 키). 근거 과목이 없으면 [].
    """
    q = question or ""
    # 개수
    m = re.search(r"(\d+)\s*(문제|문항|개|題)", q)
    count = int(m.group(1)) if m else defaults["default_count"]
    count = max(1, min(count, defaults["max_total"]))
    # 난이도
    if any(w in q for w in ("어려운", "어렵게", "상", "hard")):
        difficulty = "hard"
    elif any(w in q for w in ("쉬운", "쉽게", "하", "easy")):
        difficulty = "easy"
    else:
        difficulty = "medium"

    if not subjects:
        return []

    # 명시된 특정 과목 매칭(영문 키 또는 한국어 별칭)
    named: list[str] = []
    ql = q.lower()
    for subj in subjects:
        key_tokens = [subj.lower(), subj.replace("_", " ").lower()]
        aliases = _SUBJECT_ALIASES.get(subj, [])
        if any(t and t in ql for t in key_tokens) or any(a in q for a in aliases):
            named.append(subj)

    per_subject = any(h in q for h in _ALL_SUBJECTS_HINTS)

    if named and not per_subject:
        # 특정 과목 지정 → 각 과목 count개 (다과목 cap)
        chosen = named[: defaults["max_subjects"]]
        return [{"subject": s, "count": count, "difficulty": difficulty} for s in chosen]

    if per_subject:
        # 과목별 N문제씩 → 전 과목(또는 named 한정), 총량 cap으로 과목 수 제한
        pool = named or subjects
        max_subj = max(1, min(defaults["max_subjects"], defaults["max_total"] // count))
        chosen = pool[:max_subj]
        return [{"subject": s, "count": count, "difficulty": difficulty} for s in chosen]

    # 과목 미지정 → 상위 과목 1개에서 count개
    return [{"subject": subjects[0], "count": count, "difficulty": difficulty}]


def _assessment_item_to_dict(item: Any) -> dict[str, Any]:
    return {
        "subject": getattr(item, "subject", None),
        "difficulty": getattr(item, "difficulty", None),
        "question_text": getattr(item, "question_text", ""),
        "choices": list(getattr(item, "choices", []) or []),
        "answer": getattr(item, "answer", ""),
        "explanation": getattr(item, "explanation", None),
    }


def _format_assessment_answer(
    items: list[dict[str, Any]], plan: list[dict[str, Any]]
) -> str:
    if not items:
        return (
            "요청하신 과목에 승인된 문항이 부족해 지금은 출제할 수 없습니다. "
            "관리자가 해당 과목 문항을 검수·승인한 뒤 다시 시도해 주세요."
        )
    lines: list[str] = []
    subjects = sorted({str(p["subject"]) for p in plan})
    lines.append(f"문제은행을 근거로 {len(items)}문제를 출제했습니다 (과목: {', '.join(subjects)}).\n")
    for i, it in enumerate(items, start=1):
        subj = it.get("subject") or ""
        lines.append(f"**{i}. ({subj}) {it['question_text']}**")
        for ci, choice in enumerate(it.get("choices") or []):
            c = str(choice).strip()
            # 보기에 이미 'A.'/'A)' 라벨이 있으면 그대로, 없으면 라벨 부여(중복 방지).
            if re.match(r"^[A-Da-d1-4][.)]\s", c):
                lines.append(f"   {c}")
            else:
                lines.append(f"   {chr(65 + ci)}. {c}")
        lines.append("")
    lines.append("---\n**정답 및 해설**")
    for i, it in enumerate(items, start=1):
        ans = it.get("answer") or ""
        exp = it.get("explanation") or ""
        lines.append(f"{i}. 정답: {ans}" + (f" — {exp}" if exp else ""))
    return "\n".join(lines)


async def assessment_node(state: RAGState, deps: RAGGraphDeps) -> dict[str, Any]:
    """ADR-027 — 대화형 근거기반 출제.

    문제은행(approved)을 근거로 새 문항을 생성(persist=False, ephemeral)하고 대화형으로
    제시한다. 서비스 미주입·근거 없음이면 graceful degrade(ungrounded 안내). citation
    검증은 우회하며 grounding='assessment'로 표시한다.
    """
    svc = deps.assessment_generate_service
    if svc is None:
        msg = "출제 기능이 현재 비활성화되어 있습니다."
        return {
            "grounding": "ungrounded",
            "final_answer": msg,
            "answer_segments": [{"text": msg, "citations": []}],
            "citations": [], "citation_types": [], "confidence": 0.0,
            "fallback_reason": None,
        }

    # 가용 과목 수집(근거 grounding — 존재하는 과목으로만 계획)
    subjects: list[str] = []
    if deps.assessment_item_repo is not None:
        try:
            summary = await deps.assessment_item_repo.analytics_summary(
                domain_id=state.domain_id
            )
            by_subject = summary.get("by_subject") or {}
            subjects = [
                s for s, _ in sorted(
                    by_subject.items(), key=lambda kv: kv[1], reverse=True
                )
            ]
        except Exception:  # noqa: BLE001 — 과목 수집 실패는 빈 계획으로 degrade
            subjects = []

    defaults = _assessment_defaults(state.tenant_config)
    plan = _plan_assessment(state.question, subjects, defaults)

    from ..services.assessment_generate import GenerateCriteria

    async def _gen_one(entry: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            res = await svc.generate(
                domain_id=state.domain_id,
                criteria=GenerateCriteria(
                    subject=entry["subject"],
                    difficulty=entry["difficulty"],
                    count=int(entry["count"]),
                ),
                persist=False,
            )
        except Exception:  # noqa: BLE001 — 단일 과목 생성 실패는 건너뛴다
            return []
        return [_assessment_item_to_dict(it) for it in res.items]

    # 과목별 generate를 병렬 실행 — vLLM continuous batching을 활용해 wall-clock을
    # 순차 합산 대신 최댓값 수준으로 줄인다(다과목 출제의 nginx 504 timeout 방지).
    # 계획이 이미 max_subjects/max_total로 제한돼 동시 부하는 제한적이다.
    per_subject = await asyncio.gather(*[_gen_one(e) for e in plan])
    items: list[dict[str, Any]] = [
        it for sub in per_subject for it in sub
    ][: defaults["max_total"]]

    answer = _format_assessment_answer(items, plan)
    return {
        "grounding": "assessment",
        "assessment_items": items,
        "assessment_plan": plan,
        "final_answer": answer,
        "answer_segments": [{"text": answer, "citations": []}],
        "citations": [], "citation_types": [], "confidence": 0.0,
        "fallback_reason": None,
    }


def assessment_intent_router(state: RAGState) -> str:
    """ADR-027 §1 — model_router 후 출제 의도 액션 분기.

    query_type=assessment_generation이면 assessment 노드로, 아니면 일반 RAG
    (query_rewrite)로. 출제는 검색을 끄는 게 아니라 근거원을 문제은행으로 바꾼다.
    """
    return "assessment" if state.query_type == "assessment_generation" else "query_rewrite"


def gate_1_full_router(state: RAGState) -> str:
    """ADR-023 §3 — full 그래프 Gate 1 분기.

    pass → grounded 생성(generate_answer), fail → ungrounded 대화(generate_ungrounded).
    (slice 그래프는 기존 gate_1_router를 그대로 쓴다 — fail 시 fallback.)
    """
    return "generate_answer" if state.gate1_passed else "generate_ungrounded"


def post_mask_grounding_router(state: RAGState) -> str:
    """ADR-023 §3 — mask_response_pii 이후 grounding으로 합류점 분기.

    ungrounded는 verify/Gate 2(citation 게이트)를 건너뛰고 바로 저장한다 —
    인용이 없어 Gate 2가 fallback으로 오판하는 것을 막는다. ADR-027 출제(assessment)도
    citation 검증 대상이 아니므로 동일하게 바로 저장한다.
    """
    return (
        "compute_confidence"
        if state.grounding == "grounded"
        else "save_chat_log"
    )


def _dict_to_chunk(d: dict[str, Any]):
    """final_contexts dict → RetrievedChunk 역변환 (GenerationService 입력용)."""
    from ..interfaces.retriever import RetrievedChunk

    return RetrievedChunk(
        chunk_id=str(d.get("chunk_id", "")),
        doc_id=str(d.get("doc_id", "")),
        title=str(d.get("title", "")),
        content=str(d.get("content", "")),
        page_number=d.get("page_number"),
        section_title=d.get("section_title"),
        dense_score=0.0,
        sparse_score=0.0,
        fused_score=float(d.get("fused_score") or 0.0),
        payload=dict(d.get("payload") or {}),
        rerank_score=d.get("rerank_score"),
    )


async def save_chat_log_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    """ADR-013 §10 + ADR-019 chat_logs INSERT.

    fallback / success 모든 경로의 마지막 노드. deps.chat_log_writer None이면 no-op
    (테스트·DB-less 환경 지원). conversation_id가 자동 발급되면 state에 갱신.
    """
    if deps.chat_log_writer is None:
        return {}

    final_contexts = state.final_contexts or []
    # excerpt-only retrieved view — payload bloat 방지 + audit truth 보존
    retrieved_excerpts = [
        {
            "chunk_id": c.get("chunk_id"),
            "doc_id": c.get("doc_id"),
            "title": c.get("title"),
            "page_number": c.get("page_number"),
            "section_title": c.get("section_title"),
            "fused_score": c.get("fused_score"),
            "rerank_score": c.get("rerank_score"),
            "content": c.get("content"),  # ADR-원칙 10: chunk lifecycle과 무관하게 보존
        }
        for c in final_contexts
    ]

    # ADR-020 §4 — pii_storage_policy=mask면 마스킹된 form 보관, plain이면 원문.
    storage_question = state.question_for_storage or state.question
    payload = ChatLogPayload(
        domain_id=state.domain_id,
        request_id=state.request_id,
        user_id=state.user_id,
        conversation_id=state.conversation_id,
        question=storage_question,
        answer=state.final_answer or "",
        retrieved_chunks=retrieved_excerpts,
        citations=list(state.citations or []),
        citation_types=list(state.citation_types or []),
        rewritten_query=state.rewritten_query or None,
        llm_model=state.selected_model,
        lora_adapter=state.selected_lora,
        ui_mode=state.ui_mode,
        confidence=state.confidence,
        fallback_reason=state.fallback_reason,
        unsupported_ratio=state.unsupported_ratio,
        verifier_metrics=dict(state.verifier_metrics or {}),
        routing_decision={
            "matched_rule": state.matched_rule,
            "selected_model": state.selected_model,
            "selected_lora": state.selected_lora,
            "use_rag": state.use_rag,
            "ui_mode": state.ui_mode,
            "grounding": state.grounding,  # ADR-023 §4
            "retrieval_error": state.retrieval_error,  # ADR-023 §3 (None=정상)
        },
        classifier_decision=dict(state.classifier_decision or {}),
        model_failure_chain=list(state.model_failure_chain or []),
        inference_judge_results=list(state.inference_judge_results or []),
        conflict_groups=list(state.conflict_groups or []),
        input_pii_found=list(state.input_pii_found or []),
        output_pii_masked=list(state.output_pii_masked or []),
        pii_storage_policy=state.pii_storage_policy or "mask",
        latency_ms=state.latency_ms or None,
    )

    conversation_id = await deps.chat_log_writer.write(payload)
    return {"conversation_id": conversation_id}


_FALLBACK_DEFAULT_TEXT = "현재 등록된 문서에서 충분한 근거를 찾지 못했습니다."
_FALLBACK_INPUT_PII_TEXT = (
    "개인정보로 보이는 정보가 포함되어 있습니다. "
    "민감한 정보를 제거하고 다시 시도해 주세요."
)


async def fallback_node(state: RAGState) -> dict[str, Any]:
    """Gate 1·2 미통과·생성 실패·routing 결정 등 fallback 응답을 채운다.

    - input_pii_blocked: ADR-020 §3 사용자 안내
    - 그 외: ADR-010 일반 fallback (생성 chain 전체 실패 등)

    ADR-023: 근거 미확보(Gate 1 fail)는 더 이상 fallback(거부)이 아니라 ungrounded
    대화 경로로 처리된다. 본 노드는 PII 차단·생성 실패 등 진짜 거부 사유만 담당한다.
    """
    reason = state.fallback_reason or "unknown"
    if reason == "input_pii_blocked":
        text = _FALLBACK_INPUT_PII_TEXT
        support = "input_pii_blocked"
    else:
        text = _FALLBACK_DEFAULT_TEXT
        support = "fallback"
    return {
        "answer_segments": [
            {"text": text, "citations": [], "support_type": support}
        ],
        "limitations": f"fallback_reason={reason}",
        "final_answer": text,
        "confidence": 0.0,
    }


# --------------------------------------------------------------------------- #
# PII 노드 (ADR-020 §3·§6)
# --------------------------------------------------------------------------- #


async def check_input_pii_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    """ADR-020 §3·§4 Layer 1·2 — 입력 PII 감지 + 정책 적용 + chat_logs 마스킹 결정.

    deps.pii_service None이면 no-op (테스트·dev 환경 호환).
    Layer 1: block 시 input_blocked=True + fallback_reason='input_pii_blocked'.
    Layer 2: storage 정책(mask|plain)을 평가하여 question_for_storage 채움 — state.question은
             retrieval/LLM에 raw로 전달, save_chat_log만 마스킹된 form 사용.
             compliance_mode=gdpr_strict/hipaa_strict면 mask 강제(ADR-020 §10).
    """
    if deps.pii_service is None:
        return {}
    tenant_config = state.tenant_config or {}
    pii_cfg = tenant_config.get("pii")
    result = deps.pii_service.check_input(state.question, pii_cfg)
    out: dict[str, Any] = {"input_pii_found": result.findings}

    # Layer 2 — block 여부와 무관하게 storage 정책 결정 (block 응답도 chat_logs에 마스킹 보관)
    # ADR-020 §4: configured=plain 이라도 platform_admin 승인(`pii.storage.plain_approved`)
    # 이 없으면 PIIService가 mask로 fallback한다 (backend config_loader가 주입).
    plain_approved = bool(
        ((pii_cfg or {}).get("storage") or {}).get("plain_approved", False)
    )
    storage = deps.pii_service.mask_for_storage(
        state.question,
        pii_cfg,
        compliance_mode=str(tenant_config.get("compliance_mode") or "standard"),
        plain_approved=plain_approved,
    )
    out["pii_storage_policy"] = storage.policy
    if storage.policy == "mask":
        out["question_for_storage"] = storage.text

    if result.blocked:
        out["input_blocked"] = True
        out["fallback_reason"] = "input_pii_blocked"
        out["blocked_categories"] = result.blocked_categories
    return out


def check_input_pii_router(state: RAGState) -> str:
    """blocked → fallback, 그 외 → build_acl_filter."""
    return "fallback" if state.input_blocked else "build_acl_filter"


async def mask_response_pii_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    """ADR-020 §6 Layer 4 — final_answer PII 마스킹.

    deps.pii_service None이거나 response.enable=false / final_answer 비어있으면 no-op.
    마스킹 발생 시 final_answer 갱신 + output_pii_masked 적재 (chat_logs 보존).
    """
    if deps.pii_service is None or not state.final_answer:
        return {}
    pii_cfg = (state.tenant_config or {}).get("pii")
    result = deps.pii_service.mask_output(state.final_answer, pii_cfg)
    if not result.findings:
        return {}
    return {
        "final_answer": result.masked_text,
        "output_pii_masked": result.findings,
    }


# --------------------------------------------------------------------------- #
# Verifier 단계 노드 (ADR-010 §5)
# --------------------------------------------------------------------------- #


def _thresholds_from_state(state: RAGState) -> VerifierThresholds:
    config = state.tenant_config or {}
    return VerifierThresholds.from_config(config.get("citation"))


def _final_contexts_as_chunks(state: RAGState):
    return [_dict_to_chunk(c) for c in (state.final_contexts or [])]


async def parse_response_node(state: RAGState) -> dict[str, Any]:
    """LLM raw answer_segments를 검증 파이프라인이 사용하는 정규 dict로 변환."""
    segs = VerifierService.parse_segments(state.answer_segments or [])
    return {"answer_segments": segs}


async def verify_tier1_node(state: RAGState) -> dict[str, Any]:
    segs, removed = VerifierService.tier1_filter(
        state.answer_segments or [], num_contexts=len(state.final_contexts or [])
    )
    metrics = dict(state.verifier_metrics or {})
    metrics["tier1_markers_removed"] = removed
    return {"answer_segments": segs, "verifier_metrics": metrics}


async def detect_conflict_heuristic_node(
    state: RAGState, deps: RAGGraphDeps
) -> dict[str, Any]:
    """ADR-010 §7 secondary — date/numeric/rule_id 차이로 conflict 후보 마킹.

    deps.conflict_detector None이거나 enabled=False면 no-op.
    LLM Primary가 이미 type=conflict로 마킹한 segment는 스킵 (Detector 내부에서 처리).
    detection 발생 시 segment.support_type='conflict' + conflict_groups 채워서
    Tier 2 conflict 분기가 양측 검증을 수행한다.
    chat_logs.conflict_groups에는 segment_index별 그룹과 signal을 누적.
    """
    # tenant_config.citation에 conflict_detection 정책이 있으면 그걸 우선
    # (Y3: tenant overrides 우선). 없으면 deps default 사용.
    citation_cfg = (state.tenant_config or {}).get("citation")
    detector = (
        ConflictDetector.from_config(citation_cfg)
        if citation_cfg
        else deps.conflict_detector
    )
    if detector is None or not detector.enabled:
        return {}
    contexts = _final_contexts_as_chunks(state)
    if not contexts:
        return {}
    new_segs: list[dict[str, Any]] = []
    accumulated: list[dict[str, Any]] = list(state.conflict_groups or [])
    metrics = dict(state.verifier_metrics or {})
    detected_count = 0
    for seg in state.answer_segments or []:
        result = detector.detect_in_segment(seg, contexts)
        if not result.is_conflict:
            new_segs.append(seg)
            continue
        ns = dict(seg)
        ns["support_type"] = "conflict"
        ns["conflict_groups"] = result.conflict_groups
        ns["conflict_signal"] = result.signal
        new_segs.append(ns)
        detected_count += 1
        accumulated.append(
            {
                "segment_index": seg.get("index"),
                "signal": result.signal,
                "groups": result.conflict_groups,
                "details": result.details,
                "source": "heuristic",
            }
        )
    metrics["conflict_heuristic_detected"] = detected_count
    return {
        "answer_segments": new_segs,
        "conflict_groups": accumulated,
        "verifier_metrics": metrics,
    }


async def verify_tier2_node(state: RAGState, deps: RAGGraphDeps) -> dict[str, Any]:
    if deps.verifier_service is None:
        return {}
    thresholds = _thresholds_from_state(state)
    contexts = _final_contexts_as_chunks(state)
    segs, _assess, avg = await deps.verifier_service.tier2_classify(
        state.answer_segments or [], contexts, thresholds
    )
    metrics = dict(state.verifier_metrics or {})
    metrics["tier2_avg_similarity"] = avg
    return {"answer_segments": segs, "verifier_metrics": metrics}


async def judge_inference_node(state: RAGState, deps: RAGGraphDeps) -> dict[str, Any]:
    """ADR-010 §4·§5 — pending_judge=True segment를 LLM-as-judge로 확정.

    deps.judge_service None이면 no-op (inference는 verify_tier2에서 이미 다운그레이드됨).
    통과 시 citation_meta의 support_level이 medium으로 cap되고 verified 상태 유지.
    실패 시 citations 비움 + downgrade_reason="judge_rejected".
    """
    if deps.judge_service is None:
        return {}

    segs = list(state.answer_segments or [])
    contexts = _final_contexts_as_chunks(state)
    judge_results: list[dict[str, Any]] = list(state.inference_judge_results or [])
    min_confidence = deps.judge_service.min_confidence

    new_segs: list[dict[str, Any]] = []
    for seg in segs:
        if not seg.get("pending_judge"):
            new_segs.append(seg)
            continue
        # cited chunks 수집
        cited_chunks = [
            contexts[c - 1] for c in seg.get("citations") or []
            if 1 <= c <= len(contexts)
        ]
        result = await deps.judge_service.judge(
            claim_text=seg.get("text", ""),
            cited_chunks=cited_chunks,
            inference_chain=seg.get("inference_chain"),
        )
        passes = result.passes(min_confidence) if result.parse_ok else False
        new_segs.append(
            VerifierService.apply_judge_result(
                seg,
                judge_passes=passes,
                reasoning=result.reasoning,
                caveat=result.caveat,
            )
        )
        judge_results.append(
            {
                "segment_index": seg.get("index"),
                "valid": result.valid,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
                "caveat": result.caveat,
                "parse_ok": result.parse_ok,
            }
        )

    return {
        "answer_segments": new_segs,
        "inference_judge_results": judge_results,
    }


async def detect_unsupported_node(state: RAGState) -> dict[str, Any]:
    idxs, ratio = VerifierService.tier3_unsupported(state.answer_segments or [])
    metrics = dict(state.verifier_metrics or {})
    metrics["tier3_unsupported_segments"] = idxs
    return {"unsupported_ratio": ratio, "verifier_metrics": metrics}


async def assemble_response_node(state: RAGState) -> dict[str, Any]:
    contexts = _final_contexts_as_chunks(state)
    citations, types = VerifierService.assemble_citations(
        state.answer_segments or [], contexts, domain_id=state.domain_id
    )
    final_answer = "".join(seg.get("text", "") for seg in state.answer_segments or [])
    # inference/conflict 다운그레이드 segment가 있으면 limitations에 caveat 추가 (ADR-010 §5)
    downgraded = [
        seg for seg in (state.answer_segments or []) if seg.get("downgraded")
    ]
    limitations = state.limitations
    if downgraded:
        caveat = (
            "일부 답변은 추론·충돌 검증이 결선되지 않아 인용을 제거했습니다. "
            "담당 부서 확인을 권장합니다."
        )
        limitations = f"{limitations}\n{caveat}" if limitations else caveat
    return {
        "citations": citations,
        "citation_types": types,
        "final_answer": final_answer,
        "limitations": limitations,
    }


async def compute_confidence_node(state: RAGState) -> dict[str, Any]:
    thresholds = _thresholds_from_state(state)
    contexts = _final_contexts_as_chunks(state)
    score = VerifierService.compute_confidence(
        segments=state.answer_segments or [],
        citations=state.citations or [],
        contexts=contexts,
        unsupported_ratio=state.unsupported_ratio,
        thresholds=thresholds,
    )
    return {"confidence": score}


async def gate_2_node(state: RAGState) -> dict[str, Any]:
    thresholds = _thresholds_from_state(state)
    passed, reason = VerifierService.evaluate_gate_2(
        citations=state.citations or [],
        unsupported_ratio=state.unsupported_ratio,
        confidence=state.confidence,
        thresholds=thresholds,
    )
    out: dict[str, Any] = {"gate2_passed": passed}
    if not passed:
        out["fallback_reason"] = f"gate2_{reason}" if reason else "gate2_failed"
    return out


def gate_2_router(state: RAGState) -> str:
    return "success" if state.gate2_passed else "fallback"
