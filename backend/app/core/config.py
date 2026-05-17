"""
Settings — 환경변수 + configs/platform/* yaml 합성 (ADR-009).

본 모듈은 application-level config (env vars). domain configs는
TenantConfigService(`app.core.tenant_config_service`)에서 별도 관리.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수 — .env 또는 OS env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 환경
    env: str = Field(default="development", alias="ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Configs
    config_dir: Path = Field(default=Path("./configs"), alias="CONFIG_DIR")

    # CORS — frontend dev 호스트(3010 = WiSentinel/AuthFusion 충돌 회피 후 정착 포트).
    # 운영 배포는 https://rag.aines.kr 명시 추가 필요.
    cors_origins: list[str] = Field(
        default=["http://localhost:3010", "http://localhost:8000"]
    )

    # PostgreSQL (ADR-019)
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="domainrag", alias="POSTGRES_DB")
    postgres_user: str = Field(default="domainrag_app", alias="POSTGRES_USER")
    postgres_password: str = Field(default="changeme", alias="POSTGRES_PASSWORD")
    postgres_admin_user: str = Field(
        default="domainrag_platform_admin", alias="POSTGRES_ADMIN_USER"
    )
    postgres_admin_password: str = Field(default="changeme_admin", alias="POSTGRES_ADMIN_PASSWORD")

    # Qdrant
    qdrant_host: str = Field(default="localhost", alias="QDRANT_HOST")
    qdrant_port: int = Field(default=6333, alias="QDRANT_PORT")
    qdrant_grpc_port: int = Field(default=6334, alias="QDRANT_GRPC_PORT")
    qdrant_api_key: str | None = Field(default=None, alias="QDRANT_API_KEY")

    # MinIO
    minio_endpoint: str = Field(default="localhost:9000", alias="MINIO_ENDPOINT")
    minio_access_key: str = Field(default="minioadmin", alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(default="changeme_minio", alias="MINIO_SECRET_KEY")
    minio_bucket: str = Field(default="domainrag", alias="MINIO_BUCKET")
    minio_use_ssl: bool = Field(default=False, alias="MINIO_USE_SSL")

    # vLLM endpoints — ADR-019 §3·§4: 174번 단일 vLLM instance를 tenant/shared가 alias 공유
    tenant_slm_base_url: str = Field(
        default="http://localhost:8000/v1", alias="TENANT_SLM_BASE_URL"
    )
    shared_llm_base_url: str = Field(
        default="http://localhost:8000/v1",  # tenant_slm과 동일 default — 별도 instance 아님
        alias="SHARED_LLM_BASE_URL",
    )
    embedding_server_url: str = Field(default="http://localhost:8002", alias="EMBEDDING_SERVER_URL")
    reranker_server_url: str = Field(default="http://localhost:8003", alias="RERANKER_SERVER_URL")
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")

    # RAG backend selection — production / inmemory (dev demo)
    rag_backend: str = Field(default="production", alias="RAG_BACKEND")
    rag_default_model: str = Field(default="qwen3-7b-instruct", alias="RAG_DEFAULT_MODEL")

    # AuthFusion (ADR-018) — issuer는 사용자 API 도메인(api.aines.kr).
    # Console은 console.aines.kr(관리자 전용)로 분리되어 있다 — RP는 console 직접 호출 안 함.
    # auth_mode는 "oidc" 단일 운영. mock은 ADR-018 §9에 따라 test fixture로 격리.
    auth_mode: str = Field(default="oidc", alias="AUTH_MODE")
    authfusion_issuer: str = Field(default="https://api.aines.kr", alias="AUTHFUSION_ISSUER")
    # Discovery URL — endpoint 4종을 individual env로 받는 대신 discovery 1번으로 일괄 fetch.
    # 미설정 시 issuer + /.well-known/openid-configuration 으로 합성.
    authfusion_discovery_url: str | None = Field(
        default=None, alias="AUTHFUSION_DISCOVERY_URL"
    )
    # 개별 endpoint env는 discovery 미가용 환경(또는 명시 override) 한정.
    authfusion_jwks_uri: str | None = Field(default=None, alias="AUTHFUSION_JWKS_URI")
    authfusion_authorize_endpoint: str | None = Field(
        default=None, alias="AUTHFUSION_AUTHORIZE_ENDPOINT"
    )
    authfusion_token_endpoint: str | None = Field(
        default=None, alias="AUTHFUSION_TOKEN_ENDPOINT"
    )
    authfusion_revoke_endpoint: str | None = Field(
        default=None, alias="AUTHFUSION_REVOKE_ENDPOINT"
    )

    # OIDC client credentials — CONFIDENTIAL client (AuthFusion 권장 + PKCE 동시).
    # 등록 응답의 clientId(UUID) + clientSecret(평문 1회). KeyHub 사용 권장이나 .env 직접도 지원.
    authfusion_client_id: str | None = Field(default=None, alias="AUTHFUSION_CLIENT_ID")
    authfusion_client_secret: str | None = Field(
        default=None, alias="AUTHFUSION_CLIENT_SECRET"
    )

    # email_verified 검증 강제 여부 (id_token claim). AuthFusion 표준 동작.
    oidc_require_verified_email: bool = Field(
        default=True, alias="OIDC_REQUIRE_VERIFIED_EMAIL"
    )

    # OAuth2 callback (frontend → backend) — redirect_uri는 AuthFusion에 사전 등록.
    # 가이드 표준: https://<rp>/api/v1/auth/sso/callback — DomainRAG는 frontend가 받아
    # backend `/api/auth/callback`으로 forward하는 구조. ADR-018 §2.
    app_base_url: str = Field(default="http://localhost:3010", alias="APP_BASE_URL")

    # KeyHub (ADR-019 §8) — 운영(authfusion)·dev(local) 선택
    keyhub_mode: str = Field(default="local", alias="KEYHUB_MODE")  # local | authfusion
    keyhub_endpoint: str = Field(default="http://localhost:8085", alias="KEYHUB_ENDPOINT")
    keyhub_api_key: str = Field(default="changeme_keyhub_apikey", alias="KEYHUB_API_KEY")
    # LocalSecretStore — dev fallback
    keyhub_local_path: str = Field(
        default="./var/secrets", alias="KEYHUB_LOCAL_PATH"
    )
    keyhub_local_fernet_key: str | None = Field(
        default=None, alias="KEYHUB_LOCAL_FERNET_KEY"
    )

    # vLLM LoRA hot-swap (ADR-013 §5, ADR-019 §8)
    vllm_shared_lora_path: str = Field(
        default="./var/lora",
        alias="VLLM_SHARED_LORA_PATH",
    )

    # Ops cron (ADR-021 §3) — internal: lifespan task로 등록 / external: 외부 cron
    ops_cron_mode: str = Field(default="internal", alias="OPS_CRON_MODE")
    ops_cron_archival_interval_seconds: float = Field(
        default=86400.0, alias="OPS_CRON_ARCHIVAL_INTERVAL_SECONDS"
    )
    ops_cron_partition_interval_seconds: float = Field(
        default=86400.0, alias="OPS_CRON_PARTITION_INTERVAL_SECONDS"
    )
    ops_cron_old_collection_interval_seconds: float = Field(
        default=604800.0, alias="OPS_CRON_OLD_COLLECTION_INTERVAL_SECONDS"
    )  # 7d (매주)

    # Audit / Ledger (ADR-020 §8)
    ledger_enable: bool = Field(default=False, alias="LEDGER_ENABLE")
    ledger_endpoint: str = Field(default="http://localhost:8089", alias="LEDGER_ENDPOINT")
    ledger_api_key: str = Field(default="", alias="LEDGER_API_KEY")

    @property
    def db_dsn(self) -> str:
        """SQLAlchemy DSN — app role (RLS 적용)."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def db_admin_dsn(self) -> str:
        """SQLAlchemy DSN — platform_admin role (BYPASSRLS, cross-tenant 분석)."""
        return (
            f"postgresql+asyncpg://{self.postgres_admin_user}:{self.postgres_admin_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    _enforce_auth_policy(settings)
    return settings


def _enforce_auth_policy(s: Settings) -> None:
    """ADR-018 §9 — mock auth는 더 이상 운영 코드 경로에 존재 안 함.

    AUTH_MODE는 "oidc" 외 값을 받지 않는다. 테스트는 backend/tests/conftest.py가
    FastAPI `app.dependency_overrides`로 MockAuthAdapter를 주입하므로 settings.auth_mode
    값과 무관하다 (테스트도 authfusion으로 통과).

    레거시 `AUTH_MODE=mock` env가 운영 배포에 잘못 새어 들어가지 않게 fail-fast.
    """
    allowed = {"oidc"}
    if s.auth_mode not in allowed:
        raise RuntimeError(
            f"AUTH_MODE={s.auth_mode!r} 미지원. 허용값: {sorted(allowed)}. "
            "mock 인증은 ADR-018 §9에 따라 backend/tests/_fixtures/mock_auth.py로 격리됨 — "
            "테스트 환경은 conftest.py의 dependency_overrides로 자동 주입."
        )
