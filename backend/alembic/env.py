"""Alembic env — sync engine + env vars 기반 DSN.

alembic은 sync 실행이 표준이고, multi-statement raw SQL을 그대로 받는다
(asyncpg의 prepared-statement 제약이 없음). app 런타임은 별도로 asyncpg 사용 — 영향 없음.
"""

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

from app.core.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Migration은 platform_admin role로 실행 (CREATE/ALTER + BYPASSRLS 권한).
# Settings.db_admin_dsn은 런타임용 asyncpg URL — 여기서는 sync psycopg(3)로 변환.
settings = get_settings()
DSN = settings.db_admin_dsn.replace("+asyncpg", "+psycopg")

# Migration metadata는 골격 단계에서 빈 metadata. 실제 schema는 첫 migration 파일이 raw SQL로.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(url=DSN, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(DSN, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
