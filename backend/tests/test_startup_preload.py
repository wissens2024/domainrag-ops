"""ADR-021 §1 startup preload + helper 검증.

DB가 필요 없는 unit path만 검증. Postgres LISTEN/NOTIFY 자체는 integration test
(testcontainers)에서 검증.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.input_schema import InputSchemaLoader
from app.core.tenant_config_service import TenantConfigService
from app.services.listen_notify import PostgresNotifyListener
from app.services.startup_preload import (
    _deep_merge_into,
    _set_nested,
    preload_tenant_configs,
    preload_tenant_input_schemas,
    reload_tenant_config,
    reload_tenant_schema,
)


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def test_set_nested_creates_nested_dict():
    target: dict = {}
    _set_nested(target, "a.b.c", 1)
    assert target == {"a": {"b": {"c": 1}}}


def test_set_nested_deep_merge_dicts():
    target: dict = {"a": {"b": {"x": 1}}}
    _set_nested(target, "a.b", {"y": 2})
    assert target == {"a": {"b": {"x": 1, "y": 2}}}


def test_set_nested_empty_path_merges_dict():
    target: dict = {"existing": 1}
    _set_nested(target, "", {"new": 2})
    assert target == {"existing": 1, "new": 2}


def test_set_nested_overwrites_primitive():
    target: dict = {"a": {"b": 1}}
    _set_nested(target, "a.b", 2)
    assert target == {"a": {"b": 2}}


def test_deep_merge_into_lists_replaced():
    base: dict = {"rules": [1, 2, 3]}
    _deep_merge_into(base, {"rules": [4, 5]})
    assert base == {"rules": [4, 5]}


def test_listener_dsn_strips_sqlalchemy_prefix():
    listener = PostgresNotifyListener(
        dsn="postgresql+asyncpg://u:p@h/db", handlers={}
    )
    assert listener._dsn == "postgresql://u:p@h/db"


def test_listener_dsn_unchanged_when_raw():
    listener = PostgresNotifyListener(
        dsn="postgresql://u:p@h/db", handlers={}
    )
    assert listener._dsn == "postgresql://u:p@h/db"


# --------------------------------------------------------------------------- #
# preload — mocked session_factory
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return list(self._rows)

    def first(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, response_map: dict[str, list[tuple]]) -> None:
        # response_map: query substring → rows
        self._map = response_map
        self.queries: list[str] = []

    async def execute(self, stmt: Any, params: dict | None = None) -> _FakeResult:  # noqa: ARG002
        sql = str(stmt)
        self.queries.append(sql)
        for key, rows in self._map.items():
            if key in sql:
                # SELECT가 domain_id를 컬럼으로 포함하지 않는 (3-tuple) reload path는
                # 테스트가 사전 필터링한다고 가정.
                return _FakeResult(rows)
        return _FakeResult([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _fake_session_factory(response_map: dict[str, list[tuple]]):
    @asynccontextmanager
    async def _factory():  # session_factory(): async with → __aenter__
        async with _FakeSession(response_map) as s:
            yield s

    # async_sessionmaker는 호출 시 session 객체를 반환 — 호출 1번
    factory = MagicMock()
    factory.side_effect = lambda: _FakeSession(response_map)
    return factory


@pytest.fixture(autouse=True)
def _clear_runtime_layers():
    TenantConfigService.clear_runtime_overrides()
    InputSchemaLoader.clear_runtime_override()
    yield
    TenantConfigService.clear_runtime_overrides()
    InputSchemaLoader.clear_runtime_override()


async def _arun(coro):
    return await coro


def test_preload_tenant_configs_groups_by_category():
    rows = [
        ("security", "routing", "", {"default_route": {"model": "tenant_slm"}}),
        ("security", "citation", "verification.tier2.thresholds.strong", 0.78),
        ("security", "citation", "verification.tier2.thresholds.medium", 0.55),
        ("legal", "routing", "", {"default_route": {"model": "shared_llm"}}),
    ]
    factory = _fake_session_factory({"tenant_config_overrides": rows})

    asyncio.run(preload_tenant_configs(admin_session_factory=factory))

    security_cfg = TenantConfigService._runtime_overrides.get("security") or {}
    assert security_cfg["routing"] == {"default_route": {"model": "tenant_slm"}}
    assert security_cfg["citation"] == {
        "verification": {"tier2": {"thresholds": {"strong": 0.78, "medium": 0.55}}}
    }
    legal_cfg = TenantConfigService._runtime_overrides.get("legal") or {}
    assert legal_cfg["routing"] == {"default_route": {"model": "shared_llm"}}


def test_preload_tenant_input_schemas_applies_runtime_yaml():
    rows = [
        ("security", {"input_types": {"policy": {"display_name": "정책"}}}),
        ("legal", {"input_types": {"contract": {"display_name": "계약"}}}),
    ]
    factory = _fake_session_factory({"tenant_input_schemas": rows})

    count = asyncio.run(
        preload_tenant_input_schemas(admin_session_factory=factory)
    )

    assert count == 2
    assert "security" in InputSchemaLoader._runtime_yaml
    assert "legal" in InputSchemaLoader._runtime_yaml
    assert (
        InputSchemaLoader._runtime_yaml["security"]["input_types"]["policy"][
            "display_name"
        ]
        == "정책"
    )


def test_reload_tenant_config_replaces_existing_runtime():
    # 기존 runtime을 채워둔다
    TenantConfigService.apply_runtime_override(
        "security", "routing", {"default_route": {"model": "old"}}
    )
    rows = [
        ("routing", "", {"default_route": {"model": "new"}}),
    ]
    factory = _fake_session_factory({"tenant_config_overrides": rows})

    asyncio.run(
        reload_tenant_config(admin_session_factory=factory, domain_id="security")
    )

    cfg = TenantConfigService._runtime_overrides.get("security") or {}
    assert cfg["routing"] == {"default_route": {"model": "new"}}


def test_reload_tenant_schema_clears_when_no_active_row():
    InputSchemaLoader.apply_runtime_override(
        "security", {"input_types": {"old": {}}}
    )
    factory = _fake_session_factory({"tenant_input_schemas": []})
    asyncio.run(
        reload_tenant_schema(admin_session_factory=factory, domain_id="security")
    )
    assert "security" not in InputSchemaLoader._runtime_yaml
