"""Startup preload — tenant_config_overrides + tenant_input_schemas (ADR-021 §1).

운영 multi-instance에서 backend 재시작 시 in-memory runtime override가 소실되지
않도록 lifespan에서 DB → load() 일괄 preload한다. PUT으로 갱신된 값이 같은 프로세스
재시작 후에도 즉시 반영 보장.

- preload_tenant_configs: tenant_config_overrides 전체 row를 (tenant_id, category)로
  그룹화 + dotted path를 nested dict로 풀어 TenantConfigService.apply_runtime_override
- preload_tenant_input_schemas: tenant_input_schemas WHERE status='active' SELECT 후
  InputSchemaLoader.apply_runtime_override

admin engine(BYPASSRLS) 사용 — cross-tenant 일괄 SELECT.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.input_schema import InputSchemaLoader
from app.core.tenant_config_service import TenantConfigService

logger = logging.getLogger(__name__)


def _set_nested(target: dict, dotted_path: str, value: Any) -> None:
    """`a.b.c` + value를 nested dict로 적재.

    빈 path('')는 value가 dict면 통째로 target에 merge. 그 외 dict 위 deep merge.
    """
    if not dotted_path:
        if isinstance(value, dict):
            _deep_merge_into(target, value)
        return
    parts = dotted_path.split(".")
    cur = target
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    last = parts[-1]
    if isinstance(value, dict) and isinstance(cur.get(last), dict):
        _deep_merge_into(cur[last], value)
    else:
        cur[last] = value


def _deep_merge_into(base: dict, override: dict) -> None:
    """base를 in-place로 deep merge (ADR-009 §4 Y3 정합)."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge_into(base[k], v)
        else:
            base[k] = v


async def preload_tenant_configs(
    *, admin_session_factory: async_sessionmaker
) -> int:
    """tenant_config_overrides 전체 row → runtime override 일괄 적재.

    Returns: 적재된 (tenant_id, category) 그룹 수.
    """
    grouped: dict[tuple[str, str], dict] = {}
    async with admin_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT tenant_id, category, path, value
                      FROM tenant_config_overrides
                     WHERE active = TRUE
                    """
                )
            )
        ).all()
    for tenant_id, category, path, value in rows:
        key = (str(tenant_id), str(category))
        bucket = grouped.setdefault(key, {})
        # value는 jsonb → asyncpg/sqlalchemy가 이미 dict/primitive로 decode
        _set_nested(bucket, path or "", value)
    for (tenant_id, category), nested in grouped.items():
        TenantConfigService.apply_runtime_override(tenant_id, category, nested)
    logger.info(
        "preload_tenant_configs",
        rows=len(rows),
        groups=len(grouped),
    )
    return len(grouped)


async def preload_tenant_input_schemas(
    *, admin_session_factory: async_sessionmaker
) -> int:
    """tenant_input_schemas active row → InputSchemaLoader runtime override.

    Returns: 적재된 tenant 수.
    """
    count = 0
    async with admin_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT tenant_id, schema_yaml
                      FROM tenant_input_schemas
                     WHERE status = 'active'
                    """
                )
            )
        ).all()
    for tenant_id, schema_yaml in rows:
        if isinstance(schema_yaml, dict):
            InputSchemaLoader.apply_runtime_override(str(tenant_id), schema_yaml)
            count += 1
    logger.info("preload_tenant_input_schemas", tenants=count)
    return count


async def reload_tenant_config(
    *, admin_session_factory: async_sessionmaker, tenant_id: str
) -> None:
    """NOTIFY tenant_config_changed 수신 시 호출 — 단일 tenant만 재로드."""
    nested: dict[str, dict] = {}
    async with admin_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT category, path, value
                      FROM tenant_config_overrides
                     WHERE active = TRUE AND tenant_id = :tid
                    """
                ),
                {"tid": tenant_id},
            )
        ).all()
    for category, path, value in rows:
        bucket = nested.setdefault(str(category), {})
        _set_nested(bucket, path or "", value)
    # 기존 runtime layer를 비우고 새로 채운다 (delete 반영)
    TenantConfigService.clear_runtime_overrides(tenant_id)
    for category, value in nested.items():
        TenantConfigService.apply_runtime_override(tenant_id, category, value)


async def reload_tenant_schema(
    *, admin_session_factory: async_sessionmaker, tenant_id: str
) -> None:
    """NOTIFY tenant_schema_changed 수신 시 호출 — active schema 재로드."""
    async with admin_session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT schema_yaml
                      FROM tenant_input_schemas
                     WHERE status = 'active' AND tenant_id = :tid
                    """
                ),
                {"tid": tenant_id},
            )
        ).first()
    if row is None:
        InputSchemaLoader.clear_runtime_override(tenant_id)
        return
    schema_yaml = row[0]
    if isinstance(schema_yaml, dict):
        InputSchemaLoader.apply_runtime_override(tenant_id, schema_yaml)
