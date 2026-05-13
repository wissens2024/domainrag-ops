"""InMemoryLoRARegistry unit tests — ADR-017 §14 + ADR-013 status 머신."""

from __future__ import annotations

import pytest

from rag_core.interfaces.lora_registry import (
    AdapterRecord,
    InMemoryLoRARegistry,
    LoRAConflictError,
    LoRADeleteForbiddenError,
    LoRAInvalidTransitionError,
    LoRANotFoundError,
)


def _make(adapter_id: str, tenant_id: str = "t1") -> AdapterRecord:
    return AdapterRecord(
        adapter_id=adapter_id, tenant_id=tenant_id,
        version="v1", base_model="qwen-7b",
    )


async def test_upload_persists_with_registered_status():
    reg = InMemoryLoRARegistry()
    rec = await reg.upload(_make("a1"))
    assert rec.status == "registered"
    assert rec.registered_at is not None
    assert rec.activated_at is None


async def test_upload_conflict_raises():
    reg = InMemoryLoRARegistry()
    await reg.upload(_make("a1"))
    with pytest.raises(LoRAConflictError):
        await reg.upload(_make("a1"))


async def test_list_filters_by_tenant_and_status():
    reg = InMemoryLoRARegistry()
    await reg.upload(_make("a1", "t1"))
    await reg.upload(_make("a2", "t1"))
    await reg.upload(_make("a3", "t2"))
    await reg.activate(tenant_id="t1", adapter_id="a1")
    # t1 전체 — 2건
    assert len(await reg.list_by_tenant(tenant_id="t1")) == 2
    # t1 active만 — 1건
    active = await reg.list_by_tenant(tenant_id="t1", status="active")
    assert len(active) == 1
    assert active[0].adapter_id == "a1"
    # t2 — 1건만
    assert len(await reg.list_by_tenant(tenant_id="t2")) == 1


async def test_activate_sets_activated_at_and_idempotent():
    reg = InMemoryLoRARegistry()
    await reg.upload(_make("a1"))
    first = await reg.activate(tenant_id="t1", adapter_id="a1")
    assert first.status == "active"
    assert first.activated_at is not None
    # 두 번째 호출 — 같은 결과(activated_at 그대로)
    second = await reg.activate(tenant_id="t1", adapter_id="a1")
    assert second.activated_at == first.activated_at


async def test_retire_after_active():
    reg = InMemoryLoRARegistry()
    await reg.upload(_make("a1"))
    await reg.activate(tenant_id="t1", adapter_id="a1")
    rec = await reg.retire(tenant_id="t1", adapter_id="a1")
    assert rec.status == "retired"
    assert rec.retired_at is not None


async def test_retire_idempotent():
    reg = InMemoryLoRARegistry()
    await reg.upload(_make("a1"))
    first = await reg.retire(tenant_id="t1", adapter_id="a1")
    second = await reg.retire(tenant_id="t1", adapter_id="a1")
    assert second.retired_at == first.retired_at


async def test_activate_retired_invalid_transition():
    reg = InMemoryLoRARegistry()
    await reg.upload(_make("a1"))
    await reg.activate(tenant_id="t1", adapter_id="a1")
    await reg.retire(tenant_id="t1", adapter_id="a1")
    with pytest.raises(LoRAInvalidTransitionError):
        await reg.activate(tenant_id="t1", adapter_id="a1")


async def test_delete_forbidden_when_active():
    reg = InMemoryLoRARegistry()
    await reg.upload(_make("a1"))
    await reg.activate(tenant_id="t1", adapter_id="a1")
    with pytest.raises(LoRADeleteForbiddenError):
        await reg.delete(tenant_id="t1", adapter_id="a1")


async def test_delete_registered_then_retired_works():
    reg = InMemoryLoRARegistry()
    await reg.upload(_make("a1"))
    assert await reg.delete(tenant_id="t1", adapter_id="a1") == 1
    await reg.upload(_make("a2"))
    await reg.activate(tenant_id="t1", adapter_id="a2")
    await reg.retire(tenant_id="t1", adapter_id="a2")
    assert await reg.delete(tenant_id="t1", adapter_id="a2") == 1


async def test_delete_unknown_returns_zero():
    reg = InMemoryLoRARegistry()
    assert await reg.delete(tenant_id="t1", adapter_id="ghost") == 0


async def test_get_tenant_isolated():
    reg = InMemoryLoRARegistry()
    await reg.upload(_make("a1", "t1"))
    assert await reg.get(tenant_id="t1", adapter_id="a1") is not None
    assert await reg.get(tenant_id="t2", adapter_id="a1") is None


async def test_activate_unknown_raises():
    reg = InMemoryLoRARegistry()
    with pytest.raises(LoRANotFoundError):
        await reg.activate(tenant_id="t1", adapter_id="ghost")
