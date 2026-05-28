"""InMemoryTenantInputSchemaRepository unit tests — ADR-015 + ADR-017 §15.

status 머신(active→deprecated) + schema_version 단조 증가 + optimistic lock 검증.
"""

from __future__ import annotations

import pytest

from rag_core.interfaces.tenant_input_schema_repository import (
    InMemoryTenantInputSchemaRepository,
    SchemaVersionConflictError,
)


_BASE_YAML = {
    "input_types": [
        {"name": "policy_document", "title": "정책 문서"}
    ]
}


async def test_insert_initial_active_version_1():
    repo = InMemoryTenantInputSchemaRepository()
    rec = await repo.insert_new_active(
        domain_id="t1", base_version=None,
        schema_yaml=_BASE_YAML, ui_schema_yaml={},
    )
    assert rec.schema_version == 1
    assert rec.status == "active"
    assert rec.created_at is not None


async def test_get_active_returns_latest_active():
    repo = InMemoryTenantInputSchemaRepository()
    await repo.insert_new_active(
        domain_id="t1", base_version=None,
        schema_yaml=_BASE_YAML, ui_schema_yaml={},
    )
    record = await repo.get_active(domain_id="t1")
    assert record is not None
    assert record.schema_version == 1


async def test_insert_with_correct_base_increments_and_deprecates():
    repo = InMemoryTenantInputSchemaRepository()
    await repo.insert_new_active(
        domain_id="t1", base_version=None,
        schema_yaml=_BASE_YAML, ui_schema_yaml={},
    )
    new = await repo.insert_new_active(
        domain_id="t1", base_version=1,
        schema_yaml={"input_types": [
            {"name": "policy_document"}, {"name": "incident_report"}
        ]},
        ui_schema_yaml={},
    )
    assert new.schema_version == 2

    history = await repo.list_history(domain_id="t1")
    assert len(history) == 2
    assert history[0].schema_version == 2
    assert history[0].status == "active"
    assert history[1].schema_version == 1
    assert history[1].status == "deprecated"
    assert history[1].deprecated_at is not None


async def test_insert_with_wrong_base_raises_conflict():
    repo = InMemoryTenantInputSchemaRepository()
    await repo.insert_new_active(
        domain_id="t1", base_version=None,
        schema_yaml=_BASE_YAML, ui_schema_yaml={},
    )
    with pytest.raises(SchemaVersionConflictError) as exc_info:
        await repo.insert_new_active(
            domain_id="t1", base_version=99,
            schema_yaml=_BASE_YAML, ui_schema_yaml={},
        )
    assert exc_info.value.current == 1
    assert exc_info.value.base == 99


async def test_insert_initial_with_nonnull_base_raises():
    """tenant 미등록인데 base_version 명시 → conflict."""
    repo = InMemoryTenantInputSchemaRepository()
    with pytest.raises(SchemaVersionConflictError):
        await repo.insert_new_active(
            domain_id="t1", base_version=1,
            schema_yaml=_BASE_YAML, ui_schema_yaml={},
        )


async def test_list_history_pagination():
    repo = InMemoryTenantInputSchemaRepository()
    # 3개 active→deprecate 체인
    await repo.insert_new_active(domain_id="t1", base_version=None,
                                  schema_yaml=_BASE_YAML, ui_schema_yaml={})
    await repo.insert_new_active(domain_id="t1", base_version=1,
                                  schema_yaml=_BASE_YAML, ui_schema_yaml={})
    await repo.insert_new_active(domain_id="t1", base_version=2,
                                  schema_yaml=_BASE_YAML, ui_schema_yaml={})

    page1 = await repo.list_history(domain_id="t1", limit=2, offset=0)
    assert [r.schema_version for r in page1] == [3, 2]
    page2 = await repo.list_history(domain_id="t1", limit=2, offset=2)
    assert [r.schema_version for r in page2] == [1]


async def test_tenant_isolation():
    repo = InMemoryTenantInputSchemaRepository()
    await repo.insert_new_active(domain_id="t1", base_version=None,
                                  schema_yaml=_BASE_YAML, ui_schema_yaml={})
    await repo.insert_new_active(domain_id="t2", base_version=None,
                                  schema_yaml=_BASE_YAML, ui_schema_yaml={})
    # 두 tenant 모두 version=1로 시작 (전역 UNIQUE가 아니라 (tenant, version))
    t1 = await repo.get_active(domain_id="t1")
    t2 = await repo.get_active(domain_id="t2")
    assert t1.schema_version == t2.schema_version == 1
    assert t1.schema_id != t2.schema_id
