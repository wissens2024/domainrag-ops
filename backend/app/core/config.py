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

    # CORS
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:8000"]
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

    # AuthFusion (ADR-018)
    auth_mode: str = Field(default="mock", alias="AUTH_MODE")
    authfusion_issuer: str = Field(default="https://sso.aines.kr", alias="AUTHFUSION_ISSUER")
    authfusion_jwks_uri: str = Field(
        default="https://sso.aines.kr/.well-known/jwks.json", alias="AUTHFUSION_JWKS_URI"
    )
    authfusion_authorize_endpoint: str = Field(
        default="https://sso.aines.kr/oauth2/authorize",
        alias="AUTHFUSION_AUTHORIZE_ENDPOINT",
    )
    authfusion_token_endpoint: str = Field(
        default="https://sso.aines.kr/oauth2/token", alias="AUTHFUSION_TOKEN_ENDPOINT"
    )
    authfusion_revoke_endpoint: str = Field(
        default="https://sso.aines.kr/oauth2/revoke", alias="AUTHFUSION_REVOKE_ENDPOINT"
    )

    # OAuth2 callback (frontend → backend) — redirect_uri는 AuthFusion에 사전 등록
    app_base_url: str = Field(default="http://localhost:3000", alias="APP_BASE_URL")

    # KeyHub (ADR-019)
    keyhub_endpoint: str = Field(default="http://localhost:8085", alias="KEYHUB_ENDPOINT")
    keyhub_api_key: str = Field(default="changeme_keyhub_apikey", alias="KEYHUB_API_KEY")

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
    return Settings()
