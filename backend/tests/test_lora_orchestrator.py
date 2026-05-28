"""LoRAOrchestrator — KeyHub fetch + vLLM hot-load·unload + rollback (ADR-013 §5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.lora_orchestrator import (
    LoRAOrchestrationError,
    LoRAOrchestrator,
)
from rag_core.clients.local_secret_store import LocalSecretStore
from rag_core.interfaces.lora_registry import (
    AdapterRecord,
    InMemoryLoRARegistry,
    LoRANotFoundError,
)


class _FakeVllm:
    def __init__(self) -> None:
        self.loaded: list[tuple[str, str]] = []
        self.unloaded: list[str] = []
        self.next_load_raises: Exception | None = None
        self.next_unload_raises: Exception | None = None

    async def load_lora_adapter(self, *, lora_name: str, lora_path: str) -> None:
        if self.next_load_raises is not None:
            err = self.next_load_raises
            self.next_load_raises = None
            raise err
        self.loaded.append((lora_name, lora_path))

    async def unload_lora_adapter(self, *, lora_name: str) -> None:
        if self.next_unload_raises is not None:
            err = self.next_unload_raises
            self.next_unload_raises = None
            raise err
        self.unloaded.append(lora_name)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def setup(tmp_path: Path):
    keyhub = LocalSecretStore(base_path=tmp_path / "secrets")
    registry = InMemoryLoRARegistry()
    vllm = _FakeVllm()
    orch = LoRAOrchestrator(
        registry=registry,
        keyhub=keyhub,
        vllm=vllm,
        shared_lora_path=tmp_path / "lora",
    )
    return orch, registry, vllm, keyhub


def _upload(keyhub, registry, *, domain_id: str, adapter_id: str) -> str:
    ref = _run(
        keyhub.put_secret(f"lora/{domain_id}/{adapter_id}", b"WEIGHTS")
    )
    rec = AdapterRecord(
        adapter_id=adapter_id,
        domain_id=domain_id,
        version="v1",
        base_model="Qwen2.5-7B",
        keyhub_secret_ref=ref,
    )
    _run(registry.upload(rec))
    return ref


def test_activate_loads_into_vllm_then_marks_active(setup):
    orch, registry, vllm, keyhub = setup
    _upload(keyhub, registry, domain_id="t1", adapter_id="A")

    record = _run(orch.activate(domain_id="t1", adapter_id="A"))

    assert record.status == "active"
    assert len(vllm.loaded) == 1
    name, path = vllm.loaded[0]
    assert name == "A"
    assert "t1" in path and "A" in path
    # weights 파일이 실제 존재
    assert (Path(path) / "adapter_model.bin").read_bytes() == b"WEIGHTS"


def test_activate_unknown_raises_not_found(setup):
    orch, *_ = setup
    with pytest.raises(LoRANotFoundError):
        _run(orch.activate(domain_id="t1", adapter_id="NOPE"))


def test_activate_keyhub_fetch_fail_raises_orchestration(setup, tmp_path):
    orch, registry, _vllm, _kh = setup
    # registry에 등록하지만 KeyHub에는 secret 없음
    _run(
        registry.upload(
            AdapterRecord(
                adapter_id="X",
                domain_id="t1",
                keyhub_secret_ref="local://missing",
            )
        )
    )
    with pytest.raises(LoRAOrchestrationError):
        _run(orch.activate(domain_id="t1", adapter_id="X"))


def test_activate_vllm_failure_rollback(setup):
    orch, registry, vllm, keyhub = setup
    _upload(keyhub, registry, domain_id="t1", adapter_id="A")
    vllm.next_load_raises = RuntimeError("vllm down")

    with pytest.raises(LoRAOrchestrationError):
        _run(orch.activate(domain_id="t1", adapter_id="A"))

    # registry 상태는 그대로 'registered' (rollback)
    rec = _run(registry.get(domain_id="t1", adapter_id="A"))
    assert rec.status == "registered"
    # 파일은 정리됨
    adapter_dir = orch.shared_lora_path / "t1" / "A"
    assert not adapter_dir.exists() or not any(adapter_dir.iterdir())


def test_retire_calls_vllm_unload(setup):
    orch, registry, vllm, keyhub = setup
    _upload(keyhub, registry, domain_id="t1", adapter_id="A")
    _run(orch.activate(domain_id="t1", adapter_id="A"))

    rec = _run(orch.retire(domain_id="t1", adapter_id="A"))

    assert rec.status == "retired"
    assert vllm.unloaded == ["A"]


def test_retire_swallows_vllm_unload_failure(setup):
    orch, registry, vllm, keyhub = setup
    _upload(keyhub, registry, domain_id="t1", adapter_id="A")
    _run(orch.activate(domain_id="t1", adapter_id="A"))
    vllm.next_unload_raises = RuntimeError("already gone")

    # 예외 없이 retired로 전이해야 함
    rec = _run(orch.retire(domain_id="t1", adapter_id="A"))
    assert rec.status == "retired"


def test_activate_without_vllm_only_transits_registry(tmp_path):
    keyhub = LocalSecretStore(base_path=tmp_path / "secrets")
    registry = InMemoryLoRARegistry()
    orch = LoRAOrchestrator(
        registry=registry, keyhub=keyhub, vllm=None,
        shared_lora_path=tmp_path / "lora",
    )
    _upload(keyhub, registry, domain_id="t1", adapter_id="A")

    rec = _run(orch.activate(domain_id="t1", adapter_id="A"))
    assert rec.status == "active"
    # 파일 쓰기·vLLM 호출 없음 — adapter_dir 미생성
    assert not (orch.shared_lora_path / "t1" / "A").exists()
