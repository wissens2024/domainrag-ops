"""FastAPI dependencies — 싱글턴 service 인스턴스 노출.

RAGService와 IndexingOrchestrator는 LangGraph 컴파일 / Protocol 어댑터 결선 비용을
가지므로 process당 1회만 구성한다. 모듈 변수에 보관하고 첫 호출 시 lazy 생성.
테스트에서는 reset_*() 헬퍼로 싱글턴을 비울 수 있다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.services.indexing_orchestrator import IndexingOrchestrator
from app.services.pii_storage_approval_service import (
    InMemoryPiiStorageApprovalService,
    PiiStorageApprovalService,
)
from app.services.rag_service import RAGService, build_rag_service

_rag_service: RAGService | None = None
_indexing_orchestrator: IndexingOrchestrator | None = None
_pii_approval_service: PiiStorageApprovalService | InMemoryPiiStorageApprovalService | None = None
_evaluation_orchestrator = None
_document_approval_service = None
_hard_delete_service = None
_input_schema_service = None
_document_metadata_service = None
_ledger_audit_service = None
_tenant_config_override_service = None
_tenant_lifecycle_service = None
_dashboard_analytics = None
_chat_log_reader = None
_feedback_writer = None
_conversation_repository = None
_citation_distribution = None
_citation_reverify_service = None
_prompt_studio_service = None
_lora_registry = None
_schema_editor_service = None
_assessment_repo = None
_assessment_extract_service = None
_assessment_generate_service = None
_assessment_hybrid_service = None
_assessment_logger = None
_keyhub_adapter = None
_chat_log_eraser = None
_oauth_state_store = None
_authfusion_token_client = None


def get_keyhub_adapter(settings: Settings = Depends(get_settings)):
    """ADR-019 §8 — secret store. dev: LocalSecretStore (filesystem)."""
    global _keyhub_adapter
    if _keyhub_adapter is None:
        if settings.keyhub_mode == "authfusion":
            # 운영 — AuthFusionKeyHub은 SSO 결선 작업에서 별도 구현
            raise NotImplementedError(
                "AuthFusionKeyHub 구현체는 ADR-018 SSO 결선 완료 후 추가됩니다."
                " 현재는 KEYHUB_MODE=local 사용."
            )
        from rag_core.clients import LocalSecretStore

        fernet_key = (
            settings.keyhub_local_fernet_key.encode("utf-8")
            if settings.keyhub_local_fernet_key
            else None
        )
        _keyhub_adapter = LocalSecretStore(
            base_path=Path(settings.keyhub_local_path),
            fernet_key=fernet_key,
        )
    return _keyhub_adapter


def reset_keyhub_adapter() -> None:
    global _keyhub_adapter
    _keyhub_adapter = None


_lora_orchestrator = None


def get_lora_orchestrator(settings: Settings = Depends(get_settings)):
    """ADR-013 §5 — registry + KeyHub + vLLM 결선.

    inmemory backend / RAG_BACKEND=inmemory에서는 vLLM=None으로 두어 registry 상태만
    전이한다. production은 VllmLLMClient를 주입.
    """
    global _lora_orchestrator
    if _lora_orchestrator is None:
        from app.services.lora_orchestrator import LoRAOrchestrator

        registry = get_lora_registry(settings)
        keyhub = get_keyhub_adapter(settings)
        if settings.rag_backend == "inmemory":
            vllm = None
        else:
            from rag_core.clients.vllm_client import VllmLLMClient

            vllm = VllmLLMClient(base_url=settings.tenant_slm_base_url)
        _lora_orchestrator = LoRAOrchestrator(
            registry=registry,
            keyhub=keyhub,
            vllm=vllm,
            shared_lora_path=Path(settings.vllm_shared_lora_path),
        )
    return _lora_orchestrator


def reset_lora_orchestrator() -> None:
    global _lora_orchestrator
    _lora_orchestrator = None


def get_pii_approval_service(
    settings: Settings = Depends(get_settings),
):
    """ADR-020 §4 — platform_admin plain 승인 관리.

    inmemory backend는 InMemoryPiiStorageApprovalService — DB 없이 동작. production은
    domainrag_platform_admin role engine을 통해 pii_storage_approvals 테이블 접근.
    """
    global _pii_approval_service
    if _pii_approval_service is None:
        ledger = get_ledger_audit_service(settings)
        if settings.rag_backend == "inmemory":
            _pii_approval_service = InMemoryPiiStorageApprovalService(
                ledger_audit=ledger
            )
        else:
            from app.core.db import AdminSessionLocal

            _pii_approval_service = PiiStorageApprovalService(
                admin_session_factory=AdminSessionLocal,
                ledger_audit=ledger,
            )
    return _pii_approval_service


def reset_pii_approval_service() -> None:
    """테스트용 — singleton 리셋."""
    global _pii_approval_service
    _pii_approval_service = None


def get_rag_service(settings: Settings = Depends(get_settings)) -> RAGService:
    global _rag_service
    if _rag_service is None:
        approval = get_pii_approval_service(settings)
        ledger = get_ledger_audit_service(settings)
        _rag_service = build_rag_service(
            settings,
            pii_approval_service=approval,
            ledger_audit=ledger,
        )
    return _rag_service


def reset_rag_service() -> None:
    """테스트용 — singleton 리셋."""
    global _rag_service
    _rag_service = None


def get_indexing_orchestrator(
    settings: Settings = Depends(get_settings),
) -> IndexingOrchestrator:
    global _indexing_orchestrator
    if _indexing_orchestrator is None:
        _indexing_orchestrator = _build_indexing_orchestrator(settings)
    return _indexing_orchestrator


def reset_indexing_orchestrator() -> None:
    """테스트용 — singleton 리셋."""
    global _indexing_orchestrator
    _indexing_orchestrator = None


def get_dashboard_analytics(settings: Settings = Depends(get_settings)):
    """ADR-017 §10 — admin 대시보드 집계.

    inmemory backend는 RAGService/IndexingOrchestrator의 in-memory store(
    chat_log_writer / document_repo / chunk_repo / job_repo)를 공유하는
    InMemoryDashboardAnalytics. production은 PostgresDashboardAnalytics + RLS.
    """
    global _dashboard_analytics
    if _dashboard_analytics is None:
        if settings.rag_backend == "inmemory":
            from rag_core.interfaces.dashboard_analytics import (
                InMemoryDashboardAnalytics,
            )

            rag = get_rag_service(settings)
            orch = get_indexing_orchestrator(settings)
            writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
            _dashboard_analytics = InMemoryDashboardAnalytics(
                chat_log_writer=writer,
                document_repo=orch.service.document_repo,
                chunk_repo=orch.service.chunk_repo,
                indexing_job_repo=orch.service.job_repo,
            )
        else:
            from app.core.db import AppSessionLocal
            from app.services.dashboard_analytics import PostgresDashboardAnalytics

            _dashboard_analytics = PostgresDashboardAnalytics(
                session_factory=AppSessionLocal,
            )
    return _dashboard_analytics


def reset_dashboard_analytics() -> None:
    """테스트용 — singleton 리셋."""
    global _dashboard_analytics
    _dashboard_analytics = None


def get_citation_distribution(settings: Settings = Depends(get_settings)):
    """ADR-017 §9 — citation_type 분포 시계열 (day/hour 버킷).

    inmemory backend는 InMemoryChatLogWriter.records 위에서 단일 'all' 버킷 집계,
    production은 PostgresCitationDistributionAnalytics + RLS + date_trunc.
    """
    global _citation_distribution
    if _citation_distribution is None:
        if settings.rag_backend == "inmemory":
            from rag_core.interfaces.citation_distribution import (
                InMemoryCitationDistributionAnalytics,
            )

            rag = get_rag_service(settings)
            writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
            _citation_distribution = InMemoryCitationDistributionAnalytics(
                writer=writer
            )
        else:
            from app.core.db import AppSessionLocal
            from app.services.citation_distribution import (
                PostgresCitationDistributionAnalytics,
            )

            _citation_distribution = PostgresCitationDistributionAnalytics(
                session_factory=AppSessionLocal,
            )
    return _citation_distribution


def reset_citation_distribution() -> None:
    """테스트용 — singleton 리셋."""
    global _citation_distribution
    _citation_distribution = None


def get_citation_reverify_service(settings: Settings = Depends(get_settings)):
    """ADR-017 §9 + ADR-010 §4 — Tier 2 재검증 orchestrator.

    inmemory backend는 RAGService deps의 embedder + InMemoryChatLogCitationUpdater
    (writer.records 공유). production은 Postgres updater + admin engine audit.
    """
    global _citation_reverify_service
    if _citation_reverify_service is None:
        from app.services.citation_reverify_service import CitationReverifyService

        rag = get_rag_service(settings)
        embedder = rag._deps.retrieval_service.embedder  # type: ignore[attr-defined]
        reader = get_chat_log_reader(settings)
        if settings.rag_backend == "inmemory":
            from app.services.citation_reverify_service import (
                InMemoryChatLogCitationUpdater,
            )

            writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
            updater = InMemoryChatLogCitationUpdater(writer=writer)
            admin_sf = None
        else:
            from app.core.db import AdminSessionLocal, AppSessionLocal
            from app.services.citation_reverify_service import ChatLogCitationUpdater

            updater = ChatLogCitationUpdater(session_factory=AppSessionLocal)
            admin_sf = AdminSessionLocal
        _citation_reverify_service = CitationReverifyService(
            chat_log_reader=reader,
            updater=updater,
            embedder=embedder,
            admin_session_factory=admin_sf,
        )
    return _citation_reverify_service


def reset_citation_reverify_service() -> None:
    """테스트용 — singleton 리셋."""
    global _citation_reverify_service
    _citation_reverify_service = None


def get_assessment_item_repository(settings: Settings = Depends(get_settings)):
    """ADR-014 §1 — assessment_items CRUD."""
    global _assessment_repo
    if _assessment_repo is None:
        if settings.rag_backend == "inmemory":
            from rag_core.interfaces.assessment_item_repository import (
                InMemoryAssessmentItemRepository,
            )

            _assessment_repo = InMemoryAssessmentItemRepository()
        else:
            from app.core.db import AppSessionLocal
            from app.repositories import PostgresAssessmentItemRepository

            _assessment_repo = PostgresAssessmentItemRepository(
                session_factory=AppSessionLocal,
            )
    return _assessment_repo


def reset_assessment_item_repository() -> None:
    global _assessment_repo, _assessment_extract_service
    global _assessment_generate_service, _assessment_hybrid_service
    _assessment_repo = None
    _assessment_extract_service = None
    _assessment_generate_service = None
    _assessment_hybrid_service = None


def get_assessment_logger(settings: Settings = Depends(get_settings)):
    """ADR-014 §10 — assessment_logs INSERT."""
    global _assessment_logger
    if _assessment_logger is None:
        if settings.rag_backend == "inmemory":
            from rag_core.services.assessment_logger import (
                InMemoryAssessmentLogger,
            )

            _assessment_logger = InMemoryAssessmentLogger()
        else:
            from app.core.db import AppSessionLocal
            from app.services.assessment_logger import PostgresAssessmentLogger

            _assessment_logger = PostgresAssessmentLogger(
                session_factory=AppSessionLocal,
            )
    return _assessment_logger


def reset_assessment_logger() -> None:
    global _assessment_logger
    _assessment_logger = None


def get_assessment_extract_service(settings: Settings = Depends(get_settings)):
    """ADR-014 §3 Mode 1 — SQL 조건 매칭 + 난이도 분포 sampling."""
    global _assessment_extract_service
    if _assessment_extract_service is None:
        from rag_core.services.assessment_extract import AssessmentExtractService

        repo = get_assessment_item_repository(settings)
        _assessment_extract_service = AssessmentExtractService(repository=repo)
    return _assessment_extract_service


def get_assessment_generate_service(settings: Settings = Depends(get_settings)):
    """ADR-014 §3 Mode 2 — LLM 생성 + similarity + validator."""
    global _assessment_generate_service
    if _assessment_generate_service is None:
        from rag_core.services.assessment_generate import AssessmentGenerateService
        from rag_core.services.assessment_similarity import (
            AssessmentSimilarityChecker,
            SimilarityThresholds,
        )
        from rag_core.services.assessment_validator import AssessmentValidator

        repo = get_assessment_item_repository(settings)
        rag = get_rag_service(settings)
        gen = rag._deps.generation_service  # type: ignore[attr-defined]
        llm = getattr(gen, "_llm", None)
        embedder = rag._deps.retrieval_service.embedder  # type: ignore[attr-defined]

        similarity = AssessmentSimilarityChecker(
            embedder=embedder,
            thresholds=SimilarityThresholds(duplicate=0.85, similar=0.65),
        )
        validator = AssessmentValidator(llm_client=llm, model="shared_llm")
        _assessment_generate_service = AssessmentGenerateService(
            repository=repo,
            llm_client=llm,
            similarity_checker=similarity,
            validator=validator,
            model="shared_llm",
        )
    return _assessment_generate_service


def get_assessment_hybrid_service(settings: Settings = Depends(get_settings)):
    """ADR-014 §3 Mode 3 — extract + generate 조합."""
    global _assessment_hybrid_service
    if _assessment_hybrid_service is None:
        from rag_core.services.assessment_hybrid import AssessmentHybridService

        _assessment_hybrid_service = AssessmentHybridService(
            extract_service=get_assessment_extract_service(settings),
            generate_service=get_assessment_generate_service(settings),
        )
    return _assessment_hybrid_service


def get_schema_editor_service(settings: Settings = Depends(get_settings)):
    """ADR-017 §15 + ADR-015 — tenant_input_schemas CRUD + backward compat.

    inmemory backend: InMemoryTenantInputSchemaRepository.
    production: PostgresTenantInputSchemaRepository + RLS + UNIQUE(tenant_id, schema_version).
    """
    global _schema_editor_service
    if _schema_editor_service is None:
        from app.services.schema_editor_service import SchemaEditorService

        if settings.rag_backend == "inmemory":
            from rag_core.interfaces.tenant_input_schema_repository import (
                InMemoryTenantInputSchemaRepository,
            )

            repo = InMemoryTenantInputSchemaRepository()
        else:
            from app.core.db import AppSessionLocal
            from app.repositories import PostgresTenantInputSchemaRepository

            repo = PostgresTenantInputSchemaRepository(
                session_factory=AppSessionLocal
            )
        # ADR-017 §15 follow-up — PUT 직후 InputSchemaLoader runtime layer에 push.
        from app.core.input_schema import InputSchemaLoader

        _schema_editor_service = SchemaEditorService(
            repo=repo,
            runtime_apply=InputSchemaLoader.apply_runtime_override,
        )
    return _schema_editor_service


def reset_schema_editor_service() -> None:
    """테스트용 — singleton 리셋."""
    global _schema_editor_service
    _schema_editor_service = None


def get_lora_registry(settings: Settings = Depends(get_settings)):
    """ADR-017 §14 + ADR-013 — LoRA adapter lifecycle.

    inmemory backend: InMemoryLoRARegistry (전역 dict, adapter_id UNIQUE).
    production: PostgresLoRARegistry + RLS + adapter_registry 테이블.
    """
    global _lora_registry
    if _lora_registry is None:
        if settings.rag_backend == "inmemory":
            from rag_core.interfaces.lora_registry import InMemoryLoRARegistry

            _lora_registry = InMemoryLoRARegistry()
        else:
            from app.core.db import AppSessionLocal
            from app.repositories import PostgresLoRARegistry

            _lora_registry = PostgresLoRARegistry(
                session_factory=AppSessionLocal,
            )
    return _lora_registry


def reset_lora_registry() -> None:
    """테스트용 — singleton 리셋."""
    global _lora_registry
    _lora_registry = None


def get_prompt_studio_service(settings: Settings = Depends(get_settings)):
    """ADR-017 §12 — prompts CRUD + preview.

    in-process override store(PromptStudioService._runtime). preview는 RAGService의
    LLMClient 공유 — invoke_llm=True일 때만 호출되어 chat 영향 없음.
    """
    global _prompt_studio_service
    if _prompt_studio_service is None:
        from app.services.prompt_studio_service import PromptStudioService

        rag = get_rag_service(settings)
        # GenerationService 내부 LLM을 preview에 재사용 (chat_answer task 기준).
        # 다른 task의 LLM은 향후 task→llm 매핑이 확장될 때 분리 (현재 stub).
        gen = rag._deps.generation_service  # type: ignore[attr-defined]
        llm = getattr(gen, "_llm", None)
        _prompt_studio_service = PromptStudioService(
            config_dir=settings.config_dir.resolve(),
            llm_client=llm,
        )
    return _prompt_studio_service


def reset_prompt_studio_service() -> None:
    """테스트용 — singleton + class-level runtime override 리셋."""
    global _prompt_studio_service
    _prompt_studio_service = None
    try:
        from app.services.prompt_studio_service import PromptStudioService

        PromptStudioService.reset()
    except Exception:  # noqa: BLE001
        pass


def get_conversation_repository(settings: Settings = Depends(get_settings)):
    """ADR-017 §4 — conversations CRUD.

    inmemory backend는 RAGService의 InMemoryChatLogWriter.records 위에서 동작하는
    InMemoryConversationRepository (제목 override + 삭제 set in-process).
    production은 PostgresConversationRepository + AppSessionLocal + RLS.
    """
    global _conversation_repository
    if _conversation_repository is None:
        if settings.rag_backend == "inmemory":
            from rag_core.interfaces.conversation_repository import (
                InMemoryConversationRepository,
            )

            rag = get_rag_service(settings)
            writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
            _conversation_repository = InMemoryConversationRepository(writer=writer)
        else:
            from app.core.db import AppSessionLocal
            from app.repositories import PostgresConversationRepository

            _conversation_repository = PostgresConversationRepository(
                session_factory=AppSessionLocal,
            )
    return _conversation_repository


def reset_conversation_repository() -> None:
    """테스트용 — singleton 리셋."""
    global _conversation_repository
    _conversation_repository = None


def get_feedback_writer(settings: Settings = Depends(get_settings)):
    """ADR-017 §5 — POST /feedback의 chat_logs UPDATE.

    inmemory backend는 RAGService의 InMemoryChatLogWriter.records를 공유하는
    InMemoryFeedbackWriter. production은 PostgresFeedbackWriter + RLS.
    """
    global _feedback_writer
    if _feedback_writer is None:
        if settings.rag_backend == "inmemory":
            from rag_core.interfaces.feedback_writer import InMemoryFeedbackWriter

            rag = get_rag_service(settings)
            writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
            _feedback_writer = InMemoryFeedbackWriter(writer=writer)
        else:
            from app.core.db import AppSessionLocal
            from app.services.feedback_writer import PostgresFeedbackWriter

            _feedback_writer = PostgresFeedbackWriter(app_session_factory=AppSessionLocal)
    return _feedback_writer


def reset_feedback_writer() -> None:
    """테스트용 — singleton 리셋."""
    global _feedback_writer
    _feedback_writer = None


def get_chat_log_reader(settings: Settings = Depends(get_settings)):
    """ADR-017 §8 — admin chat_logs 조회.

    inmemory backend는 RAGService의 InMemoryChatLogWriter.records를 공유하는
    InMemoryChatLogReader. production은 Postgres + RLS — `domainrag_app` engine을
    재활용한다(읽기는 RLS로 충분, BYPASSRLS는 불필요).
    """
    global _chat_log_reader
    if _chat_log_reader is None:
        if settings.rag_backend == "inmemory":
            from rag_core.interfaces.chat_log_reader import InMemoryChatLogReader

            rag = get_rag_service(settings)
            writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
            _chat_log_reader = InMemoryChatLogReader(writer=writer)
        else:
            from app.core.db import AppSessionLocal
            from app.repositories import PostgresChatLogReader

            _chat_log_reader = PostgresChatLogReader(session_factory=AppSessionLocal)
    return _chat_log_reader


def reset_chat_log_reader() -> None:
    """테스트용 — singleton 리셋."""
    global _chat_log_reader
    _chat_log_reader = None


def get_chat_log_eraser(settings: Settings = Depends(get_settings)):
    """ADR-020 §10 — chat_logs right-to-erasure.

    inmemory backend는 RAGService와 같은 InMemoryChatLogWriter records를 공유하는
    InMemoryChatLogEraser. production은 Postgres + RLS + admin engine audit.
    """
    global _chat_log_eraser
    if _chat_log_eraser is None:
        if settings.rag_backend == "inmemory":
            from rag_core.services.chat_log_erasure import InMemoryChatLogEraser

            rag = get_rag_service(settings)
            writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
            _chat_log_eraser = InMemoryChatLogEraser(writer=writer)
        else:
            from app.core.db import AdminSessionLocal, AppSessionLocal
            from app.services.chat_log_erasure import PostgresChatLogEraser

            _chat_log_eraser = PostgresChatLogEraser(
                app_session_factory=AppSessionLocal,
                admin_session_factory=AdminSessionLocal,
            )
    return _chat_log_eraser


def reset_chat_log_eraser() -> None:
    """테스트용 — singleton 리셋."""
    global _chat_log_eraser
    _chat_log_eraser = None


def get_oauth_state_store(settings: Settings = Depends(get_settings)):
    """ADR-018 §2 — PKCE state store. dev/test는 InMemory. 운영 다중 인스턴스에서는
    별도 ADR로 Redis 구현체 wire (본 함수 분기점)."""
    global _oauth_state_store
    if _oauth_state_store is None:
        from app.core.oauth_state_store import InMemoryOAuthStateStore

        _oauth_state_store = InMemoryOAuthStateStore()
    return _oauth_state_store


def reset_oauth_state_store() -> None:
    """테스트용 — singleton 리셋."""
    global _oauth_state_store
    _oauth_state_store = None


def get_authfusion_token_client(settings: Settings = Depends(get_settings)):
    """ADR-018 §6 — token/revoke endpoint 호출 클라이언트.

    AuthFusion은 CONFIDENTIAL client + PKCE 권장 (OIDC-INTEGRATION-GUIDE §8).
    client_secret 미설정 시 4xx `invalid_client` 발생하므로 본 wiring에서 명시 주입.
    """
    global _authfusion_token_client
    if _authfusion_token_client is None:
        from app.core.auth_config import AuthConfigLoader
        from app.core.authfusion_token_client import HttpxAuthFusionTokenClient

        auth_cfg = AuthConfigLoader.load(settings)
        if not auth_cfg.token_endpoint:
            # mock 모드에서 endpoint 미설정 — None 반환, endpoint가 mock 응답 분기.
            return None
        _authfusion_token_client = HttpxAuthFusionTokenClient(
            token_endpoint=auth_cfg.token_endpoint,
            revoke_endpoint=auth_cfg.revoke_endpoint,
            default_client_secret=auth_cfg.client_secret,
        )
    return _authfusion_token_client


def reset_authfusion_token_client() -> None:
    """테스트용 — singleton 리셋."""
    global _authfusion_token_client
    _authfusion_token_client = None


def get_evaluation_orchestrator(settings: Settings = Depends(get_settings)):
    """ADR-009 §7 + ADR-017 §16 — 평가 실행 wrapper.

    deps는 RAGService와 공유해 inmemory backend의 vector store/embedder를 그대로 사용한다.
    repository는 Postgres(운영) 또는 InMemory(dev/tests).
    """
    global _evaluation_orchestrator
    if _evaluation_orchestrator is None:
        from pathlib import Path

        from rag_core.interfaces.evaluation_job_repository import (
            InMemoryEvaluationJobRepository,
        )

        from app.services.evaluation_orchestrator import EvaluationOrchestrator

        rag = get_rag_service(settings)
        deps = rag._deps  # type: ignore[attr-defined]

        if settings.rag_backend == "inmemory":
            repo = InMemoryEvaluationJobRepository()
        else:
            from app.core.db import AppSessionLocal
            from app.repositories.evaluation_job_repository import (
                PostgresEvaluationJobRepository,
            )

            repo = PostgresEvaluationJobRepository(session_factory=AppSessionLocal)

        eval_root = settings.config_dir.resolve().parent / "data" / "eval"
        _evaluation_orchestrator = EvaluationOrchestrator(
            deps=deps,
            repo=repo,
            eval_root=eval_root,
            ensure_initialized=rag.ensure_initialized,
            ledger_audit=get_ledger_audit_service(settings),
        )
    return _evaluation_orchestrator


def reset_evaluation_orchestrator() -> None:
    """테스트용 — singleton 리셋."""
    global _evaluation_orchestrator
    _evaluation_orchestrator = None


def get_document_approval_service(settings: Settings = Depends(get_settings)):
    """ADR-012 §3-8 — documents/chunks/payload payload-only sync.

    IndexingOrchestrator와 같은 backend의 DocumentRepository/ChunkRepository/VectorStore를
    공유한다 (inmemory backend는 RAGService deps와 동일 인스턴스). audit은 admin engine
    (production만), inmemory는 admin None으로 audit skip.
    """
    global _document_approval_service
    if _document_approval_service is None:
        from app.services.document_approval_service import DocumentApprovalService

        orch = get_indexing_orchestrator(settings)
        admin = None
        if settings.rag_backend != "inmemory":
            from app.core.db import AdminSessionLocal

            admin = AdminSessionLocal

        _document_approval_service = DocumentApprovalService(
            document_repo=orch.service.document_repo,
            chunk_repo=orch.service.chunk_repo,
            vector_store=orch.service.vector_store,
            admin_session_factory=admin,
        )
    return _document_approval_service


def reset_document_approval_service() -> None:
    """테스트용 — singleton 리셋."""
    global _document_approval_service
    _document_approval_service = None


def get_hard_delete_service(settings: Settings = Depends(get_settings)):
    """ADR-007/012 hard delete cross-system orchestrator.

    inmemory backend: IndexingOrchestrator deps + RAGService의 InMemoryChatLogWriter
        records를 InMemoryChatLogsActionHandler로 묶어 mask/delete_logs 모드 동작.
    production: PostgresChunkRepository/DocumentRepository + QdrantVectorStore + MinIOStorage,
        chat_logs는 _app session(RLS) SQL 경로.
    """
    global _hard_delete_service
    if _hard_delete_service is None:
        from app.services.hard_delete_service import (
            HardDeleteService,
            InMemoryChatLogsActionHandler,
        )

        orch = get_indexing_orchestrator(settings)
        handler = None
        app_sf = None
        admin_sf = None
        if settings.rag_backend == "inmemory":
            rag = get_rag_service(settings)
            writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
            handler = InMemoryChatLogsActionHandler(writer=writer)
        else:
            from app.core.db import AdminSessionLocal, AppSessionLocal

            app_sf = AppSessionLocal
            admin_sf = AdminSessionLocal

        _hard_delete_service = HardDeleteService(
            document_repo=orch.service.document_repo,
            chunk_repo=orch.service.chunk_repo,
            vector_store=orch.service.vector_store,
            storage=orch._storage,  # type: ignore[attr-defined]
            app_session_factory=app_sf,
            admin_session_factory=admin_sf,
            chat_logs_handler=handler,
            ledger_audit=get_ledger_audit_service(settings),
        )
    return _hard_delete_service


def reset_hard_delete_service() -> None:
    """테스트용 — singleton 리셋."""
    global _hard_delete_service
    _hard_delete_service = None


def get_input_schema_service(settings: Settings = Depends(get_settings)):
    """ADR-015 — common_fields.yaml + tenants/<id>/input_schema.yaml 합성 + 검증."""
    global _input_schema_service
    if _input_schema_service is None:
        from app.core.input_schema import InputSchemaService

        _input_schema_service = InputSchemaService(
            config_dir=settings.config_dir.resolve()
        )
    return _input_schema_service


def reset_input_schema_service() -> None:
    """테스트용 — singleton + loader cache + runtime override 리셋."""
    global _input_schema_service
    _input_schema_service = None
    try:
        from app.core.input_schema import InputSchemaLoader

        InputSchemaLoader.reset()
        InputSchemaLoader.clear_runtime_override()
    except Exception:  # noqa: BLE001
        pass


def get_document_metadata_service(settings: Settings = Depends(get_settings)):
    """ADR-017 §6.4 — PATCH documents 부분 갱신 + chunks/Qdrant payload sync.

    IndexingOrchestrator의 deps와 doc_repo/chunk_repo/vector_store 공유 + InputSchemaService
    를 받아 merged validation. inmemory backend는 admin=None으로 audit skip.
    """
    global _document_metadata_service
    if _document_metadata_service is None:
        from app.services.document_metadata_service import DocumentMetadataService

        orch = get_indexing_orchestrator(settings)
        schema = get_input_schema_service(settings)
        admin = None
        if settings.rag_backend != "inmemory":
            from app.core.db import AdminSessionLocal

            admin = AdminSessionLocal
        _document_metadata_service = DocumentMetadataService(
            document_repo=orch.service.document_repo,
            chunk_repo=orch.service.chunk_repo,
            vector_store=orch.service.vector_store,
            schema_service=schema,
            admin_session_factory=admin,
        )
    return _document_metadata_service


def reset_document_metadata_service() -> None:
    """테스트용 — singleton 리셋."""
    global _document_metadata_service
    _document_metadata_service = None


def get_ledger_audit_service(settings: Settings = Depends(get_settings)):
    """ADR-020 §8 — AuthFusion Ledger publish facade.

    LEDGER_ENABLE=false면 NoopLedgerClient(in-memory record), true면 HttpxLedgerClient.
    인메모리/운영 모두 동일 LedgerAuditService 인터페이스로 호출 가능.
    """
    global _ledger_audit_service
    if _ledger_audit_service is None:
        from app.services.ledger_audit_service import LedgerAuditService
        from app.services.ledger_client import HttpxLedgerClient, NoopLedgerClient

        if settings.ledger_enable and settings.ledger_endpoint:
            client = HttpxLedgerClient(
                endpoint=settings.ledger_endpoint,
                api_key=settings.ledger_api_key or None,
            )
        else:
            client = NoopLedgerClient()
        _ledger_audit_service = LedgerAuditService(
            client=client, enable=settings.ledger_enable
        )
    return _ledger_audit_service


def reset_ledger_audit_service() -> None:
    """테스트용 — singleton 리셋."""
    global _ledger_audit_service
    _ledger_audit_service = None


def get_tenant_config_override_service(settings: Settings = Depends(get_settings)):
    """ADR-009 §3·§8 — tenant_config_overrides CRUD + audit + ledger."""
    global _tenant_config_override_service
    if _tenant_config_override_service is None:
        ledger = get_ledger_audit_service(settings)
        if settings.rag_backend == "inmemory":
            from app.services.tenant_config_service import (
                InMemoryTenantConfigOverrideService,
            )

            _tenant_config_override_service = InMemoryTenantConfigOverrideService(
                ledger_audit=ledger,
            )
        else:
            from app.core.db import AppSessionLocal
            from app.services.tenant_config_service import (
                TenantConfigOverrideService,
            )

            _tenant_config_override_service = TenantConfigOverrideService(
                session_factory=AppSessionLocal,
                ledger_audit=ledger,
            )
    return _tenant_config_override_service


def reset_tenant_config_override_service() -> None:
    """테스트용 — singleton 리셋."""
    global _tenant_config_override_service
    _tenant_config_override_service = None


def get_tenant_lifecycle_service(settings: Settings = Depends(get_settings)):
    """ADR-008/012 + ADR-017 §18 — tenant CRUD + status 전이 + hard delete.

    inmemory backend: dict 기반 InMemoryTenantLifecycleService.
    production: PostgresTenantLifecycleService — admin engine(BYPASSRLS)으로 tenants/
        관련 테이블 + Qdrant/MinIO cross-system 일관성 (ADR-012 §6).
    """
    global _tenant_lifecycle_service
    if _tenant_lifecycle_service is None:
        ledger = get_ledger_audit_service(settings)
        orch = get_indexing_orchestrator(settings)
        if settings.rag_backend == "inmemory":
            from app.services.tenant_lifecycle_service import (
                InMemoryTenantLifecycleService,
            )

            _tenant_lifecycle_service = InMemoryTenantLifecycleService(
                vector_store=orch.service.vector_store,
                storage=orch._storage,  # type: ignore[attr-defined]
                embedder_dim_provider=lambda: orch.service.embedder.dense_dim,
                ledger_audit=ledger,
            )
        else:
            from app.core.db import AdminSessionLocal
            from app.services.tenant_lifecycle_service import (
                PostgresTenantLifecycleService,
            )

            _tenant_lifecycle_service = PostgresTenantLifecycleService(
                admin_session_factory=AdminSessionLocal,
                vector_store=orch.service.vector_store,
                storage=orch._storage,  # type: ignore[attr-defined]
                embedder_dim_provider=lambda: orch.service.embedder.dense_dim,
                ledger_audit=ledger,
            )
    return _tenant_lifecycle_service


def reset_tenant_lifecycle_service() -> None:
    """테스트용 — singleton 리셋."""
    global _tenant_lifecycle_service
    _tenant_lifecycle_service = None


def _build_indexing_orchestrator(settings: Settings) -> IndexingOrchestrator:
    if settings.rag_backend == "inmemory":
        return _build_inmemory_orchestrator(settings)
    return _build_production_orchestrator(settings)


def _build_production_orchestrator(settings: Settings) -> IndexingOrchestrator:
    """운영 — Postgres repository + MinIO storage + TEI bge-m3 + Qdrant."""
    from qdrant_client import AsyncQdrantClient
    from rag_core.clients.qdrant_store import QdrantVectorStore
    from rag_core.clients.tei_embedder import TEIBgeM3Embedder
    from rag_core.services.indexing_service import IndexingService

    from app.core.db import AppSessionLocal
    from app.repositories import (
        PostgresChunkRepository,
        PostgresDocumentRepository,
        PostgresIndexingJobRepository,
    )
    from app.services.document_storage import MinIOStorage
    from app.services.rag_service import (
        _build_pii_service,
        _config_loader_from_tenant_config_service,
    )

    # parser/chunker는 운영 구현이 추가될 때까지 InMemory 결정론을 그대로 사용한다.
    # 운영 PDF parser/markdown parser는 별도 ADR-007 §5 후속 작업에서 결선된다.
    from rag_core.clients.in_memory import InMemoryChunker, InMemoryParser

    qdrant = AsyncQdrantClient(
        url=f"http://{settings.qdrant_host}:{settings.qdrant_port}",
        api_key=settings.qdrant_api_key,
        prefer_grpc=False,
    )
    vector_store = QdrantVectorStore(client=qdrant)
    embedder = TEIBgeM3Embedder(base_url=settings.embedding_server_url)
    pii_service = _build_pii_service()

    document_repo = PostgresDocumentRepository(session_factory=AppSessionLocal)
    chunk_repo = PostgresChunkRepository(session_factory=AppSessionLocal)
    job_repo = PostgresIndexingJobRepository(session_factory=AppSessionLocal)

    indexing_service = IndexingService(
        parser=InMemoryParser(),
        chunker=InMemoryChunker(max_tokens=400, overlap_tokens=80),
        embedder=embedder,
        vector_store=vector_store,
        pii_service=pii_service,
        chunk_repo=chunk_repo,
        document_repo=document_repo,
        job_repo=job_repo,
    )

    from minio import Minio

    minio_client = Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_use_ssl,
    )
    storage = MinIOStorage(
        client=minio_client,
        bucket=settings.minio_bucket,
        cache_dir=Path(tempfile.gettempdir()) / "domainrag-uploads",
    )

    return IndexingOrchestrator(
        indexing_service=indexing_service,
        storage=storage,
        config_loader=_config_loader_from_tenant_config_service,
    )


def _build_inmemory_orchestrator(settings: Settings) -> IndexingOrchestrator:
    """RAG_BACKEND=inmemory — 인프라 의존 없이 endpoint 동작 검증용.

    중요: RAGService(inmemory)와 vector_store를 공유해야 새로 업로드된 chunk가 chat 응답에
    반영된다. 본 헬퍼는 build_rag_service 호출로 싱글턴 RAGService를 먼저 보장한 뒤 그
    내부의 deps(=vector_store / embedder / pii_service / config_loader)를 그대로 재사용한다.
    """
    from rag_core.clients.in_memory import (
        InMemoryChunker,
        InMemoryChunkRepository,
        InMemoryDocumentRepository,
        InMemoryIndexingJobRepository,
        InMemoryParser,
    )
    from rag_core.services.indexing_service import IndexingService

    # RAGService 싱글턴을 (혹시 아직 안 만들어졌다면) 만든다 → vector_store 공유.
    rag = get_rag_service(settings)  # noqa: F841 — 부작용으로 RAGService 빌드
    deps = rag._deps  # type: ignore[attr-defined]

    indexing_service = IndexingService(
        parser=InMemoryParser(),
        chunker=InMemoryChunker(max_tokens=20, overlap_tokens=0),
        embedder=deps.retrieval_service.embedder,
        vector_store=deps.retrieval_service.vector_store,
        pii_service=deps.pii_service,
        chunk_repo=InMemoryChunkRepository(),
        document_repo=InMemoryDocumentRepository(),
        job_repo=InMemoryIndexingJobRepository(),
    )

    from app.services.document_storage import LocalFilesystemStorage

    storage = LocalFilesystemStorage(
        base_dir=Path(tempfile.gettempdir()) / "domainrag-inmemory-uploads"
    )

    return IndexingOrchestrator(
        indexing_service=indexing_service,
        storage=storage,
        config_loader=deps.config_loader,
    )
