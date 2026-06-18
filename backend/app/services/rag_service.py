"""RAGService — chat_structured slice를 결선해 노출하는 backend orchestrator.

ADR-013 §9 LangGraph 토폴로지 + ADR-017 §3.1 응답 schema 정합.

본 슬라이스 범위:
  - tenant_resolver / load_tenant_config / build_acl_filter / retrieve_context
    / gate_1 / generate_answer / fallback
구현 미흡 노드(parse_response·verify_*·judge_inference·mask_response_pii·gate_2·
save_chat_log)는 이후 같은 deps 패턴으로 결선된다. 본 응답은 verifier 미가동
상태이므로 citation의 support_level·verified를 채우지 않는다 (CLAUDE.md 금지 #3).
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from rag_core.clients.in_memory import (
    InMemoryEmbedder,
    InMemoryLLMClient,
    InMemoryReranker,
    InMemoryVectorStore,
)
from rag_core.clients.qdrant_store import QdrantVectorStore
from rag_core.clients.tei_embedder import TEIBgeM3Embedder
from rag_core.clients.tei_reranker import TEIReranker
from rag_core.clients.vllm_client import VllmLLMClient
from rag_core.services.chat_log_writer import InMemoryChatLogWriter
from rag_core.services.generation_service import (
    GenerationPrompt,
    GenerationService,
)
from rag_core.pii import RegexPIIDetector
from rag_core.services.conflict_detector import ConflictDetector
from rag_core.services.judge_service import JudgePrompt, JudgeService
from rag_core.services.model_router import ModelRouter
from rag_core.services.pii_service import PIIService
from rag_core.services.query_classifier import QueryClassifier
from rag_core.services.query_rewriter import (
    QueryRewriter,
    QueryRewritePrompt,
)
from rag_core.services.streaming_chat_service import (
    StreamEvent,
    StreamingChatService,
    StreamingPrompt,
)
from rag_core.services.retrieval_service import RetrievalService
from rag_core.services.verifier_service import VerifierService
from rag_core.workflows import (
    RAGGraphDeps,
    RAGState,
    build_chat_structured_full,
)

from app.core.auth_adapter import UserContext
from app.core.config import Settings
from app.core.tenant_config_service import TenantConfigService


_PROMPT_YAML = "configs/platform/prompts/rag_answer.yaml"
_SCHEMA_JSON = "configs/platform/prompts/answer_schema.json"
_JUDGE_YAML = "configs/platform/prompts/inference_judge.yaml"
_JUDGE_SCHEMA = "configs/platform/prompts/inference_judge_schema.json"
_TIER2_YAML = "configs/platform/prompts/query_classifier_tier2.yaml"
_QUERY_REWRITE_YAML = "configs/platform/prompts/query_rewrite.yaml"
_STREAMING_YAML = "configs/platform/prompts/chat_streaming.yaml"


def _user_context_to_dict(user: UserContext) -> dict[str, Any]:
    return {
        "user_id": user.user_id,
        "domain_id": user.domain_id,
        "clearance": user.clearance,
        "department": user.department,
        "domain_groups": list(user.domain_groups),
        "roles": list(user.roles),
        "preferred_username": user.preferred_username,
        "email": user.email,
    }


def _config_to_dict(tc) -> dict[str, Any]:
    """TenantConfig dataclass → dict (LangGraph 노드는 dict 사용)."""
    return {
        "citation": tc.citation,
        "retrieval": tc.retrieval,
        "model": tc.model,
        "routing": tc.routing,
        "query_classifier": tc.query_classifier,
        "lifecycle": tc.lifecycle,
        "auth": tc.auth,
        "pii": tc.pii,
        "audit": tc.audit,
        "data_retention": tc.data_retention,
    }


class RAGService:
    """build_chat_structured_full 그래프 + chat_streaming 결선의 backend wrapper.

    Args:
        deps: rag_core RAGGraphDeps (이미 service들이 주입됨)
        default_model: state.selected_model이 비었을 때의 기본값
        streaming_service: ADR-013 §6.2 chat_streaming 모드 처리기 (None이면 endpoint 비활성)
        on_first_call: 첫 호출 직전에 1회만 실행되는 async hook (예: inmemory seed)
    """

    def __init__(
        self,
        *,
        deps: RAGGraphDeps,
        default_model: str,
        streaming_service: StreamingChatService | None = None,
        on_first_call=None,
        ledger_audit=None,
    ) -> None:
        self._deps = deps
        self._graph = build_chat_structured_full(deps)
        self._default_model = default_model
        self._streaming_service = streaming_service
        self._on_first_call = on_first_call
        self._initialized = on_first_call is None
        self._ledger = ledger_audit

    @property
    def streaming_enabled(self) -> bool:
        return self._streaming_service is not None

    async def ensure_initialized(self) -> None:
        """첫 호출 hook(`on_first_call`)을 명시적으로 트리거.

        chat_*가 아닌 다른 entrypoint(예: evaluation runner)가 같은 deps를 사용할 때
        seed corpus 등을 보장하기 위해 호출. idempotent.
        """
        if not self._initialized and self._on_first_call is not None:
            await self._on_first_call()
        self._initialized = True

    async def chat_structured(
        self,
        *,
        domain_id: str,
        user: UserContext,
        question: str,
        conversation_id: str | None,
    ) -> dict[str, Any]:
        if not self._initialized:
            await self._on_first_call()
            self._initialized = True

        # message_id == request_id — Feedback API가 같은 ID로 chat_logs를 식별한다
        # (ADR-017 §5 + §3.1 metadata). 한 응답 = 한 chat_logs row.
        # conversation_id는 응답과 chat_logs.conversation_id가 일치하도록 upfront 발급
        # — Conversation API(ADR-017 §4)가 같은 ID로 lookup 가능.
        request_id = uuid.uuid4().hex
        message_id = request_id
        conversation_id = conversation_id or uuid.uuid4().hex
        start = time.perf_counter()

        state = RAGState(
            request_id=request_id,
            domain_id=domain_id,
            user_id=user.user_id,
            conversation_id=conversation_id,
            question=question,
            user_context=_user_context_to_dict(user),
            selected_model=self._default_model,
            ui_mode="chat_structured",
        )
        out = await self._graph.ainvoke(state)
        latency_ms = int((time.perf_counter() - start) * 1000)

        # ADR-020 §8 — high severity PII block 발생 시 Ledger publish (best-effort)
        if (
            self._ledger is not None
            and out.get("fallback_reason") == "input_pii_blocked"
        ):
            try:
                await self._ledger.publish_pii_high_severity_block(
                    domain_id=domain_id,
                    actor=user.user_id,
                    categories=list(out.get("blocked_categories") or []),
                    details={"request_id": request_id, "ui_mode": "chat_structured"},
                )
            except Exception:  # noqa: BLE001
                pass  # 실패는 swallow — DomainRAG 응답 영향 차단

        return _build_chat_response(
            state=out,
            domain_id=domain_id,
            conversation_id=conversation_id,
            message_id=message_id,
            latency_ms=latency_ms,
        )

    async def chat_streaming(
        self,
        *,
        domain_id: str,
        user: UserContext,
        question: str,
        conversation_id: str | None,
    ):
        """ADR-013 §6.2 — SSE 토큰 스트림. citation 비활성, RAG 미사용.

        Returns: AsyncIterator[StreamEvent].
        """
        if self._streaming_service is None:
            raise RuntimeError("streaming_service not configured")
        if not self._initialized:
            await self._on_first_call()
            self._initialized = True

        tenant_config_dict = await self._load_tenant_config(domain_id)
        return self._streaming_service.stream(
            domain_id=domain_id,
            user_id=user.user_id,
            question=question,
            tenant_config=tenant_config_dict,
            conversation_id=conversation_id,
            selected_model=self._default_model,
        )

    async def _load_tenant_config(self, domain_id: str) -> dict[str, Any]:
        loader = self._deps.config_loader
        result = loader(domain_id)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        return dict(result or {})


# --------------------------------------------------------------------------- #
# Response builder (ADR-017 §3.1)
# --------------------------------------------------------------------------- #


def _build_chat_response(
    *,
    state: dict[str, Any],
    domain_id: str,
    conversation_id: str,
    message_id: str,
    latency_ms: int,
) -> dict[str, Any]:
    fallback_reason = state.get("fallback_reason")
    answer_segments = state.get("answer_segments") or []
    answer_text = state.get("final_answer") or "".join(
        seg.get("text", "") for seg in answer_segments
    )
    verifier_metrics = state.get("verifier_metrics") or {}

    base_meta = {
        "ui_mode": "chat_structured",
        # ADR-023 §4 — grounded(citation 검증) vs ungrounded(일반 대화, 근거 없음).
        "grounding": state.get("grounding") or "grounded",
        "llm_model": state.get("selected_model"),
        "lora_adapter": state.get("selected_lora"),
        "latency_ms": latency_ms,
        "gate1_metrics": state.get("gate1_metrics") or {},
        "confidence": state.get("confidence", 0.0),
        "citation_types": state.get("citation_types") or [],
        "verifier": {
            "tier1_markers_removed": verifier_metrics.get("tier1_markers_removed"),
            "tier2_avg_similarity": verifier_metrics.get("tier2_avg_similarity"),
            "tier3_unsupported_segments": verifier_metrics.get(
                "tier3_unsupported_segments"
            ),
            "claim_extraction_mode": "structured",
            "inference_judge_results": state.get("inference_judge_results") or [],
            "conflict_heuristic_detected": verifier_metrics.get(
                "conflict_heuristic_detected", 0
            ),
            "conflict_groups": state.get("conflict_groups") or [],
        },
        "pii": {
            "input_pii_found": state.get("input_pii_found") or [],
            "output_pii_masked": state.get("output_pii_masked") or [],
        },
    }

    if fallback_reason:
        if fallback_reason == "input_pii_blocked":
            fallback_block: dict[str, Any] = {
                "reason": fallback_reason,
                "blocked_categories": state.get("blocked_categories") or [],
                "suggested_actions": [
                    "민감 정보(주민번호·계좌·API key 등)를 제거하고 다시 시도하세요.",
                ],
            }
        else:
            fallback_block = {
                "reason": fallback_reason,
                "near_misses": state.get("near_misses") or [],
                "suggested_actions": [
                    "더 구체적인 키워드로 다시 질문해 보세요.",
                    "관련 부서에 직접 문의하세요.",
                ],
                "retry_after_seconds": 60,
            }
        return {
            "status": "fallback",
            "conversation_id": conversation_id,
            "message_id": message_id,
            "answer": answer_text or "현재 등록된 문서에서 충분한 근거를 찾지 못했습니다.",
            "fallback": fallback_block,
            "citations": [],
            "metadata": base_meta,
        }

    return {
        "status": "success",
        "conversation_id": conversation_id,
        "message_id": message_id,
        "answer": answer_text,
        "answer_segments": answer_segments,
        "citations": state.get("citations") or [],
        "limitations": state.get("limitations"),
        "metadata": base_meta,
    }


# --------------------------------------------------------------------------- #
# Factory — production / inmemory backend
# --------------------------------------------------------------------------- #


def _config_loader_from_tenant_config_service(domain_id: str) -> dict[str, Any]:
    return _config_to_dict(TenantConfigService.load(domain_id))


def _build_config_loader_with_pii_approval(approval_service):
    """ADR-020 §4 — tenant_config에 pii.storage.plain_approved 자동 주입.

    PIIService.mask_for_storage가 plain_approved=False면 mask로 fallback하므로 본
    loader는 매 요청마다 active 승인 여부를 조회해 config 트리에 심는다.
    """

    async def _loader(domain_id: str) -> dict[str, Any]:
        cfg = _config_to_dict(TenantConfigService.load(domain_id))
        approved = await approval_service.is_plain_approved(domain_id)
        pii_section = cfg.setdefault("pii", {})
        storage_section = pii_section.setdefault("storage", {})
        storage_section["plain_approved"] = approved
        return cfg

    return _loader


def _build_prompt(settings: Settings) -> GenerationPrompt:
    repo_root = settings.config_dir.resolve().parent
    return GenerationPrompt.load(
        prompt_yaml=Path(repo_root / _PROMPT_YAML),
        schema_json=Path(repo_root / _SCHEMA_JSON),
    )


def _build_prompt_provider(fallback: GenerationPrompt):
    """Prompt Studio PATCH(ADR-017 §12)가 chat_structured 흐름에 즉시 반영되도록.

    GenerationService가 매 generate_structured 호출 시 본 provider를 호출해
    effective system/user template을 가져온다. response_schema는 fallback 유지
    (PromptRecord는 yaml 메타만 보관, schema 본문 미보유).

    InMemory store(`PromptStudioService._runtime`)에 override가 있으면 그 record,
    없으면 platform yaml 기반 record를 반환. 둘 다 없으면 None — GenerationService가
    self._prompt(=fallback)으로 fallback.
    """

    def _provider(domain_id: str | None) -> GenerationPrompt | None:
        if domain_id is None:
            return None
        # PromptStudioService는 backend deps singleton. 순환 import 회피 위해 lazy.
        try:
            from app.deps import get_prompt_studio_service
            from app.core.config import get_settings as _gs

            svc = get_prompt_studio_service(_gs())
        except Exception:  # noqa: BLE001
            return None
        record = svc.get_prompt(domain_id, "chat_answer")
        if record is None:
            return None
        return GenerationPrompt(
            system=record.system,
            user=record.user,
            response_schema=fallback.response_schema,
        )

    return _provider


def _build_judge_prompt(settings: Settings) -> JudgePrompt:
    repo_root = settings.config_dir.resolve().parent
    return JudgePrompt.load(
        prompt_yaml=Path(repo_root / _JUDGE_YAML),
        schema_json=Path(repo_root / _JUDGE_SCHEMA),
    )


def _build_tier2_prompt(settings: Settings):
    repo_root = settings.config_dir.resolve().parent
    return QueryClassifier.load_tier2_prompt(Path(repo_root / _TIER2_YAML))


def _build_pii_service() -> PIIService:
    """ADR-020 — RegexPIIDetector(packages/rag_core/rag_core/pii/rules)를 그대로 사용."""
    import rag_core.pii as pii_pkg

    rules_dir = Path(pii_pkg.__file__).resolve().parent / "rules"
    return PIIService(detector=RegexPIIDetector(rules_dir=rules_dir))


def _build_conflict_detector() -> ConflictDetector:
    """ADR-010 §7 secondary — default 모든 패턴 활성. 테넌트가 citation.yaml override 시
    detect_conflict_heuristic_node가 ConflictDetector.from_config로 재구성한다."""
    return ConflictDetector(
        enabled_patterns={"date_diff", "numeric_diff", "rule_id_diff"}
    )


def _build_query_rewriter(
    settings: Settings,
    *,
    tenant_llm,
    shared_llm,
) -> QueryRewriter:
    """ADR-011 §5 — HyDE/llm_expand 두 strategy 지원. tenant_config에서 endpoint 선택."""
    repo_root = settings.config_dir.resolve().parent
    prompt = QueryRewritePrompt.load(Path(repo_root / _QUERY_REWRITE_YAML))
    return QueryRewriter(
        llm_clients={"tenant_slm": tenant_llm, "shared_llm": shared_llm},
        prompt=prompt,
        default_model=settings.rag_default_model,
    )


def _build_streaming_service(
    settings: Settings,
    *,
    llm,
    pii_service: PIIService,
    chat_log_writer,
    query_classifier=None,
    model_router=None,
    ledger_audit=None,
) -> StreamingChatService:
    """ADR-013 §6.2 — tenant_slm으로 SSE 자유 대화 streaming.

    classifier+router를 주입하면 streaming도 routing 결정을 거쳐 selected_model/lora 적용
    + chat_logs.classifier_decision/routing_decision 기록 (sync chat과 동일 분석 가능).
    ledger_audit가 주입되면 input_pii_blocked 시 ADR-020 §8 publish 호출.
    """
    repo_root = settings.config_dir.resolve().parent
    prompt = StreamingPrompt.load(Path(repo_root / _STREAMING_YAML))
    return StreamingChatService(
        llm=llm,
        prompt=prompt,
        default_model=settings.rag_default_model,
        pii_service=pii_service,
        chat_log_writer=chat_log_writer,
        query_classifier=query_classifier,
        model_router=model_router,
        ledger_audit=ledger_audit,
    )


def build_rag_service(
    settings: Settings,
    *,
    pii_approval_service=None,
    ledger_audit=None,
) -> RAGService:
    """env(rag_backend)에 따라 어댑터를 골라 RAGService 결선.

    Args:
        pii_approval_service: ADR-020 §4 — chat 흐름의 config_loader가 plain_approved를
            매 요청 조회. None이면 본 함수가 backend별 기본 구현체를 만든다.
        ledger_audit: ADR-020 §8 — chat 응답이 input_pii_blocked면 publish.
    """
    if settings.rag_backend == "inmemory":
        return _build_inmemory_service(
            settings,
            pii_approval_service=pii_approval_service,
            ledger_audit=ledger_audit,
        )
    return _build_production_service(
        settings,
        pii_approval_service=pii_approval_service,
        ledger_audit=ledger_audit,
    )


def _build_production_service(
    settings: Settings,
    *,
    pii_approval_service=None,
    ledger_audit=None,
) -> RAGService:
    from qdrant_client import AsyncQdrantClient

    from app.core.db import AdminSessionLocal, AppSessionLocal
    from app.services.chat_log_writer import build_postgres_chat_log_writer
    from app.services.pii_storage_approval_service import PiiStorageApprovalService

    qdrant = AsyncQdrantClient(
        url=f"http://{settings.qdrant_host}:{settings.qdrant_port}",
        api_key=settings.qdrant_api_key,
        prefer_grpc=False,
    )
    vector_store = QdrantVectorStore(client=qdrant)
    embedder = TEIBgeM3Embedder(base_url=settings.embedding_server_url)
    reranker = TEIReranker(base_url=settings.reranker_server_url)
    # routing config의 logical model 이름 → 실 vLLM model id 매핑.
    # settings.rag_default_model 하나로 모든 logical name을 흡수 (단일 vLLM 운영).
    # 향후 tenant_slm/shared_llm을 별도 instance로 분리할 땐 별도 env로.
    _model_aliases = {
        "tenant_slm": settings.rag_default_model,
        "shared_llm": settings.rag_default_model,
        "qwen2.5-7b-awq": settings.rag_default_model,
        "qwen3-7b-instruct": settings.rag_default_model,
    }
    tenant_llm = VllmLLMClient(
        base_url=settings.tenant_slm_base_url, model_aliases=_model_aliases,
    )
    shared_llm = VllmLLMClient(
        base_url=settings.shared_llm_base_url, model_aliases=_model_aliases,
    )
    retrieval = RetrievalService(
        embedder=embedder, vector_store=vector_store, reranker=reranker
    )
    prompt = _build_prompt(settings)
    generation = GenerationService(
        llm=tenant_llm, prompt=prompt, model=settings.rag_default_model,
        prompt_provider=_build_prompt_provider(prompt),
    )
    verifier = VerifierService(embedder=embedder)
    judge = JudgeService(
        llm=shared_llm,
        prompt=_build_judge_prompt(settings),
        model="qwen2.5-7b-awq",  # ADR-013 §4 shared_llm base_model(=Qwen2.5-7B-Instruct-AWQ) — configs로 분리 가능
    )
    classifier = QueryClassifier(
        llm=tenant_llm,                       # ADR-013 §3 tier2.endpoint=tenant_slm
        prompt=_build_tier2_prompt(settings),
        model=settings.rag_default_model,
    )
    router = ModelRouter()
    chat_log_writer = build_postgres_chat_log_writer(
        session_factory=AppSessionLocal,
        swallow_errors=True,  # ADR-022 후보: chat_logs 저장 실패가 응답을 막지 않음
    )
    pii_service = _build_pii_service()
    if pii_approval_service is None:
        pii_approval_service = PiiStorageApprovalService(
            admin_session_factory=AdminSessionLocal
        )
    deps = RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=_build_config_loader_with_pii_approval(pii_approval_service),
        today_provider=date.today,
        verifier_service=verifier,
        judge_service=judge,
        chat_log_writer=chat_log_writer,
        query_classifier=classifier,
        model_router=router,
        pii_service=pii_service,
        conflict_detector=_build_conflict_detector(),
        query_rewriter=_build_query_rewriter(
            settings, tenant_llm=tenant_llm, shared_llm=shared_llm
        ),
    )
    streaming = _build_streaming_service(
        settings,
        llm=tenant_llm,
        pii_service=pii_service,
        chat_log_writer=chat_log_writer,
        query_classifier=classifier,
        model_router=router,
        ledger_audit=ledger_audit,
    )
    return RAGService(
        deps=deps,
        default_model=settings.rag_default_model,
        streaming_service=streaming,
        ledger_audit=ledger_audit,
    )


def _build_inmemory_service(
    settings: Settings,
    *,
    pii_approval_service=None,
    ledger_audit=None,
) -> RAGService:
    """rag_backend=inmemory — 인프라 없이 /chat을 시연할 수 있는 dev 모드.

    seed corpus는 보안 도메인 2 chunks. LLM은 고정 JSON 응답 mock — 운영 금지.
    LLM citation 인덱스는 retrieval이 final_contexts로 반환할 순서와 일치시켜야
    Tier 2가 strong으로 통과한다. InMemory 환경에서 c1·c2 텍스트의 reranker score가
    동일·낮으므로 retrieval 순서는 fused_score 그대로(c1, c2)로 가정.
    """
    import json

    embedder = InMemoryEmbedder(dense_dim=64)
    reranker = InMemoryReranker()
    vector_store = InMemoryVectorStore()
    # InMemory reranker(Jaccard)가 question="패스워드 정책은?"에서 c2("만료")를 c1보다 먼저
    # 정렬하므로 mock LLM이 retrieval 순서와 정합되도록 cite 순서를 c2→c1으로 둔다.
    fixed_llm_response = json.dumps(
        {
            "answer_segments": [
                {"text": "패스워드 만료 주기는 90일입니다", "citations": [1],
                 "support_type": "direct"},
                {"text": "패스워드는 12자 이상이어야 합니다", "citations": [2],
                 "support_type": "direct"},
            ],
            "limitations": "본 응답은 inmemory dev 모드의 고정 mock입니다.",
        },
        ensure_ascii=False,
    )
    llm = InMemoryLLMClient(responses=[fixed_llm_response])

    async def _seed() -> None:
        await vector_store.create_collection(domain_id="security", dense_dim=64)
        seeds = [
            ("c1", "패스워드는 12자 이상이어야 합니다",
             {"approval_status": "approved", "security_level": "internal",
              "acl": ["group:security"], "doc_id": "d1",
              "title": "패스워드 정책", "page_number": 1, "section_title": "기본",
              "content": "패스워드는 12자 이상이어야 합니다"}),
            ("c2", "패스워드 만료 주기는 90일입니다",
             {"approval_status": "approved", "security_level": "internal",
              "acl": ["group:security"], "doc_id": "d1",
              "title": "패스워드 정책", "page_number": 2, "section_title": "만료",
              "content": "패스워드 만료 주기는 90일입니다"}),
        ]
        points = []
        for cid, text, payload in seeds:
            d, s = await embedder.embed_query(text)
            points.append(
                {"id": cid, "dense_vector": d, "sparse_vector": s, "payload": payload}
            )
        await vector_store.upsert_chunks(domain_id="security", points=points)

    retrieval = RetrievalService(
        embedder=embedder, vector_store=vector_store, reranker=reranker
    )
    prompt = _build_prompt(settings)
    generation = GenerationService(
        llm=llm, prompt=prompt, model=settings.rag_default_model,
        prompt_provider=_build_prompt_provider(prompt),
    )
    verifier = VerifierService(embedder=embedder)
    # 별도 mock LLM — judge는 generation과 다른 큐를 가져야 함 (judge 비활성 기본,
    # inference 응답을 만들 일이 거의 없으므로 기본 응답으로 valid=false 두고 실제
    # inference test에서는 응답을 다시 inject하는 방식이 운영 가까움)
    judge_llm = InMemoryLLMClient(
        responses=[json.dumps({"valid": False, "confidence": 0.0,
                               "reasoning": "inmemory dev — judge mock default"})]
    )
    judge = JudgeService(
        llm=judge_llm,
        prompt=_build_judge_prompt(settings),
        model="mock-shared",
    )

    from app.services.pii_storage_approval_service import (
        InMemoryPiiStorageApprovalService,
    )

    inmemory_approval_service = pii_approval_service or InMemoryPiiStorageApprovalService()

    async def _inmemory_config_loader(domain_id: str) -> dict[str, Any]:
        # InMemory cosine 분포에 맞춘 임계 — 운영(citation.yaml)은 strong=0.75/medium=0.55.
        cfg = _config_to_dict(TenantConfigService.load(domain_id))
        citation = cfg.setdefault("citation", {})
        citation.setdefault("verification", {}).setdefault("tier2", {})[
            "thresholds"
        ] = {"strong": 0.99, "medium": 0.85}
        citation.setdefault("gates", {})["retrieval"] = {
            "min_top1_rerank": 0.0,
            "min_strong_chunks": 0,
            "strong_chunk_threshold": 0.0,
        }
        citation["gates"]["generation"] = {
            "min_verified_count": 1,
            "max_unsupported_ratio": 0.5,
            "min_confidence": 0.3,
        }
        approved = await inmemory_approval_service.is_plain_approved(domain_id)
        pii_section = cfg.setdefault("pii", {})
        pii_section.setdefault("storage", {})["plain_approved"] = approved
        return cfg

    chat_log_writer = InMemoryChatLogWriter()
    classifier = QueryClassifier(
        llm=llm, prompt=_build_tier2_prompt(settings),
        model=settings.rag_default_model,
    )
    router = ModelRouter()
    pii_service = _build_pii_service()
    deps = RAGGraphDeps(
        retrieval_service=retrieval,
        generation_service=generation,
        config_loader=_inmemory_config_loader,
        today_provider=None,
        verifier_service=verifier,
        judge_service=judge,
        chat_log_writer=chat_log_writer,
        query_classifier=classifier,
        model_router=router,
        pii_service=pii_service,
        conflict_detector=_build_conflict_detector(),
        query_rewriter=_build_query_rewriter(
            settings, tenant_llm=llm, shared_llm=judge_llm
        ),
    )
    streaming = _build_streaming_service(
        settings,
        llm=llm,
        pii_service=pii_service,
        chat_log_writer=chat_log_writer,
        query_classifier=classifier,
        model_router=router,
        ledger_audit=ledger_audit,
    )
    return RAGService(
        deps=deps,
        default_model=settings.rag_default_model,
        streaming_service=streaming,
        on_first_call=_seed,
        ledger_audit=ledger_audit,
    )
